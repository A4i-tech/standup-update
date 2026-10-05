import json, urllib.request, os
from datetime import datetime, timedelta, timezone

GITHUB_TOKEN = os.environ['GH_PROJECT_TOKEN']
POWER_AUTOMATE_URL = os.environ['POWER_AUTOMATE_URL']
TARGET_USER = 'farmanahmed888'
ORG = 'A4i-tech'
PR_REPOS = ['byoeb', 'SEEDS', 'Shiksha-Copilot', 'infra-ops', 'OmniIngest', 'ai-ops']
BOT_LOGINS = {'a4i-architect'}

ACTIVE_STATUSES = {'Todo', 'In Development', 'Awaiting Review', 'Awaiting Release'}

def gh_graphql(query, variables=None):
    req = urllib.request.Request(
        'https://api.github.com/graphql',
        data=json.dumps({'query': query, 'variables': variables or {}}).encode(),
        headers={
            'Authorization': f'Bearer {GITHUB_TOKEN}',
            'Content-Type': 'application/json',
            'User-Agent': 'standup-bot'
        }
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def gh_rest(path):
    req = urllib.request.Request(
        f'https://api.github.com{path}',
        headers={
            'Authorization': f'Bearer {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'standup-bot'
        }
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

# 1. Tickets assigned to TARGET_USER in org project #1
PROJECT_QUERY = """
query($org: String!, $after: String) {
  organization(login: $org) {
    projectV2(number: 1) {
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content {
            ... on Issue {
              number title url
              repository { name }
              assignees(first: 10) { nodes { login } }
            }
          }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""

items = []
after = None
while True:
    resp = gh_graphql(PROJECT_QUERY, {'org': ORG, 'after': after})
    page = resp['data']['organization']['projectV2']['items']
    items.extend(page['nodes'])
    if not page['pageInfo']['hasNextPage']:
        break
    after = page['pageInfo']['endCursor']

tickets = {}  # (repo, number) -> {title, url, status, assignees}
for item in items:
    content = item['content']
    if 'repository' not in content:  # draft issue or PR item, not an Issue
        continue
    assignees = [u['login'] for u in content['assignees']['nodes']]
    if not assignees:
        continue

    status = next(
        (fv['name'] for fv in item['fieldValues']['nodes'] if fv.get('field', {}).get('name') == 'Status'),
        ''
    )
    if status not in ACTIVE_STATUSES:
        continue

    repo = content['repository']['name']
    tickets[(repo, content['number'])] = {
        'title': content['title'][:55],
        'url': content['url'],
        'status': status,
        'assignees': assignees,
    }

# 2. Open and merged PRs in the tracked repos, with the issues each PR closes.
# Merged PRs count as "handled" (e.g. merged to a staging branch that never
# auto-closed the issue) even though they no longer need review.
PR_QUERY = """
query($owner: String!, $repo: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(states: [OPEN, MERGED], first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url createdAt state isDraft mergeable
        author { login }
        closingIssuesReferences(first: 10) {
          nodes { number repository { name } }
        }
        reviewRequests(first: 10) {
          nodes { requestedReviewer { ... on User { login } } }
        }
      }
    }
  }
}
"""

def pending_reviewers(pr):
    requested = {
        n['requestedReviewer']['login'] for n in pr['reviewRequests']['nodes']
        if n['requestedReviewer']
    }
    return requested - BOT_LOGINS

def fetch_prs(pr_repo):
    prs, after = [], None
    while True:
        resp = gh_graphql(PR_QUERY, {'owner': ORG, 'repo': pr_repo, 'after': after})
        page = resp['data']['repository']['pullRequests']
        prs.extend(page['nodes'])
        if not page['pageInfo']['hasNextPage']:
            return prs
        after = page['pageInfo']['endCursor']

pr_by_ticket = {}  # (issue_repo, issue_number) -> open pr dict
draft_by_ticket = {}  # (issue_repo, issue_number) -> draft pr dict
handled_tickets = set()  # (issue_repo, issue_number) with any open or merged PR
for pr_repo in PR_REPOS:
    prs = fetch_prs(pr_repo)
    for pr in prs:
        pr['_repo'] = pr_repo
        if pr['isDraft']:  # not ready for review; keep separate from "no PR yet"
            for closed_issue in pr['closingIssuesReferences']['nodes']:
                key = (closed_issue['repository']['name'], closed_issue['number'])
                if key in tickets and key not in draft_by_ticket:
                    draft_by_ticket[key] = pr
            continue
        pr['_reviewers'] = pending_reviewers(pr)
        for closed_issue in pr['closingIssuesReferences']['nodes']:
            key = (closed_issue['repository']['name'], closed_issue['number'])
            if key not in tickets:
                continue
            handled_tickets.add(key)
            if pr['state'] == 'OPEN' and key not in pr_by_ticket:
                pr_by_ticket[key] = pr

# 3. First "review requested" timestamp per matched PR (fallback: PR createdAt)
def review_raised_at(repo, pr):
    events = gh_rest(f'/repos/{ORG}/{repo}/issues/{pr["number"]}/timeline?per_page=100')
    requested = [e['created_at'] for e in events if e['event'] == 'review_requested']
    return min(requested) if requested else pr['createdAt']

def days_since(iso_ts):
    start = datetime.strptime(iso_ts, '%Y-%m-%dT%H:%M:%SZ').date()
    elapsed = (datetime.now(timezone.utc).date() - start).days
    return sum((start + timedelta(days=i)).weekday() < 5 for i in range(elapsed))

def traffic_light(days):
    if days <= 2:
        return '🟢'
    if days <= 5:
        return '🟡'
    return '🔴'

reviewed_rows, no_pr_rows, draft_rows, conflict_rows = [], [], [], []
for key, ticket in tickets.items():
    repo, issue_num = key
    pr = pr_by_ticket.get(key)
    issue_md = f'[{repo}#{issue_num}]({ticket["url"]}) {ticket["title"]}'
    if pr:
        days = days_since(review_raised_at(pr['_repo'], pr))
        pr_md = f'[{pr["_repo"]}#{pr["number"]}]({pr["url"]})'
        opened_by = pr['author']['login'] if pr['author'] else 'unknown'
        if pr['mergeable'] == 'CONFLICTING':
            conflict_rows.append([issue_md, opened_by, pr_md, f'{days}d'])
        else:
            emergency = ' 🚨' if days > 15 else ''
            reviewed_rows.append({
                'reviewers': pr['_reviewers'],
                'card': [issue_md, opened_by, pr_md, f'{traffic_light(days)} {days}d{emergency}'],
            })
    elif key in draft_by_ticket:
        draft_pr = draft_by_ticket[key]
        draft_rows.append([
            issue_md,
            f'[{draft_pr["_repo"]}#{draft_pr["number"]}]({draft_pr["url"]})',
            draft_pr['author']['login'] if draft_pr['author'] else 'unknown',
        ])
    elif key not in handled_tickets:
        no_pr_rows.append(key)

rows_by_reviewer = {}
for r in reviewed_rows:
    for reviewer in r['reviewers']:
        rows_by_reviewer.setdefault(reviewer, []).append(r)

PROJECT_BOARD_URL = f'https://github.com/orgs/{ORG}/projects/1/views/2'

def card_table(rows, columns, widths):
    def cell(text, bold=False):
        return {'type': 'TableCell', 'items': [{'type': 'TextBlock', 'text': text, 'wrap': True, 'weight': 'Bolder' if bold else 'Default'}]}
    return {
        'type': 'Table',
        'firstRowAsHeaders': True,
        'columns': [{'width': w} for w in widths],
        'rows': [{'type': 'TableRow', 'cells': [cell(c, True) for c in columns]}]
        + [{'type': 'TableRow', 'cells': [cell(c) for c in r]} for r in rows],
    }

def card_heading(text):
    return {'type': 'TextBlock', 'text': text, 'weight': 'Bolder', 'size': 'Medium', 'separator': True}

card_body = [
    {'type': 'TextBlock', 'text': 'Daily Standup - GitHub Tasks', 'weight': 'Bolder', 'size': 'Large'},
    {'type': 'TextBlock', 'text': '🟢 0-2 days · 🟡 3-5 days · 🔴 6+ days · 🚨 15+ days', 'wrap': True},
]
for reviewer in sorted(rows_by_reviewer, key=str.lower):
    card_body += [
        card_heading(f'Needs review from {reviewer} ({len(rows_by_reviewer[reviewer])})'),
        card_table([r['card'] for r in rows_by_reviewer[reviewer]], ['Issue', 'Opened By', 'PR', 'Status'], [4, 2, 3, 2]),
    ]
if conflict_rows:
    card_body += [
        card_heading(f'Merge Conflicts ({len(conflict_rows)})'),
        card_table(conflict_rows, ['Issue', 'Opened By', 'PR', 'Days'], [4, 2, 3, 1]),
    ]
if draft_rows:
    card_body += [
        card_heading(f'Draft PRs ({len(draft_rows)})'),
        card_table(draft_rows, ['Issue', 'PR', 'Author'], [4, 3, 2]),
    ]
card_body.append(card_heading(f'No PR Yet ({len(no_pr_rows)})'))
card_body.append({'type': 'TextBlock', 'text': f'[View the full list on the project board]({PROJECT_BOARD_URL})'})

card_payload = {
    'type': 'message',
    'attachments': [{
        'contentType': 'application/vnd.microsoft.card.adaptive',
        'contentUrl': None,
        'content': {
            '$schema': 'http://adaptivecards.io/schemas/adaptive-card.json',
            'type': 'AdaptiveCard',
            'version': '1.5',
            'msteams': {'width': 'Full'},
            'body': card_body,
        },
    }],
}

flow_req = urllib.request.Request(
    POWER_AUTOMATE_URL,
    data=json.dumps(card_payload, separators=(',', ':')).encode(),
    headers={'Content-Type': 'application/json'}
)
with urllib.request.urlopen(flow_req) as resp:
    print(f'Power Automate flow triggered: {resp.status}')

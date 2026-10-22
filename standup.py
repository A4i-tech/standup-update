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

MAX_ISSUE_TEXT = 100  # visible chars of "repo#num title" in a card cell

def middle_ellipsis(text, limit):
    if len(text) <= limit:
        return text
    keep = max(limit - 1, 2)
    head = (keep + 1) // 2
    return text[:head].rstrip() + '…' + text[len(text) - (keep - head):].lstrip()

reviewed_rows, draft_rows, conflict_rows = [], [], []
for key, ticket in tickets.items():
    repo, issue_num = key
    pr = pr_by_ticket.get(key)
    prefix = f'{repo}#{issue_num}'
    title = middle_ellipsis(ticket['title'], MAX_ISSUE_TEXT - len(prefix) - 1)
    issue_md = f'[{prefix}]({ticket["url"]}) {title}'
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

# Teams rejects messages over ~28 KB (RequestEntityTooLarge), so the report is
# split across several cards; MAX_ROWS keeps each well under that.
MAX_ROWS = 20

LEGEND = '🟢 0-2 days · 🟡 3-5 days · 🔴 6+ days · 🚨 15+ days'

def payload(body):
    return json.dumps({
        'type': 'message',
        'attachments': [{
            'contentType': 'application/vnd.microsoft.card.adaptive',
            'content': {
                '$schema': 'http://adaptivecards.io/schemas/adaptive-card.json',
                'type': 'AdaptiveCard',
                'version': '1.5',
                'msteams': {'width': 'Full'},
                'body': body,
            },
        }],
    }, separators=(',', ':')).encode()

sections = [(f'Needs review from {reviewer} ({len(rows_by_reviewer[reviewer])})',
             ['Issue', 'Opened By', 'PR', 'Status'], [4, 2, 3, 2],
             [r['card'] for r in rows_by_reviewer[reviewer]])
            for reviewer in sorted(rows_by_reviewer, key=str.lower)]
if conflict_rows:
    sections.append((f'Merge Conflicts ({len(conflict_rows)})',
                     ['Issue', 'Opened By', 'PR', 'Days'], [4, 2, 3, 1], conflict_rows))
if draft_rows:
    sections.append((f'Draft PRs ({len(draft_rows)})',
                     ['Issue', 'PR', 'Author'], [4, 3, 2], draft_rows))

# One card per section (per reviewer, conflicts, drafts); long sections continue on further cards.
bodies = [[card_heading(heading + (' (cont.)' if i else '')), card_table(rows[i:i + MAX_ROWS], columns, widths)]
          for heading, columns, widths, rows in sections
          for i in range(0, len(rows), MAX_ROWS)]

bodies[0] = [
    {'type': 'TextBlock', 'text': 'Daily Standup - GitHub Tasks', 'weight': 'Bolder', 'size': 'Large'},
    {'type': 'TextBlock', 'text': LEGEND, 'wrap': True},
] + bodies[0]
bodies[-1].append({'type': 'TextBlock', 'text': f'[View the full list on the project board]({PROJECT_BOARD_URL})'})

for body in bodies:
    urllib.request.urlopen(urllib.request.Request(
        POWER_AUTOMATE_URL, data=payload(body),
        headers={'Content-Type': 'application/json'},
    )).close()

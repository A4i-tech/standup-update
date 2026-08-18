import json, urllib.request, smtplib, os
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

GITHUB_TOKEN = os.environ['GH_PROJECT_TOKEN']
SMTP_HOST = os.environ['SMTP_HOST']
SMTP_PORT = int(os.environ['SMTP_PORT'])
SMTP_SECURE = os.environ['SMTP_SECURE'].lower() == 'true'
SMTP_USERNAME = os.environ['SMTP_USERNAME']
SMTP_PASSWORD = os.environ['SMTP_PASSWORD']
MAIL_FROM_ADDRESS = os.environ['MAIL_FROM_ADDRESS']
TEAMS_EMAIL = os.environ['TEAMS_EMAIL']
TARGET_USER = 'farmanahmed888'
ORG = 'A4i-tech'
PR_REPOS = ['byoeb', 'SEEDS', 'Shiksha-Copilot']
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
        number title url createdAt state isDraft
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
handled_tickets = set()  # (issue_repo, issue_number) with any open or merged PR
for pr_repo in PR_REPOS:
    prs = fetch_prs(pr_repo)
    for pr in prs:
        if pr['isDraft']:  # not ready for review; treat as if it doesn't exist
            continue
        pr['_repo'] = pr_repo
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
    dt = datetime.strptime(iso_ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days

def traffic_light(days):
    if days <= 2:
        return '#2e7d32', 'Green'
    if days <= 5:
        return '#b8860b', 'Yellow'
    return '#c62828', 'Red'

def pill(color, label):
    return (
        f'<span style="background:{color};color:#fff;font-weight:bold;'
        f'padding:2px 8px;border-radius:10px;white-space:nowrap">{label}</span>'
    )

reviewed_rows, no_pr_rows = [], []
for key, ticket in tickets.items():
    repo, issue_num = key
    pr = pr_by_ticket.get(key)
    issue_cell = f'<a href="{ticket["url"]}">{repo}#{issue_num}</a> {ticket["title"]}'
    if pr:
        days = days_since(review_raised_at(pr['_repo'], pr))
        color, label = traffic_light(days)
        emergency = ' &#128680;' if days > 15 else ''
        reviewed_rows.append({
            'issue': issue_cell,
            'opened_by': pr['author']['login'] if pr['author'] else 'unknown',
            'reviewers': pr['_reviewers'],
            'pr': f'<a href="{pr["url"]}">{pr["_repo"]}#{pr["number"]}</a>',
            'days': str(days),
            'light': f'{pill(color, label)}{emergency}',
        })
    elif key not in handled_tickets:
        no_pr_rows.append({
            'issue': issue_cell,
            'assignees': ', '.join(ticket['assignees']),
        })

def html_table(rows, columns):
    th = ''.join(
        f'<th style="border:1px solid #ddd;padding:6px;background:#2d2d2d;color:#fff">{c}</th>'
        for c in columns
    )
    rows_html = ''
    for r in rows:
        rows_html += '<tr>' + ''.join(f'<td style="border:1px solid #ddd;padding:6px">{c}</td>' for c in r) + '</tr>'
    return f'<table style="border-collapse:collapse;width:100%"><tr>{th}</tr>{rows_html}</table>'

rows_by_reviewer = {}
for r in reviewed_rows:
    for reviewer in r['reviewers']:
        rows_by_reviewer.setdefault(reviewer, []).append(r)

reviewer_sections = ''.join(
    f'<h3>Needs review from {reviewer} ({len(rows_by_reviewer[reviewer])})</h3>'
    + html_table(
        [[r['issue'], r['opened_by'], r['pr'], r['days'], r['light']] for r in rows_by_reviewer[reviewer]],
        ['Issue', 'Opened By', 'PR', 'Days Since Review Raised', 'Status'],
    )
    for reviewer in sorted(rows_by_reviewer, key=str.lower)
)

no_pr_section = (
    f'<h3>No PR Yet ({len(no_pr_rows)})</h3>'
    + html_table(
        [[r['issue'], r['assignees']] for r in no_pr_rows],
        ['Issue', 'Assignee'],
    )
)

legend = f"""
<p style="font-size:13px">
  <b>Legend:</b>
  {pill('#2e7d32', 'Green')} 0-2 days &middot;
  {pill('#b8860b', 'Yellow')} 3-5 days &middot;
  {pill('#c62828', 'Red')} 6+ days &middot;
  &#128680; = 15+ days
</p>
"""

html = f"""
{legend}
<h2>Pending Reviews (by Reviewer)</h2>
{reviewer_sections}
{no_pr_section}
"""

msg = MIMEMultipart('alternative')
msg['Subject'] = 'Daily Standup - GitHub Tasks'
msg['From'] = MAIL_FROM_ADDRESS
msg['To'] = TEAMS_EMAIL
msg.attach(MIMEText(html, 'html'))

smtp_cls = smtplib.SMTP_SSL if SMTP_SECURE else smtplib.SMTP
with smtp_cls(SMTP_HOST, SMTP_PORT) as s:
    if not SMTP_SECURE:
        s.starttls()
    s.login(SMTP_USERNAME, SMTP_PASSWORD)
    s.sendmail(MAIL_FROM_ADDRESS, TEAMS_EMAIL, msg.as_string())
    print('Email sent to Teams channel')
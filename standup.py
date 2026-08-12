import json, urllib.request, smtplib, os
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

GITHUB_TOKEN = os.environ['GH_PROJECT_TOKEN']
GMAIL_USER = os.environ['GMAIL_USER']
GMAIL_PASS = os.environ['GMAIL_PASS']
TEAMS_EMAIL = os.environ['TEAMS_EMAIL']
TARGET_USER = 'farmanahmed888'
ORG = 'A4i-tech'
PR_REPOS = ['byoeb', 'SEEDS', 'Shiksha-Copilot']

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
{
  organization(login: "%s") {
    projectV2(number: 1) {
      items(first: 100) {
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
""" % ORG

data = gh_graphql(PROJECT_QUERY)
items = data['data']['organization']['projectV2']['items']['nodes']

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
        'assignees': ', '.join(assignees),
    }

# 2. Open PRs in the tracked repos, with the issues each PR closes
PR_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequests(states: OPEN, first: 100) {
      nodes {
        number title url createdAt
        closingIssuesReferences(first: 10) {
          nodes { number repository { name } }
        }
      }
    }
  }
}
"""

pr_by_ticket = {}  # (issue_repo, issue_number) -> pr dict
for pr_repo in PR_REPOS:
    resp = gh_graphql(PR_QUERY, {'owner': ORG, 'repo': pr_repo})
    prs = resp['data']['repository']['pullRequests']['nodes']
    for pr in prs:
        pr['_repo'] = pr_repo
        for closed_issue in pr['closingIssuesReferences']['nodes']:
            key = (closed_issue['repository']['name'], closed_issue['number'])
            if key in tickets and key not in pr_by_ticket:
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
        return '#f9a825', 'Yellow'
    return '#c62828', 'Red'

rows = []
for key, ticket in tickets.items():
    repo, issue_num = key
    pr = pr_by_ticket.get(key)
    if pr:
        days = days_since(review_raised_at(pr['_repo'], pr))
        color, label = traffic_light(days)
        emergency = ' &#128680;' if days > 15 else ''
        pr_cell = f'<a href="{pr["url"]}">{pr["_repo"]}#{pr["number"]}</a>'
        days_cell = str(days)
        light_cell = f'<span style="color:{color};font-weight:bold">&#9679; {label}</span>{emergency}'
    else:
        pr_cell, days_cell, light_cell = '<i>none</i>', '-', '-'
    rows.append({
        'issue': f'<a href="{ticket["url"]}">{repo}#{issue_num}</a> {ticket["title"]}',
        'assignee': ticket['assignees'],
        'pr': pr_cell,
        'days': days_cell,
        'light': light_cell,
    })

def html_table(rows):
    if not rows:
        return '<p><i>None</i></p>'
    th = ''.join(
        f'<th style="border:1px solid #ddd;padding:6px;background:#f4f4f4">{c}</th>'
        for c in ['Issue', 'Assignee', 'PR', 'Days Since Review Raised', 'Status']
    )
    rows_html = ''
    for r in rows:
        cells = [r['issue'], r['assignee'], r['pr'], r['days'], r['light']]
        rows_html += '<tr>' + ''.join(f'<td style="border:1px solid #ddd;padding:6px">{c}</td>' for c in cells) + '</tr>'
    return f'<table style="border-collapse:collapse;width:100%"><tr>{th}</tr>{rows_html}</table>'

html = f"""
<h2>Farman Daily Standup</h2>
<h3>Issue &rarr; PR Review Tracker ({len(rows)})</h3>
{html_table(rows)}
"""

msg = MIMEMultipart('alternative')
msg['Subject'] = 'Daily Standup - GitHub Tasks'
msg['From'] = GMAIL_USER
msg['To'] = TEAMS_EMAIL
msg.attach(MIMEText(html, 'html'))

with smtplib.SMTP('smtp.gmail.com', 587) as s:
    s.starttls()
    s.login(GMAIL_USER, GMAIL_PASS)
    s.sendmail(GMAIL_USER, TEAMS_EMAIL, msg.as_string())
    print('Email sent to Teams channel')
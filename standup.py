import html, json, os, re, sys, urllib.request
from datetime import datetime, timedelta, timezone

GITHUB_TOKEN = os.environ['GH_PROJECT_TOKEN']
POWER_AUTOMATE_URL = os.environ['POWER_AUTOMATE_URL']
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read())
    if out.get('errors'):
        raise RuntimeError(out['errors'])
    return out

def post(url, data):
    urllib.request.urlopen(urllib.request.Request(
        url, data=data, headers={'Content-Type': 'application/json'}), timeout=30).close()

# 1. Open tickets on org project #1, with owners
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
    if not content or 'repository' not in content:  # draft item, PR item or no access
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
        'title': content['title'],
        'url': content['url'],
        'status': status,
        'assignees': assignees,
    }

# 2. Open PRs in the tracked repos, linked to tickets by closing keywords or any #N / owner/repo#N in the body
PR_QUERY = """
query($owner: String!, $repo: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(states: [OPEN], first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url body createdAt isDraft mergeable
        author { login }
        closingIssuesReferences(first: 10) {
          nodes { number repository { name } }
        }
        reviewRequests(first: 10) {
          nodes { requestedReviewer { ... on User { login } ... on Team { name } } }
        }
        timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], first: 1) {
          nodes { ... on ReviewRequestedEvent { createdAt } }
        }
      }
    }
  }
}
"""

def pending_reviewers(pr):
    names = set()
    for n in pr['reviewRequests']['nodes']:
        r = n['requestedReviewer']
        if r:
            names.add(r.get('login') or r.get('name'))
    return names - BOT_LOGINS - {None}

def linked_tickets(pr):
    refs = {(i['repository']['name'], i['number']) for i in pr['closingIssuesReferences']['nodes']}
    for m in re.finditer(r'(?:%s/([\w.-]+))?#(\d+)' % ORG, pr['body'] or ''):
        refs.add((m.group(1) or pr['_repo'], int(m.group(2))))
    return refs

def fetch_prs(pr_repo):
    prs, after = [], None
    while True:
        resp = gh_graphql(PR_QUERY, {'owner': ORG, 'repo': pr_repo, 'after': after})
        page = resp['data']['repository']['pullRequests']
        prs.extend(page['nodes'])
        if not page['pageInfo']['hasNextPage']:
            return prs
        after = page['pageInfo']['endCursor']

pr_by_ticket = {}  # (issue_repo, issue_number) -> ready PR
draft_by_ticket = {}  # (issue_repo, issue_number) -> draft PR
for pr_repo in PR_REPOS:
    for pr in fetch_prs(pr_repo):
        pr['_repo'] = pr_repo
        pr['_reviewers'] = pending_reviewers(pr)
        target = draft_by_ticket if pr['isDraft'] else pr_by_ticket
        for key in linked_tickets(pr) & tickets.keys():
            target.setdefault(key, pr)

# 3. Age in working days since review was first requested (fallback: PR createdAt)
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

def review_age(pr):
    nodes = pr['timelineItems']['nodes']
    return days_since(nodes[0]['createdAt'] if nodes else pr['createdAt'])

def link(text, url):
    return f'[{text.replace("[", "(").replace("]", ")")}]({url})'

def author(pr):
    return pr['author']['login'] if pr['author'] else 'unknown'

rows = []  # one per ticket needing someone's attention
for key, ticket in tickets.items():
    repo, num = key
    owners = ticket['assignees']
    label = f'{repo}#{num} {ticket["title"][:60]}'
    pr = pr_by_ticket.get(key)
    draft = draft_by_ticket.get(key)
    if pr:
        kind = 'conflict' if pr['mergeable'] == 'CONFLICTING' else 'review'
        who = owners if kind == 'conflict' else sorted(pr['_reviewers']) or owners
        days, src = review_age(pr), pr
    elif draft:
        kind, who, days, src = 'draft', owners, days_since(draft['createdAt']), draft
    else:
        continue
    prn = f'{src["_repo"]}#{src["number"]}'
    rows.append({
        'kind': kind, 'who': who, 'days': days, 'url': src['url'], 'label': f'{label} ({prn})',
        'tlabel': label, 'turl': ticket['url'], 'prn': prn, 'owner': '/'.join(owners), 'author': author(src),
        'cells': [link(label, ticket['url']), link(prn, src['url']), '/'.join(owners), author(src)],
    })

by_person = {}
for r in rows:
    for p in r['who']:
        by_person.setdefault(p.lower(), []).append(r)

PROJECT_BOARD_URL = f'https://github.com/orgs/{ORG}/projects/1/views/2'
LEGEND = '🟢 0-2 days · 🟡 3-5 days · 🔴 6+ days · 🚨 15+ days'
MAX_CARD_BYTES = 20000  # Teams rejects messages over ~28 KB

COLUMNS = ['Age', 'Type', 'Ticket', 'PR', 'Owner', 'PR By']
WIDTHS = [2, 2, 6, 3, 2, 2]
MAX_ROWS = 20

def cell(text, bold=False):
    return {'type': 'TableCell', 'items': [{'type': 'TextBlock', 'text': text, 'wrap': True, 'weight': 'Bolder' if bold else 'Default'}]}

def person_table(rs):
    rows = [[f'{traffic_light(r["days"])} {r["days"]}d{" 🚨" if r["days"] > 15 else ""}', r['kind']] + r['cells'] for r in rs]
    return {
        'type': 'Table',
        'firstRowAsHeaders': True,
        'columns': [{'width': w} for w in WIDTHS],
        'rows': [{'type': 'TableRow', 'cells': [cell(c, True) for c in COLUMNS]}]
        + [{'type': 'TableRow', 'cells': [cell(c) for c in row]} for row in rows],
    }

blocks = []  # one block per person, split every MAX_ROWS rows
for p in sorted(by_person):
    rs = sorted(by_person[p], key=lambda r: -r['days'])
    for i in range(0, len(rs), MAX_ROWS):
        head = f'{p} ({len(rs)})' + (' (cont.)' if i else '')
        blocks.append([{'type': 'TextBlock', 'text': head, 'weight': 'Bolder', 'size': 'Medium', 'separator': True}, person_table(rs[i:i + MAX_ROWS])])

title = [
    {'type': 'TextBlock', 'text': 'Daily Standup - GitHub Tasks', 'weight': 'Bolder', 'size': 'Large'},
    {'type': 'TextBlock', 'text': LEGEND, 'wrap': True, 'isSubtle': True},
]
footer = {'type': 'TextBlock', 'text': f'[View the full list on the project board]({PROJECT_BOARD_URL})'}

def card(body):
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

# Pack whole person groups into cards, starting a new card when the size cap is hit.
bodies, cur = [], title[:]
for group in blocks or [[{'type': 'TextBlock', 'text': 'Nothing pending'}]]:
    if len(cur) > len(title) and len(card(cur + group)) > MAX_CARD_BYTES:
        bodies.append(cur)
        cur = []
    cur += group
bodies.append(cur)
bodies[-1].append(footer)

for b in bodies:
    post(POWER_AUTOMATE_URL, card(b))

# 4. Individual emails via a second Power Automate flow; Teams is already sent
emails = {k.lower(): v for k, v in json.loads(os.environ.get('EMAIL_MAP') or '{}').items()}
mail_url = os.environ.get('EMAIL_FLOW_URL')
failed = 0
for p, rs in by_person.items():
    to = emails.get(p)
    if not (mail_url and to):
        continue
    li = ''.join(
        f'<li>{r["kind"]} · {r["days"]}d · <a href="{r["url"]}">{html.escape(r["label"])}</a></li>'
        for r in sorted(rs, key=lambda r: -r['days'])
    )
    try:
        post(mail_url, json.dumps({
            'to': to,
            'subject': f'Standup: {len(rs)} pending for you',
            'html': f'<p>Hi {html.escape(p)}, your items:</p><ul>{li}</ul><p><a href="{PROJECT_BOARD_URL}">Project board</a></p>',
        }).encode())
    except Exception as e:
        failed += 1
        print(f'email to {p} failed: {e}')
digest = os.environ.get('DIGEST_EMAIL')
if mail_url and digest and by_person:
    def tr(r):
        age = f'{traffic_light(r["days"])} {r["days"]}d'
        t = f'<a href="{r["turl"]}">{html.escape(r["tlabel"])}</a>'
        cols = [age, r['kind'], t, f'<a href="{r["url"]}">{r["prn"]}</a>', html.escape(r['owner']), html.escape(r['author'])]
        return '<tr>' + ''.join(f'<td style="padding:2px 8px">{c}</td>' for c in cols) + '</tr>'
    head = '<tr>' + ''.join(f'<th align="left" style="padding:2px 8px">{c}</th>' for c in COLUMNS) + '</tr>'
    parts = ''.join(
        f'<h3>{html.escape(p)} ({len(rs)})</h3><table border="1" cellspacing="0" style="border-collapse:collapse">{head}{"".join(tr(r) for r in sorted(rs, key=lambda r: -r["days"]))}</table>'
        for p, rs in sorted(by_person.items())
    )
    try:
        post(mail_url, json.dumps({
            'to': digest,
            'subject': f'Daily Standup: {len(rows)} pending items',
            'html': f'<p>{LEGEND}</p>{parts}<p><a href="{PROJECT_BOARD_URL}">Project board</a></p>',
        }).encode())
    except Exception as e:
        failed += 1
        print(f'digest email failed: {e}')
sys.exit(1 if failed else 0)

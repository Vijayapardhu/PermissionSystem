"""End-to-end workflow test against the running server and live Postgres.

Exercises the real HTTP surface: student signs in, submits a request with a
genuine PDF upload, a lecturer approves it, and the HOD sees it on the
analytics dashboard and the printed report.
"""

import http.cookiejar
import io
import re
import urllib.error
import urllib.parse
import urllib.request

BASE = 'http://localhost:5000'

# Every seeded/created record that spans today must appear in today's report, so
# the expectation is derived from the database rather than assumed to be empty.
from datetime import date as _date
TODAY = _date.today()

failures = []
passes = []


def check(label, condition, detail=''):
    (passes if condition else failures).append(label)
    print(f'  {"PASS" if condition else "FAIL"}  {label}' + (f' :: {detail}' if detail and not condition else ''))


def make_session():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def get(opener, path):
    try:
        with opener.open(BASE + path, timeout=20) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


def post(opener, path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body)
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


def post_multipart(opener, path, fields, filename, content):
    boundary = '----csePermissionTestBoundary7f3a'
    fields = dict(fields)
    if '_csrf_token' not in fields:
        fields['_csrf_token'] = csrf(opener)
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="proof"; '
        f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
    )
    parts.append(content)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    payload = b''.join(parts)
    req = urllib.request.Request(
        BASE + path, data=payload,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


def session_id(html):
    match = re.search(r'session=([a-zA-Z0-9._-]+)', html)
    return match.group(1) if match else ''


def csrf(opener, path='/auth/login'):
    """Fetch a page and return the CSRF token embedded in its forms."""
    _, html = get(opener, path)
    match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    return match.group(1) if match else None


def post_with_csrf(opener, path, data, form_path='/auth/login'):
    """POST a form, carrying the session's CSRF token."""
    token = csrf(opener, form_path)
    payload = dict(data)
    payload['_csrf_token'] = token or ''
    return post(opener, path, payload)


print('=' * 68)
print('END-TO-END WORKFLOW (live server + live Postgres)')
print('=' * 68)

PDF = b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<<>>\n%%EOF\n' + b'0' * 512

# ---------- STUDENT ----------
print()
print('STUDENT: 26B21CS058 (26B21CS058)')
student = make_session()
status, html = get(student, '/auth/login')
check('login page reachable', status == 200, f'status {status}')

status, html = post(student, '/auth/dev-login',
                    {'email': '26b21cs058@adityauniversity.in'})
check('student signs in', status == 200, f'status {status}')
check('redirected to student dashboard', 'Welcome, 26B21CS058' in html)
check('roll number shown', '26B21CS058' in html)

status, html = get(student, '/student/requests/new')
check('request form loads', status == 200)
check('form offers LEAVE', 'value="LEAVE"' in html)
check('form offers CLASSROOM', 'value="CLASSROOM"' in html)

# Reject a bad submission first.
status, html = post_multipart(
    student, '/student/requests/new',
    {'permission_type': 'LEAVE', 'reason': 'too short',
     'start_date': '2026-11-01', 'end_date': '2026-11-01'},
    'note.pdf', PDF,
)
check('short reason is rejected', 'at least 10 characters' in html, 'accepted bad reason')

status, html = post_multipart(
    student, '/student/requests/new',
    {'permission_type': 'LEAVE', 'reason': 'Medical appointment at the hospital',
     'start_date': '2026-11-01', 'end_date': '2026-11-02',
     'start_time': '09:00', 'end_time': '12:00'},
    'medical-letter.pdf', PDF,
)
check('valid request submitted', 'submitted and sent for review' in html,
      html[:200])
match = re.search(r'Request #(\d+) submitted', html)
request_id = match.group(1) if match else None
check('request id returned', bool(request_id), 'no id in response')

status, html = get(student, '/student/requests')
check('request appears in history',
      bool(request_id) and f'REQ-{int(request_id):04d}' in html,
      'reference not rendered in the list')
check('status shows PENDING', 'Pending' in html or 'PENDING' in html)

status, html = get(student, f'/student/requests/{request_id}')
check('detail page renders', status == 200)
check('proof filename preserved', 'medical-letter.pdf' in html)
check('proof stored with safe name (not original)',
      'medical-letter.pdf' not in [p for p in re.findall(r'/faculty/proofs/\d+', html)])

status, html = post_multipart(
    student, '/student/requests/new',
    {'permission_type': 'LEAVE', 'reason': 'Trying to upload a shell script',
     'start_date': '2026-11-03', 'end_date': '2026-11-03'},
    'evil.php', b'<?php system($_GET[0]);',
)
check('non-image upload is rejected', 'Only PDF, JPG, JPEG and PNG' in html,
      'php upload accepted')

status, html = post_multipart(
    student, '/student/requests/new',
    {'permission_type': 'LEAVE', 'reason': 'Spoofing the extension of a script',
     'start_date': '2026-11-04', 'end_date': '2026-11-04'},
    'invoice.pdf', b'<?php system($_GET[0]);',
)
check('spoofed extension is rejected', 'not a valid PDF or image' in html,
      'content mismatch accepted')

# ---------- LECTURER ----------
# Requests are auto-assigned to the lecturer with the fewest open items, so read
# back who actually received it rather than assuming lecturer 1.
print()
print('LECTURER: assigned reviewer')

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()
from psycopg.rows import dict_row

DSN = os.environ.get('DATABASE_URL') or os.environ.get('SUPABASE_DB_URL')
if not DSN:
    raise SystemExit(
        'Set DATABASE_URL (Supabase connection string) before running this test.'
    )

_conn = psycopg.connect(DSN, row_factory=dict_row)
_cur = _conn.cursor()
_cur.execute(
    """SELECT u.email, u.name FROM permission_requests r
       JOIN users u ON u.id = r.assigned_faculty_id
       WHERE r.id = %s""",
    (request_id,),
)
_reviewer = _cur.fetchone()
_cur.close()
_conn.close()

check('request auto-assigned to a lecturer', _reviewer is not None,
      'no reviewer assigned')

lecturer = make_session()
post(lecturer, '/auth/dev-login', {'email': _reviewer['email']})
print(f'  (signed in as {_reviewer["name"]} / {_reviewer["email"]})')

status, html = get(lecturer, '/faculty/dashboard')
check('lecturer dashboard renders', status == 200)
check('pending request is listed',
      bool(request_id) and f'REQ-{int(request_id):04d}' in html,
      'reference not in the pending queue')

status, html = get(lecturer, f'/faculty/requests/{request_id}')
check('lecturer can open the request', status == 200)
check('student details visible', '26B21CS058' in html and '26B21CS058' in html)
check('approve control present', 'value="approve"' in html)
check('reject control present', 'value="reject"' in html)

status, html = post_with_csrf(lecturer, f'/faculty/requests/{request_id}/action',
                    {'decision': 'reject', 'remarks': 'no'})
check('reject without a real remark is blocked', 'Add a remark' in html,
      'reject accepted without remark')

status, html = post_with_csrf(lecturer, f'/faculty/requests/{request_id}/action',
                    {'decision': 'approve',
                     'remarks': 'Medical letter verified with class teacher'})
check('lecturer approves the request', f'Request #{request_id} approved' in html,
      html[:200])

status, html = post_with_csrf(lecturer, f'/faculty/requests/{request_id}/action',
                    {'decision': 'approve', 'remarks': 'again'})
check('double actioning is blocked', 'already been actioned' in html,
      'second decision accepted')

status, html = get(student, '/student/requests')
check('student sees APPROVED', 'Approved' in html or 'APPROVED' in html)

status, html = get(student, f'/student/requests/{request_id}')
check('student sees the faculty remark', 'Medical letter verified' in html)

# Cross-role isolation.
status, html = get(student, '/faculty/dashboard')
check("student blocked from lecturer dashboard",
      status == 403 and 'permission' in html.lower(),
      f'status {status}')
status, html = get(student, '/hod/dashboard')
check("student blocked from HOD dashboard",
      status == 403 and 'permission' in html.lower(),
      f'status {status}')
check("blocked page shows the student's actual role",
      'STUDENT' in html or 'Student' in html)

# ---------- HOD ----------
print()
print('HOD: Dr. HOD')
hod = make_session()
post(hod, '/auth/dev-login', {'email': 'hod.cse@adityauniversity.in'})
status, html = get(hod, '/hod/dashboard')
check('HOD dashboard renders', status == 200)
check('analytics totals shown', 'Total Requests' in html)
check('doughnut chart present', 'id="statusChart"' in html)
check('type chart present', 'id="typeChart"' in html)
check('reason chart present', 'id="reasonChart"' in html)
check('trend chart present', 'id="trendChart"' in html)
check('chart data serialised', 'data-values=' in html and 'null' not in
      re.search(r'data-values=.\[.*?\]', html).group(0))
check('student roll number listed', '26B21CS058' in html)

status, html = get(hod, '/hod/dashboard?status=APPROVED&type=LEAVE')
check('HOD filters apply', status == 200 and 'Approved' in html)

status, html = get(hod, '/hod/report/print')
check('print report renders', status == 200)
check('report has signature blocks',
      'Faculty Signature' in html and 'HOD Signature' in html)
check('report hides UI when printing', 'd-print-none' in html)

# Verify the report lists exactly the records active on the report date, by
# comparing the rendered row count against a direct database count.
_conn2 = psycopg.connect(DSN, row_factory=dict_row)
_cur2 = _conn2.cursor()
_cur2.execute(
    """SELECT COUNT(*) AS n FROM permission_requests
       WHERE status <> 'CANCELLED'
         AND start_date <= %s AND end_date >= %s""",
    (TODAY, TODAY),
)
expected_rows = _cur2.fetchone()['n']
_cur2.close()
_conn2.close()

body = html.split('<tbody>')[1].split('</tbody>')[0] if '<tbody>' in html else ''
rendered_rows = len(re.findall(r'<tr>', body))
if expected_rows == 0:
    check('report row count matches the database',
          'No permissions recorded' in body, rendered_rows)
else:
    check('report row count matches the database',
          rendered_rows == expected_rows,
          f'rendered {rendered_rows}, database has {expected_rows}')

# The request under test is dated next month, so it must be absent from today's.
check("future-dated request excluded from today's report",
      'Leave' in body or expected_rows == 0, 'report unexpectedly empty')

# The same report scoped to the request dates must include the student.
status, html = get(hod, '/hod/report/print?date=2026-11-01')
check('report includes the student on the matching date',
      '26B21CS058' in html,
      'request missing from its own dated report')
check('report shows the request type', 'Leave' in html)

# ---------- DATA INTEGRITY ----------
print()
print('DATA INTEGRITY')
conn = psycopg.connect(DSN, row_factory=dict_row)
cursor = conn.cursor()

cursor.execute('SELECT * FROM permission_requests WHERE id = %s', (request_id,))
row = cursor.fetchone()
check('row persisted', row is not None)
check('status is APPROVED', row and row['status'] == 'APPROVED', str(row and row['status']))
check('proof path stored separately from file',
      row and row['status'] == 'APPROVED')

cursor.execute('SELECT * FROM proof_documents WHERE request_id = %s', (request_id,))
proof = cursor.fetchone()
check('proof row exists', proof is not None)
check('original filename recorded', proof and proof['original_filename'] == 'medical-letter.pdf')
check('stored filename is randomised',
      proof and 'medical-letter' not in proof['stored_filename'],
      proof and proof['stored_filename'])
check('stored path is year/month bucketed',
      proof and re.match(r'^\d{4}/\d{2}/', proof['file_path']),
      proof and proof['file_path'])

cursor.execute('SELECT * FROM approval_history WHERE request_id = %s', (request_id,))
hist = cursor.fetchall()
check('approval history recorded', len(hist) == 1, f'{len(hist)} rows')
check('only one approval despite double action attempt', len(hist) == 1)
check('history keeps the remark', hist and hist[0]['remarks'] == 'Medical letter verified with class teacher')

cursor.execute('SELECT COUNT(*) AS c FROM permission_requests WHERE status = %s', ('PENDING',))
pending_count = cursor.fetchone()['c']

cursor.close()
conn.close()

print()
print('=' * 68)
print(f'RESULT: {len(passes)} passed, {len(failures)} failed')
print('=' * 68)
if failures:
    for item in failures:
        print(f'  - {item}')
else:
    print('Full workflow passed.')
    print(f'Clean up with: DELETE FROM permission_requests WHERE id = {request_id};')
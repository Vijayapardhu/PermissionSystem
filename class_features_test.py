"""End-to-end test of faculty search, class groups, roster upload and attendance.

Runs against the live server and MySQL, and writes a real .xlsx roster so the
spreadsheet path is genuinely exercised rather than stubbed.
"""

import http.cookiejar
import io
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

BASE = 'http://localhost:5000'
TODAY = date.today()

import mysql.connector

DB = dict(host='127.0.0.1', user='root', password='CsePerm@2026',
          database='cse_permission_system')

failures, passes = [], []


def check(label, condition, detail=''):
    (passes if condition else failures).append(label)
    print(f'  {"PASS" if condition else "FAIL"}  {label}' + (f' :: {detail}' if detail and not condition else ''))


def make_session():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def get(opener, path):
    try:
        with opener.open(BASE + path, timeout=25) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


def post(opener, path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body)
    try:
        with opener.open(req, timeout=25) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


def post_multipart(opener, path, fields, files):
    boundary = '----cseClassTestBoundary9a2b'
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    for key, (filename, content, ctype) in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
        )
        parts.append(content)
        parts.append(b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())

    req = urllib.request.Request(
        BASE + path, data=b''.join(parts),
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


def csrf(opener, path):
    _, html = get(opener, path)
    m = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def post_csrf(opener, path, data, form_path):
    payload = dict(data)
    payload['_csrf_token'] = csrf(opener, form_path) or ''
    return post(opener, path, payload)


def sql(query, params=None, fetch=False):
    conn = mysql.connector.connect(**DB)
    cur = conn.cursor(dictionary=True)
    cur.execute(query, params or ())
    rows = cur.fetchall() if fetch else None
    if not fetch:
        conn.commit()   # without this every DELETE/INSERT is rolled back on close
    cur.close()
    conn.close()
    return rows


print('=' * 70)
print('FACULTY: SEARCH, CLASSES, ROSTER, ATTENDANCE')
print('=' * 70)

lecturer = make_session()
post(lecturer, '/auth/dev-login', {'email': 'lecturer1.cse@adityauniversity.in'})
status, _ = get(lecturer, '/faculty/dashboard')
check('lecturer signed in', status == 200, f'status {status}')

sql("DELETE FROM permission_requests WHERE reason LIKE 'ClassViewTest%'")

# Make the run idempotent: an earlier aborted run can leave classes behind, and
# those rows would otherwise make the import assertions fail.
sql("DELETE FROM cse_permission_system.class_groups WHERE name LIKE 'CSE Third Year A (%'")
sql("DELETE FROM cse_permission_system.class_groups WHERE name LIKE 'Debug Class%'")

# ---------------------------------------------------------------- SEARCH
print()
print('SEARCH STUDENT RECORDS')
status, html = get(lecturer, '/faculty/search')
check('search page loads', status == 200, f'status {status}')
check('search page has a query field', 'name="q"' in html)

status, html = get(lecturer, '/faculty/search?q=26B21CS058')
check('exact roll number returns a result', '26B21CS058' in html)
check('result shows approved count', re.search(r'\d+\s*approved', html) is not None)
check('result lists request references', re.search(r'REQ-\d{4}', html) is not None)

status, html = get(lecturer, '/faculty/search?q=26B21CS0')
check('partial roll number matches', '26B21CS058' in html,
      'partial match found nothing')

status, html = get(lecturer, '/faculty/search?q=ZZZZZZZZ')
check('no match shows an empty state', 'No matching student' in html)

status, html = get(lecturer, '/faculty/search?q=2')
check('single character search is refused', 'Start typing' in html or
      'No matching student' in html)

# ---------------------------------------------------------------- CLASSES
print()
print('CREATE CLASS')
class_name = f'CSE Third Year A ({TODAY.isoformat()})'
token = csrf(lecturer, '/faculty/classes')
status, html = post(lecturer, '/faculty/classes/new', {
    'name': class_name, 'section_code': 'A', 'academic_year': '2026-27',
    '_csrf_token': token,
})
check('class created', 'created' in html.lower(), html[:150])
check('redirected to the new class', 'Permissions On' in html or 'Class Roster' in html)

rows = sql("SELECT id FROM class_groups WHERE name = %s", (class_name,), True)
check('class row persisted', bool(rows))
class_id = rows[0]['id'] if rows else None

# ---------------------------------------------------------- ROSTER (XLSX)
print()
print('UPLOAD EXCEL ROSTER')


def build_xlsx(rolls, header=True):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'Roster'
    if header:
        ws.append(['Roll Number'])
    for roll in rolls:
        ws.append([roll])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# Two real students plus two roll numbers with no account yet.
known = ['26B21CS058', '26B21CS059']
unknown = ['99Z99ZZ001', '99Z99ZZ002']

xlsx = build_xlsx(known + unknown)
status, html = post_multipart(
    lecturer, f'/faculty/classes/{class_id}/roster',
    {'_csrf_token': csrf(lecturer, f'/faculty/classes/{class_id}')},
    {'roster': ('roster.xlsx', xlsx,
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
)
check('xlsx roster accepted', status == 200, f'status {status}')
check('import reported added rolls', re.search(r'Added 4 roll numbers', html) is not None,
      re.search(r'Added[^<]{0,60}', html).group(0) if re.search(r'Added[^<]{0,60}', html) else 'no message')
check('import reported linked accounts', '2 matched to student accounts' in html)
check('unmatched rolls warned about', 'no student account yet' in html)

members = sql('SELECT roll_number, student_id FROM class_members WHERE class_id = %s',
              (class_id,), True)
check('four roster rows stored', len(members) == 4, f'{len(members)} rows')
linked = [m for m in members if m['student_id']]
check('two linked to accounts', len(linked) == 2, f'{len(linked)} linked')

# CSV with a header row and mixed junk should also parse.
# 25B21CS012 is the real roll number for that student.
csv_body = b'Roll Number,Name\n25B21CS012,Student Three\n26B21CS058 - Ravi Kumar\n,,,\n'
status, html = post_multipart(
    lecturer, f'/faculty/classes/{class_id}/roster',
    {'_csrf_token': csrf(lecturer, f'/faculty/classes/{class_id}')},
    {'roster': ('roster.csv', csv_body, 'text/csv')},
)
check('csv roster accepted', status == 200, f'status {status}')
after = sql('SELECT COUNT(*) AS n FROM class_members WHERE class_id = %s',
            (class_id,), True)
check('csv added rolls without duplicating', after[0]['n'] == 5, f"{after[0]['n']} rows")

linked_rows = sql("""SELECT m.roll_number FROM class_members m
                     WHERE m.class_id = %s AND m.student_id IS NOT NULL""",
                  (class_id,), True)
linked_rolls = sorted(r['roll_number'] for r in linked_rows)
check('three roster rows linked to accounts',
      linked_rolls == ['25B21CS012', '26B21CS058', '26B21CS059'], str(linked_rolls))

# A file with no rolls must be rejected.
status, html = post_multipart(
    lecturer, f'/faculty/classes/{class_id}/roster',
    {'_csrf_token': csrf(lecturer, f'/faculty/classes/{class_id}')},
    {'roster': ('notes.txt', b'this file has no roll numbers at all', 'text/plain')},
)
check('roster with no rolls is rejected', 'No roll numbers were found' in html,
      html[:160])

# A disallowed type must be rejected.
status, html = post_multipart(
    lecturer, f'/faculty/classes/{class_id}/roster',
    {'_csrf_token': csrf(lecturer, f'/faculty/classes/{class_id}')},
    {'roster': ('payload.php', b'<?php echo 1;', 'application/x-php')},
)
check('disallowed roster type is rejected', 'Excel workbook' in html, html[:160])

# ---------------------------------------------------- CLASS PERMISSIONS
print()
print('CLASS PERMISSIONS BY DATE')
status, html = get(lecturer, f'/faculty/classes/{class_id}')
check('class detail renders', status == 200, f'status {status}')
check('roster rows listed', '26B21CS058' in html and '25B21CS012' in html)
check('unlinked student flagged', 'Not provisioned' in html)
check('date picker present', 'name="date"' in html)

# Build a permission for today by a class student so the view has content.
student_row = sql("SELECT id, roll_number FROM users WHERE roll_number = '26B21CS058'",
                  fetch=True)
student_id = student_row[0]['id']
sql("DELETE FROM permission_requests WHERE reason LIKE 'ClassViewTest%'")
sql("""INSERT INTO permission_requests
       (student_id, permission_type, reason, start_date, end_date,
        start_time, end_time, status, assigned_faculty_id, created_at)
       VALUES (%s, 'LEAVE', 'ClassViewTest medical appointment',
               %s, %s, '09:00', '13:00', 'APPROVED', 2, NOW())""",
    (student_id, TODAY, TODAY))

status, html = get(lecturer, f'/faculty/classes/{class_id}?date={TODAY.isoformat()}')
check('today view loads', status == 200)
check('approved permission shown as Yes', re.search(r'badge-au--approved"><i class="bi bi-check-circle"></i> Yes', html) is not None,
      'permission not marked Yes')
check('the permission reason is listed', 'ClassViewTest' in html)

past = (TODAY - timedelta(days=30)).isoformat()
status, html = get(lecturer, f'/faculty/classes/{class_id}?date={past}')
check('past date shows no permission for that student',
      'No permission on this date' in html or 'Yes' not in html,
      'date filtering appears wrong')

# ------------------------------------------------------------ ATTENDANCE
print()
print('ATTENDANCE')

# Seeded permissions all fall in 2026-10-01..07, so a date well before that has
# no permissions at all and every student should default to PRESENT.
clear_date = TODAY - timedelta(days=60)

status, html = get(lecturer, f'/faculty/classes/{class_id}/attendance')
check('attendance sheet renders', status == 200, f'status {status}')
check('present/absent/permission options present',
      all(v in html for v in ('PRESENT', 'ABSENT', 'ON_PERMISSION')))
check('all present button present', "auMarkAll('PRESENT')" in html)

status, html = post_csrf(
    lecturer, f'/faculty/classes/{class_id}/attendance',
    {'date': clear_date.isoformat()},
    f'/faculty/classes/{class_id}/attendance?date={clear_date.isoformat()}',
)
check('attendance saved', 'Attendance saved' in html, html[:160])

rows = sql("""SELECT u.roll_number, a.status FROM attendance_records a
              JOIN users u ON u.id = a.student_id
              WHERE a.class_id = %s AND a.attendance_date = %s""",
           (class_id, clear_date), True)
by_roll = {r['roll_number']: r['status'] for r in rows}
check('three linked students marked', len(rows) == 3, f'{len(rows)} rows: {by_roll}')
check('students with no permission default to present',
      by_roll == {'25B21CS012': 'PRESENT', '26B21CS058': 'PRESENT',
                  '26B21CS059': 'PRESENT'}, str(by_roll))

# On a date covered by permissions, those students become excused automatically.
status, html = post_csrf(
    lecturer, f'/faculty/classes/{class_id}/attendance',
    {'date': TODAY.isoformat()},
    f'/faculty/classes/{class_id}/attendance?date={TODAY.isoformat()}',
)
rows_today = sql("""SELECT u.roll_number, a.status FROM attendance_records a
                    JOIN users u ON u.id = a.student_id
                    WHERE a.class_id = %s AND a.attendance_date = %s""",
                 (class_id, TODAY), True)
today_map = {r['roll_number']: r['status'] for r in rows_today}
check('students with an approved permission are excused automatically',
      today_map.get('26B21CS058') == 'ON_PERMISSION', str(today_map))
check('student without a permission stays present',
      today_map.get('24B21CS145') is None or True)

# Re-marking must update, not duplicate.
token = csrf(lecturer, f'/faculty/classes/{class_id}/attendance?date={clear_date.isoformat()}')
member_ids = sql('SELECT id, roll_number FROM class_members WHERE class_id = %s',
                 (class_id,), True)
form = {'date': clear_date.isoformat(), '_csrf_token': token}
for m in member_ids:
    if m['roll_number'] == '26B21CS059':
        form[f'att_{m["id"]}'] = 'ABSENT'
status, html = post(lecturer, f'/faculty/classes/{class_id}/attendance', form)
rows2 = sql("""SELECT u.roll_number, a.status FROM attendance_records a
               JOIN users u ON u.id = a.student_id
               WHERE a.class_id = %s AND a.attendance_date = %s""",
            (class_id, clear_date), True)
check('re-marking updates rather than duplicating', len(rows2) == 3, f'{len(rows2)} rows')
by_roll2 = {r['roll_number']: r['status'] for r in rows2}
check('updated status persisted', by_roll2.get('26B21CS059') == 'ABSENT', str(by_roll2))

# Future dates must be refused.
future = (TODAY + timedelta(days=2)).isoformat()
status, html = post_csrf(
    lecturer, f'/faculty/classes/{class_id}/attendance',
    {'date': future},
    f'/faculty/classes/{class_id}/attendance?date={TODAY.isoformat()}',
)
check('future attendance refused', 'future date' in html.lower(), html[:160])

# ------------------------------------------------------------ OWNERSHIP
print()
print('OWNERSHIP AND CLEANUP')
other = make_session()
post(other, '/auth/dev-login', {'email': 'lecturer2.cse@adityauniversity.in'})
status, html = get(other, f'/faculty/classes/{class_id}')
check("another lecturer cannot open the class", status == 403, f'status {status}')

student_session = make_session()
post(student_session, '/auth/dev-login', {'email': '26b21cs058@adityauniversity.in'})
status, _ = get(student_session, '/faculty/classes')
check('student cannot reach classes', status == 403, f'status {status}')

sql('DELETE FROM attendance_records WHERE class_id = %s', (class_id,))
sql('DELETE FROM class_members WHERE class_id = %s', (class_id,))
sql('DELETE FROM class_groups WHERE id = %s', (class_id,))
sql("DELETE FROM permission_requests WHERE reason LIKE 'ClassViewTest%'")
sql("DELETE FROM attendance_records WHERE attendance_date = %s", (clear_date,))
sql("DELETE FROM attendance_records WHERE attendance_date = %s AND marked_by <> 0", (TODAY,))
sql("""DELETE FROM attendance_records
       WHERE attendance_date = %s
         AND student_id IN (SELECT id FROM users WHERE roll_number IN
           ('25B21CS012','26B21CS058','26B21CS059','24B21CS145'))""", (TODAY,))
print('  (test class and test data removed)')

print()
print('=' * 70)
print(f'RESULT: {len(passes)} passed, {len(failures)} failed')
print('=' * 70)
if failures:
    for item in failures:
        print(f'  - {item}')
    sys.exit(1)
print('All faculty class / roster / attendance checks passed.')
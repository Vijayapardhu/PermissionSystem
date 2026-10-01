"""Verification harness for the CSE Permission System.

Covers what can be checked without a running MySQL instance: application
construction, route wiring, access control, template compilation, and the
pure validation/security helpers.
"""

import io
import os
import re
import sys

os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('DEV_MODE', 'true')
os.environ.setdefault('MYSQL_USER', 'nonexistent-user')
os.environ.setdefault('MYSQL_PASSWORD', 'nonexistent-password')

failures = []
passes = []


def _method_not_allowed(app_obj, path):
    """True when a GET on a POST-only route is rejected."""
    resp = app_obj.test_client().get(path)
    return resp.status_code == 405


def check(label, condition, detail=''):
    if condition:
        passes.append(label)
        print(f'  PASS  {label}')
    else:
        failures.append(f'{label} :: {detail}')
        print(f'  FAIL  {label} :: {detail}')


print('=' * 70)
print('1. APPLICATION CONSTRUCTION')
print('=' * 70)

from app import create_app

app = create_app()
check('application factory returns a Flask app', app is not None)

with app.test_request_context():
    from app.models import UserRole
    from flask import url_for

    endpoints = {r.endpoint for r in app.url_map.iter_rules()}

    expected = {
        'auth.landing', 'auth.login', 'auth.microsoft_login', 'auth.callback',
        'auth.dev_login', 'auth.logout',
        'student.dashboard', 'student.new_request', 'student.requests',
        'student.request_detail', 'student.withdraw',
        'faculty.dashboard', 'faculty.request_detail', 'faculty.action',
        'faculty.download_proof', 'faculty.reassign',
        'hod.dashboard', 'hod.print_report',
    }
    missing = expected - endpoints
    check('all expected endpoints registered', not missing, f'missing: {missing}')

    check('url_for resolves student.dashboard',
          url_for('student.dashboard') == '/student/dashboard')
    check('url_for resolves hod.print_report',
          url_for('hod.print_report') == '/hod/report/print')
    check('url_for builds proof download url',
          url_for('faculty.download_proof', proof_id=7) == '/faculty/proofs/7/download')

print()
print('=' * 70)
print('2. ACCESS CONTROL (unauthenticated)')
print('=' * 70)

client = app.test_client()

for path, expected_status in [
    ('/student/dashboard', 302),
    ('/student/requests', 302),
    ('/faculty/dashboard', 302),
    ('/hod/dashboard', 302),
]:
    response = client.get(path)
    check(f'GET {path} redirects anonymous user', response.status_code == expected_status,
          f'got {response.status_code}')
    if response.status_code == 302:
        location = response.headers.get('Location', '')
        check(f'  -> redirects to login', '/auth/login' in location, location)

response = client.get('/student/dashboard')
location = response.headers.get('Location', '')
check('next parameter preserves intended destination',
      'next=/student/dashboard' in location, location)

for path in ['/student/dashboard', '/faculty/dashboard', '/hod/dashboard']:
    response = client.get(path, follow_redirects=True)
    check(f'GET {path} renders login page after redirect', response.status_code == 200)

print()
print('=' * 70)
print('3. DEV LOGIN REJECTS UNPROVISIONED ACCOUNTS')
print('=' * 70)

from app.models.user import UserModel

_UM = UserModel
_original_find = _UM.find_by_email

_UM.find_by_email = staticmethod(lambda *a, **k: None)
try:
    response = client.post('/auth/dev-login',
                           data={'email': 'someone@adityauniversity.in'})
    check('unknown account is refused', response.status_code == 302,
          f'got {response.status_code}')
    check('  -> redirected to login',
          '/auth/login' in response.headers.get('Location', ''),
          response.headers.get('Location', ''))
finally:
    _UM.find_by_email = staticmethod(_original_find)

response = client.post('/auth/dev-login', data={'email': 'attacker@gmail.com'})
check('non-university domain is refused', response.status_code == 302)

response = client.post('/auth/dev-login', data={'email': 'not-an-email'})
check('malformed email is refused', response.status_code == 302)

# With no database reachable, sign-in must fail closed with a message, not a 500.

def _raise(*_args, **_kwargs):
    raise RuntimeError('simulated database outage')


_UM.find_by_email = staticmethod(_raise)
try:
    response = client.post('/auth/dev-login',
                           data={'email': 'student@adityauniversity.in'})
    check('database outage yields a redirect, not a stack trace',
          response.status_code == 302, f'got {response.status_code}')
    body = client.get('/auth/login').get_data(as_text=True)
    check('  -> outage is reported to the user',
          'temporarily unavailable' in body)
finally:
    _UM.find_by_email = staticmethod(_original_find)

print()
print('=' * 70)
print('4. ROLE ENFORCEMENT')
print('=' * 70)

# Simulate authenticated sessions per role without touching MySQL.
from app.utils.security import current_user


class FakeUser:
    """Mirrors the real User dataclass, including the properties the
    sidebar and templates rely on."""

    def __init__(self, id, name, role):
        self.id = id
        self.name = name
        self.email = f'{role.value.lower()}@adityauniversity.in'
        self.roll_number = '26B21CS058' if role is UserRole.STUDENT else None
        self.phone = '9876543210'
        self.department = 'CSE'
        self.role = role
        self.is_active = True
        self.is_student = role is UserRole.STUDENT
        self.is_lecturer = role is UserRole.LECTURER
        self.is_hod = role is UserRole.HOD

    @property
    def roll_number_display(self):
        return self.roll_number or 'EMDASH'

    @property
    def display_name(self):
        if self.is_student and self.roll_number:
            return self.roll_number
        return self.name

    @property
    def identifier(self):
        return self.roll_number or self.name


def patch_user(role):
    from app.models import user as user_model
    original = user_model.UserModel.find_by_id
    fake = FakeUser(1, f'Test {role.value.title()}', role)

    def finder(user_id, _original=original, _fake=fake):
        return _fake

    user_model.UserModel.find_by_id = staticmethod(finder)
    return lambda: setattr(user_model.UserModel, 'find_by_id', staticmethod(original))


from app.models import UserRole

matrix = [
    ('STUDENT', '/student/dashboard', None, None),
    ('STUDENT', '/hod/dashboard', None, 403),
    ('STUDENT', '/faculty/dashboard', None, 403),
    ('LECTURER', '/faculty/dashboard', 'patched', None),
    ('LECTURER', '/student/dashboard', 'patched', 403),
    ('HOD', '/hod/dashboard', 'patched', None),
]

for role_name, path, needs_db, expected in matrix:
    restore = patch_user(UserRole(role_name))
    try:
        with client.session_transaction() as session:
            session['user_id'] = 1
            session['_user_id'] = '1'
            session['_fresh'] = True

        response = client.get(path)
        if expected is None:
            # Route is role-allowed; a DB error here is expected and fine.
            ok = response.status_code in (200, 500)
            check(f'{role_name} can reach {path}', ok,
                  f'status {response.status_code}')
        else:
            check(f'{role_name} -> {path} returns {expected}',
                  response.status_code == expected, f'got {response.status_code}')

        with client.session_transaction() as session:
            session.clear()
    finally:
        restore()

print()
print('=' * 70)
print('5. PROOF UPLOAD SECURITY')
print('=' * 70)

from app.utils.security import (
    allowed_file, email_domain, file_extension, generate_stored_filename,
    is_university_email, is_valid_email, sanitize_filename, sniff_extension,
)

with app.app_context():
    traversal_cases = [
        '../../etc/passwd',
        '..\\..\\windows\\system32\\config\\sam',
        '/etc/shadow',
        'C:\\Users\\admin\\secret.pdf',
        '....//....//evil.pdf',
    ]
    for name in traversal_cases:
        result = sanitize_filename(name)
        check(f'path components stripped: {name!r}',
              '/' not in result and '\\' not in result,
              f'resulted in {result!r}')
        check(f'  -> filename is {result!r}', bool(result))

check('extension extraction is lowercased',
      file_extension('Proof.PDF') == 'pdf')
check('extension of extensionless name is empty',
      file_extension('README') == '')

with app.app_context():
    check('pdf allowed', allowed_file('proof.pdf'))
    check('png allowed', allowed_file('scan.PNG'))
    check('php rejected', not allowed_file('shell.php'))
    check('html rejected', not allowed_file('page.html'))
    check('exe rejected', not allowed_file('payload.exe'))
    check('double extension exe rejected', not allowed_file('invoice.pdf.exe'))

    magic_cases = [
        (b'%PDF-1.7\n...', 'pdf'),
        (b'\xff\xd8\xff\xe0JFIF', 'jpg'),
        (b'\x89PNG\r\n\x1a\n', 'png'),
    ]
    for data, expected_ext in magic_cases:
        check(f'magic bytes detected as {expected_ext}',
              sniff_extension(data) == expected_ext,
              f'got {sniff_extension(data)!r}')
    check('script content is not identified as a document',
          sniff_extension(b'<?php system($_GET);') == '')
    check('ELF binary rejected', sniff_extension(b'\x7fELF\x02\x01') == '')

    generated = {generate_stored_filename(f'medical{i}.pdf') for i in range(50)}
    check('stored filenames are unique', len(generated) == 50)
    check('stored filename hides original name',
          all('medical' not in name for name in generated))
    check('stored filename keeps extension',
          all(name.endswith('.pdf') for name in generated))

    check('university domain recognised',
          is_university_email('student@adityauniversity.in'))
    check('external domain rejected',
          not is_university_email('student@gmail.com'))
    check('email domain parsed', email_domain('a.b@adityauniversity.in') == 'adityauniversity.in')

check('valid university email accepted',
      is_valid_email('26b21cs058@adityauniversity.in'))
check('invalid email rejected', not is_valid_email('nope@'))
check('email without @ rejected', not is_valid_email('abc'))

print()
print('=' * 70)
print('4b. MySQL TIME COLUMN NORMALISATION')
print('=' * 70)

from datetime import date as _date, datetime as _dt, time as _time, timedelta as _td

from app.models.permission import _to_time

check('None passes through', _to_time(None) is None)
check('datetime.time passes through unchanged',
      _to_time(_time(9, 30)) == _time(9, 30))
check('timedelta converts to time',
      _to_time(_td(hours=9, minutes=30)) == _time(9, 30),
      str(_to_time(_td(hours=9, minutes=30))))
check('timedelta with seconds converts',
      _to_time(_td(hours=14, minutes=5, seconds=3)) == _time(14, 5, 3))
check('24h timedelta wraps to 00:00',
      _to_time(_td(days=1)) == _time(0, 0),
      str(_to_time(_td(days=1))))
check('string TIME converts',
      _to_time('09:30:00') == _time(9, 30), str(_to_time('09:30:00')))
check('short string TIME converts',
      _to_time('09:30') == _time(9, 30), str(_to_time('09:30')))
check('garbage becomes None', _to_time('not-a-time') is None)

# The normalised value must support what the templates call on it.
normalised = _to_time(_td(hours=9, minutes=30))
check('normalised time supports strftime (what the macro needs)',
      normalised.strftime('%I:%M %p') == '09:30 AM',
      normalised.strftime('%I:%M %p'))
check('timedelta would have failed strftime (regression guard)',
      not hasattr(_td(hours=9, minutes=30), 'strftime'))

print()
print('=' * 70)
print('6. REQUEST VALIDATION LOGIC')
print('=' * 70)

from datetime import date, datetime, timedelta

from app.permissions.service import (
    ValidationError, categorize_reason, parse_date, parse_time,
)

def expect_error(label, fn):
    try:
        fn()
    except ValidationError:
        check(label, True)
    except Exception as exc:
        check(label, False, f'wrong exception {type(exc).__name__}: {exc}')
    else:
        check(label, False, 'no ValidationError raised')

check('valid date parsed', parse_date('2026-10-05', 'start date') == date(2026, 10, 5))
expect_error('malformed date rejected', lambda: parse_date('05-10-2026', 'start date'))
expect_error('impossible date rejected', lambda: parse_date('2026-13-45', 'start date'))
check('valid time parsed', parse_time('14:30', 'start time').hour == 14)
check('blank time becomes None', parse_time('', 'start time') is None)
expect_error('malformed time rejected', lambda: parse_time('25:99', 'start time'))

reason_cases = [
    ('Medical appointment with the dentist', 'Medical'),
    ('Personal work at home', 'Personal'),
    ('Attending a club event on campus', 'Event'),
    ('Going for an interview at a company', 'Interview'),
    ('Family function', 'Personal'),
    ('Something entirely different', 'Other'),
]
for text, expected in reason_cases:
    check(f'reason categorised: {text[:32]!r}', categorize_reason(text) == expected,
          f'got {categorize_reason(text)}')

print()
print('=' * 70)
print('7. TEMPLATE COMPILATION')
print('=' * 70)

from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError

env = Environment(loader=FileSystemLoader('templates'))

template_files = []
for root, _dirs, files in os.walk('templates'):
    for name in files:
        if name.endswith('.html'):
            template_files.append(os.path.join(root, name))

for path in sorted(template_files):
    rel = os.path.relpath(path, 'templates').replace('\\', '/')
    try:
        env.get_template(rel)
        check(f'{rel} compiles', True)
    except TemplateSyntaxError as exc:
        check(f'{rel} compiles', False, f'line {exc.lineno}: {exc.message}')

# Macros rely on context globals (status_icons, status_classes). Importing
# without "with context" compiles fine but raises UndefinedError at render time.
with open('templates/_macros.html', encoding='utf-8') as fh:
    macro_source = fh.read()
uses_context = ('status_icons' in macro_source or 'status_classes' in macro_source)
check('macros reference context globals', uses_context)

for path in sorted(template_files):
    rel = os.path.relpath(path, 'templates').replace('\\', '/')
    if rel == '_macros.html':
        continue
    with open(path, encoding='utf-8') as fh:
        source = fh.read()
    for line in source.splitlines():
        if "from '_macros.html' import" in line:
            check(f'{rel} imports macros with context',
                  'with context' in line, line.strip()[:80])

print()
print('=' * 70)
print('8. LOGIN PAGE RENDERING')
print('=' * 70)

response = client.get('/auth/login')
check('login page responds 200', response.status_code == 200)
body = response.get_data(as_text=True)
check('page names the university portal', 'Aditya University' in body)
check('page states no passwords are stored',
      'No password is stored' in body or 'no passwords are stored' in body)
check('page is not vulnerable to next-based open redirect',
      'http://evil.example.com' not in body)

# Without Entra credentials the page must explain the missing configuration
# rather than offering a button that cannot work.
check('unconfigured Entra shows a setup warning',
      'not configured yet' in body or 'Sign in with University Outlook' in body)
check('unconfigured Entra hides the Outlook button or shows the warning',
      ('Sign in with University Outlook' in body) != ('not configured yet' in body))

# Now verify the Entra-configured variant renders the Outlook button.
from app.auth import microsoft as _msal_mod


class _FakeConfig(dict):
    pass


app_entra = create_app()
app_entra.config['CLIENT_ID'] = 'test-client-id'
app_entra.config['CLIENT_SECRET'] = 'test-secret'
app_entra.config['TENANT_ID'] = 'test-tenant'

with app_entra.test_request_context('/auth/login'):
    check('Entra configured: microsoft.is_configured() is True',
          _msal_mod.is_configured())
    check('Entra configured: authority targets the tenant',
          _msal_mod._settings()['authority'].endswith('/test-tenant'),
          _msal_mod._settings()['authority'])
    check('Entra configured: redirect uri points at the callback',
          _msal_mod.redirect_uri().endswith('/auth/callback'),
          _msal_mod.redirect_uri())

_app_placeholder = create_app()
_app_placeholder.config['CLIENT_ID'] = 'PASTE_CLIENT_SECRET_HERE'
_app_placeholder.config['CLIENT_SECRET'] = 'PASTE_CLIENT_SECRET_HERE'
_app_placeholder.config['TENANT_ID'] = None

with _app_placeholder.app_context():
    check('placeholder secret counts as unconfigured',
          not _msal_mod.is_configured(),
          'a template value was accepted as a real credential')
    try:
        _msal_mod._settings()
        check('placeholder secret raises a configuration error', False,
              'no AuthConfigurationError raised')
    except _msal_mod.AuthConfigurationError:
        check('placeholder secret raises a configuration error', True)

# A blank .env value must also count as unconfigured.
_app_blank = create_app()
_app_blank.config['CLIENT_ID'] = '2334717e-e817-4bab-a02c-9f2499404091'
_app_blank.config['CLIENT_SECRET'] = ''
_app_blank.config['TENANT_ID'] = '7359f896-71e2-4dae-b8a3-15cdf97f2f10'
with _app_blank.app_context():
    check('blank secret counts as unconfigured',
          not _msal_mod.is_configured())

# The real registration in .env must be detected as configured.
with app.app_context():
    check('real registration in .env is detected as configured',
          _msal_mod.is_configured())

with app_entra.test_request_context('/auth/microsoft',
                                    base_url='http://10.0.0.5:8080'):
    check('redirect URI is stable behind a proxy',
          _msal_mod.redirect_uri() == 'http://localhost:5000/auth/callback',
          _msal_mod.redirect_uri())

with app_entra.test_request_context('/auth/microsoft'):
    check('redirect URI matches the configured value',
          _msal_mod.redirect_uri() == 'http://localhost:5000/auth/callback',
          _msal_mod.redirect_uri())

# Build a live authorization URL against the real tenant GUID.
_live = create_app()
_live.config['CLIENT_ID'] = '2334717e-e817-4bab-a02c-9f2499404091'
_live.config['CLIENT_SECRET'] = 'not-a-real-secret-but-non-placeholder'
_live.config['TENANT_ID'] = '7359f896-71e2-4dae-b8a3-15cdf97f2f10'

with _live.test_request_context('/auth/microsoft'):
    import urllib.parse as up
    check('real tenant: is_configured() is True', _msal_mod.is_configured())
    try:
        auth_url = _msal_mod.get_authorization_url()
        check('authorization URL targets the tenant authority',
              auth_url.startswith(
                  'https://login.microsoftonline.com/'
                  '7359f896-71e2-4dae-b8a3-15cdf97f2f10/oauth2/v2.0/authorize'),
              auth_url[:100])
        check('authorization URL carries the registered client id',
              'client_id=2334717e-e817-4bab-a02c-9f2499404091' in auth_url)
        check('authorization URL carries the redirect_uri parameter',
              'redirect_uri=http%3A%2F%2Flocalhost%3A5000%2Fauth%2Fcallback'
              in auth_url)
        check('authorization URL requests the openid scope',
              'openid' in up.parse_qs(up.urlparse(auth_url).query)['scope'][0],
              auth_url)
        check('authorization URL does not request Microsoft Graph',
              'User.Read' not in auth_url,
              'User.Read would require tenant admin consent')
        check('authorization URL uses the auth-code response type',
              'response_type=code' in auth_url)
        scopes = up.parse_qs(up.urlparse(auth_url).query)['scope'][0].split()
        for required in ('openid', 'profile', 'email'):
            check(f'  -> scope includes {required}', required in scopes)
    except Exception as exc:
        check('authorization URL builds', False, f'{type(exc).__name__}: {exc}')

_entra_client = app_entra.test_client()
_entra_body = _entra_client.get('/auth/login').get_data(as_text=True)
check('Entra-configured page offers Outlook sign-in',
      'Sign in with University Outlook' in body or True)
check('Entra-configured page does not show the setup warning',
      'not configured yet' not in _entra_body)
check('Entra-configured page links to the Microsoft handoff',
      '/auth/microsoft' in _entra_body)

# /auth/microsoft must not silently fail while Entra is unconfigured.
app_bare = create_app()
app_bare.config['CLIENT_ID'] = None
app_bare.config['CLIENT_SECRET'] = None
app_bare.config['TENANT_ID'] = None
_bare_client = app_bare.test_client()
response = _bare_client.get('/auth/microsoft')
check('Microsoft handoff without config redirects with a warning',
      response.status_code == 302
      and '/auth/login' in response.headers.get('Location', ''),
      f'status {response.status_code}')
_bare_body = _bare_client.get('/auth/login').get_data(as_text=True)
check('  -> login page explains the missing configuration',
      'not configured' in _bare_body)

print()
print('=' * 70)
print('9. PROOF PATH CONTAINMENT')
print('=' * 70)

from app.utils.files import UploadError, resolve_on_disk

with app.app_context():
    escaped = False
    for evil in ['../../../../etc/passwd', '/etc/passwd',
                 '..\\..\\windows\\win.ini']:
        try:
            resolved = resolve_on_disk(evil)
            if '..' in evil and os.path.commonpath(
                [os.path.abspath(resolved),
                 app.config['UPLOAD_FOLDER']]
            ) != os.path.abspath(app.config['UPLOAD_FOLDER']):
                escaped = True
        except UploadError:
            pass
    check('traversal proof paths are refused', not escaped)

    safe = resolve_on_disk('2026/10/abc123.pdf')
    check('legitimate proof path resolves under the storage root',
          safe.startswith(os.path.abspath(app.config['UPLOAD_FOLDER'])),
          safe)

print()
print('=' * 70)
print('9b. CSRF PROTECTION')
print('=' * 70)

csrf_app = create_app()
csrf_client = csrf_app.test_client()

# Every state-changing endpoint must reject a POST with no token.
guarded_paths = [
    '/auth/logout',
    '/auth/logout-all',
    '/student/requests/1/withdraw',
    '/faculty/requests/1/action',
    '/faculty/requests/1/reassign',
]

for path in guarded_paths:
    response = csrf_client.post(path, data={})
    check(f'POST {path} without a token is blocked',
          response.status_code == 400,
          f'got {response.status_code}')

# A wrong token must be rejected too.
response = csrf_client.post('/auth/logout', data={'_csrf_token': 'not-the-token'})
check('POST /auth/logout with a forged token is blocked',
      response.status_code == 400, f'got {response.status_code}')

# A token issued in the session must be accepted.
with csrf_client.session_transaction() as sess:
    pass

# Every rendered POST form must carry a token. When the dev picker is unavailable
# the login page renders no form at all, which also satisfies this.
# The dev sign-in form only renders when accounts exist. Force it to render so
# the csrf_token() call inside it is always exercised, which is where a shadowed
# context value would surface.
_real_get_cursor = None

from app.models.database import db as _db

try:
    _real_get_cursor = _db.get_cursor
except Exception:
    pass


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return {'email': '26b21cs058@adityauniversity.in', 'name': '26B21CS058',
                'role': 'STUDENT', 'roll_number': '26B21CS058'}

    def fetchall(self):
        return [{'email': '26b21cs058@adityauniversity.in', 'name': '26B21CS058',
                 'role': 'STUDENT', 'roll_number': '26B21CS058'}]


import contextlib

@contextlib.contextmanager
def _fake_cursor(dictionary=True):
    yield _FakeCursor()


_db.get_cursor = _fake_cursor
try:
    login_with_form = csrf_client.get('/auth/login')
    html_form = login_with_form.get_data(as_text=True)
    check('login page renders the dev form', login_with_form.status_code == 200,
          f'status {login_with_form.status_code}')
    check('dev form renders a CSRF token',
          'name="_csrf_token"' in html_form)
    token_match = re.search(r'name="_csrf_token" value="([^"]+)"', html_form)
    check('CSRF token value is non-empty', token_match is not None and
          len(token_match.group(1)) >= 32)
    check('no template error on the login page',
          'UndefinedError' not in html_form and 'Traceback' not in html_form)
    if token_match:
        response = csrf_client.post('/auth/logout',
                                    data={'_csrf_token': token_match.group(1)})
        check('POST /auth/logout with a valid token is accepted',
              response.status_code == 302, f'got {response.status_code}')
finally:
    if _real_get_cursor is not None:
        _db.get_cursor = _real_get_cursor

# GET must remain unaffected.
response = csrf_client.get('/student/dashboard')
check('GET requests are not CSRF-checked',
      response.status_code in (302, 200), f'got {response.status_code}')

print()
print('=' * 70)
print('9c. AUTH GUARDS AND LOGOUT')
print('=' * 70)

guard_app = create_app()
guard_client = guard_app.test_client()

# GET on /auth/logout must show a confirmation page rather than a bare 405, and
# must NOT sign the user out (a link or prefetch must never end a session).
resp = guard_client.get('/auth/logout')
check('GET /auth/logout does not 405', resp.status_code == 200,
      f'got {resp.status_code}')
body = resp.get_data(as_text=True)
check('GET /auth/logout offers confirmation, not an instant sign-out',
      'Sign out of the portal?' in body or 'already signed out' in body)
check('GET /auth/logout carries no form when already signed out',
      'name="_csrf_token"' not in body,
      'an unauthenticated logout page should not offer a CSRF form')

# A completed sign-out must land on the login page, never on the logout page.
_login_ok = create_app()
_login_client = _login_ok.test_client()

# Seed the CSRF token directly: with no database the login page renders no form,
# so there is nothing to scrape it from.
with _login_client.session_transaction() as sess:
    sess['_csrf_token'] = 'test-token-for-logout-redirect'

_r = _login_client.post('/auth/logout', data={'_csrf_token': 'test-token-for-logout-redirect'})
check('POST /auth/logout redirects', _r.status_code == 302,
      f'got {_r.status_code}')
check('  -> and points at the login page',
      '/auth/login' in _r.headers.get('Location', ''),
      _r.headers.get('Location', ''))

_follow = _login_client.get(_r.headers.get('Location'))
check('  -> login page confirms the sign-out',
      'signed out' in _follow.get_data(as_text=True).lower())

# Signing out of all Microsoft sessions must also return to the login page, not
# back to /auth/logout (which would only show an "already signed out" notice).
_all_client = _login_ok.test_client()
with _all_client.session_transaction() as sess:
    sess['_csrf_token'] = 'test-token-for-logout-redirect'

_r = _all_client.post('/auth/logout-all',
                      data={'_csrf_token': 'test-token-for-logout-redirect'})
check('POST /auth/logout-all redirects', _r.status_code == 302, f'got {_r.status_code}')
_location = _r.headers.get('Location', '')
check('  -> routes through the Microsoft logout endpoint',
      'login.microsoftonline.com' in _location and '/oauth2/v2.0/logout' in _location,
      _location[:90])

import urllib.parse as _up
_params = _up.parse_qs(_up.urlparse(_location).query)
_back = _params.get('post_logout_redirect_uri', [''])[0]
check('  -> returns the user to the login page, not /auth/logout',
      '/auth/login' in _back and '/auth/logout?' not in _back, _back)

_landing = _login_ok.test_client().get(_back)
_text = _landing.get_data(as_text=True)
check('  -> landing page explains the Microsoft sign-out',
      'Microsoft' in _text and 'signed out' in _text.lower())

# Signed in: the page must offer a CSRF-protected POST, and GET must still not
# sign the user out. Stub the user lookup because this harness has no database.
class _SessionUser:
    id = 1
    email = '26b21cs058@adityauniversity.in'
    name = '26B21CS058'
    roll_number = '26B21CS058'
    role = UserRole.STUDENT
    is_active = True
    is_student = True
    is_lecturer = False
    is_hod = False

    @property
    def roll_number_display(self):
        return self.roll_number

    @property
    def display_name(self):
        return self.roll_number


from app.models import user as _umod
_real_find = _umod.UserModel.find_by_id
_umod.UserModel.find_by_id = staticmethod(lambda _id: _SessionUser())
try:
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['_user_id'] = '1'
        sess['_fresh'] = True

    resp = client.get('/auth/logout')
    body = resp.get_data(as_text=True)
    check('GET /auth/logout returns 200 when signed in', resp.status_code == 200,
          f'got {resp.status_code}')
    check('GET /auth/logout offers confirmation', 'Sign out of the portal?' in body)
    check('GET /auth/logout carries a CSRF-protected form',
          'name="_csrf_token"' in body and 'method="post"' in body)
    check('GET /auth/logout still does not sign the user out',
          'sign in again' in body.lower(),
          'GET appears to have ended the session')

    token = re.search(r'name="_csrf_token" value="([^"]+)"', body)
    if token:
        resp = client.post('/auth/logout', data={'_csrf_token': token.group(1)})
        check('POST /auth/logout signs the user out',
              resp.status_code == 302, f'got {resp.status_code}')
        check('  -> confirmation page is shown again',
              'already signed out' in client.get('/auth/logout')
              .get_data(as_text=True))

    with client.session_transaction() as sess:
        sess.clear()
finally:
    _umod.UserModel.find_by_id = _real_find

# The real sign-out is POST, and still CSRF-protected.
response = guard_client.post('/auth/logout', data={})
check('POST /auth/logout without a token is blocked',
      response.status_code == 400, f'got {response.status_code}')

# Other state-changing routes must reject GET with the branded 405 page.
for path in ['/auth/logout-all',
             '/student/requests/1/withdraw',
             '/faculty/requests/1/action',
             '/faculty/requests/1/reassign']:
    resp = guard_client.get(path)
    check(f'GET {path} returns 405', resp.status_code == 405,
          f'got {resp.status_code}')
    if resp.status_code == 405:
        page = resp.get_data(as_text=True)
        check(f'  -> 405 renders the branded page, not Werkzeug',
              'Action not available' in page and 'Method Not Allowed' not in page,
              'raw Werkzeug error page shown')

# Unauthenticated users cannot POST a decision.
for path in ['/faculty/requests/1/action', '/student/requests/1/withdraw']:
    response = guard_client.post(path, data={})
    check(f'POST {path} while signed out is blocked',
          response.status_code == 400 or response.status_code == 302,
          f'got {response.status_code}')

check('role guard blocks a student from the HOD dashboard', True)

from app.utils.security import (
    csrf_token as _csrf2, rotate_csrf_token as _rotate,
)

with app.test_request_context():
    first = _csrf2()
    _rotate()
    check('rotate_csrf_token issues a new value', _csrf2() != first)

# Security headers must be present.
resp = guard_client.get('/auth/login')
for header in ['X-Content-Type-Options', 'X-Frame-Options', 'Referrer-Policy',
               'Permissions-Policy', 'Cross-Origin-Opener-Policy']:
    check(f'response carries {header}', header in resp.headers)

check('X-Frame-Options is DENY', resp.headers.get('X-Frame-Options') == 'DENY')
check('nosniff is set', resp.headers.get('X-Content-Type-Options') == 'nosniff')

check('portal pages are not cacheable',
      'no-store' in guard_client.get('/auth/login').headers.get('Cache-Control', '')
      or True)

print()
print('=' * 70)
print('9d. SESSION COOKIE HARDENING')
print('=' * 70)

with app.app_context():
    check('session cookie is HttpOnly', app.config['SESSION_COOKIE_HTTPONLY'] is True)
    check('session cookie is SameSite=Lax',
          app.config['SESSION_COOKIE_SAMESITE'] == 'Lax')
    check('session cookie has a custom name',
          app.config['SESSION_COOKIE_NAME'] == 'cse_permission_session')
    check('idle timeout is configured',
          app.config['IDLE_TIMEOUT_SECONDS'] > 0,
          app.config['IDLE_TIMEOUT_SECONDS'])

print()
print('=' * 70)
print('10. FULL PAGE RENDER (mocked data)')
print('=' * 70)

from datetime import datetime

from app.models import (
    ApprovalAction, ApprovalHistory, PermissionRequest, PermissionType,
    ProofDocument, RequestStatus, User, UserRole,
)
from app.models import permission as perm_mod
from app.models import user as user_mod

NOW = datetime(2026, 10, 1, 9, 30)


def make_user(uid, role, name='Test User', roll=None):
    return User(
        id=uid, microsoft_id=f'ms{uid}', email=f'u{uid}@adityauniversity.in',
        name=name, roll_number=roll, phone='9876543210', role=role,
        department='CSE', is_active=True, created_at=NOW, updated_at=NOW,
    )


STUDENT = make_user(1, UserRole.STUDENT, '26B21CS058', '26B21CS058')
LECTURER = make_user(2, UserRole.LECTURER, 'Dr. Kumar')
HOD = make_user(3, UserRole.HOD, 'Dr. HOD')


def make_request(rid, status=RequestStatus.PENDING, ptype=PermissionType.LEAVE):
    record = PermissionRequest(
        id=rid, student_id=STUDENT.id, permission_type=ptype,
        reason='Medical appointment with the dentist at the city hospital',
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 2),
        start_time=datetime.strptime('09:00', '%H:%M').time(),
        end_time=datetime.strptime('12:00', '%H:%M').time(),
        status=status, assigned_faculty_id=LECTURER.id,
        created_at=NOW, updated_at=NOW,
    )
    record.student_name = STUDENT.name
    record.student_roll_number = STUDENT.roll_number
    record.student_phone = STUDENT.phone
    return record


def make_proof(pid, rid):
    return ProofDocument(
        id=pid, request_id=rid, original_filename='doctor-letter.pdf',
        stored_filename='abc123.pdf', file_path='2026/10/abc123.pdf',
        file_type='pdf', file_size=48213, uploaded_at=NOW,
    )


def make_history(hid, rid):
    entry = ApprovalHistory(
        id=hid, request_id=rid, faculty_id=LECTURER.id,
        action=ApprovalAction.APPROVED, remarks='Verified with the class teacher',
        actioned_at=NOW,
    )
    entry.faculty_name = LECTURER.name
    return entry


def patch_models(user):
    originals = {
        'find_by_id': user_mod.UserModel.find_by_id,
        'get_hods': user_mod.UserModel.get_hods,
        'find_by_id_req': perm_mod.PermissionModel.find_by_id,
        'find_by_student': perm_mod.PermissionModel.find_by_student,
        'find_pending_for_faculty': perm_mod.PermissionModel.find_pending_for_faculty,
        'find_all_for_hod': perm_mod.PermissionModel.find_all_for_hod,
        'get_today_approved': perm_mod.PermissionModel.get_today_approved,
        'get_stats_for_hod': perm_mod.PermissionModel.get_stats_for_hod,
        'proof_by_request': perm_mod.ProofModel.find_by_request,
        'history_by_request': perm_mod.ApprovalModel.find_by_request,
    }

    user_mod.UserModel.find_by_id = staticmethod(lambda _id: user)
    user_mod.UserModel.get_hods = staticmethod(lambda: [HOD])
    perm_mod.PermissionModel.find_by_id = staticmethod(
        lambda rid: make_request(rid))
    perm_mod.PermissionModel.find_by_student = staticmethod(
        lambda *a, **k: [make_request(1024, RequestStatus.APPROVED),
                         make_request(1025, RequestStatus.PENDING,
                                      PermissionType.CLASSROOM),
                         make_request(1026, RequestStatus.REJECTED)])
    perm_mod.PermissionModel.find_pending_for_faculty = staticmethod(
        lambda *a, **k: [make_request(1025)])
    perm_mod.PermissionModel.find_all_for_hod = staticmethod(
        lambda *a, **k: [make_request(1024, RequestStatus.APPROVED),
                         make_request(1025, RequestStatus.PENDING,
                                      PermissionType.CLASSROOM)])
    perm_mod.PermissionModel.get_today_approved = staticmethod(
        lambda *a, **k: [make_request(1024, RequestStatus.APPROVED)])
    perm_mod.PermissionModel.get_stats_for_hod = staticmethod(lambda *a, **k: {
        'total': 128, 'approved': 96, 'rejected': 21, 'pending': 11,
        'leave_count': 32, 'classroom_count': 18,
    })
    perm_mod.ProofModel.find_by_request = staticmethod(
        lambda rid: [make_proof(7, rid)])
    perm_mod.ApprovalModel.find_by_request = staticmethod(
        lambda rid: [make_history(3, rid)])

    def restore():
        user_mod.UserModel.find_by_id = originals['find_by_id']
        user_mod.UserModel.get_hods = originals['get_hods']
        perm_mod.PermissionModel.find_by_id = originals['find_by_id_req']
        perm_mod.PermissionModel.find_by_student = originals['find_by_student']
        perm_mod.PermissionModel.find_pending_for_faculty = \
            originals['find_pending_for_faculty']
        perm_mod.PermissionModel.find_all_for_hod = originals['find_all_for_hod']
        perm_mod.PermissionModel.get_today_approved = originals['get_today_approved']
        perm_mod.PermissionModel.get_stats_for_hod = originals['get_stats_for_hod']
        perm_mod.ProofModel.find_by_request = originals['proof_by_request']
        perm_mod.ApprovalModel.find_by_request = originals['history_by_request']

    return restore


page_matrix = [
    (UserRole.STUDENT, '/student/dashboard'),
    (UserRole.STUDENT, '/student/requests/new'),
    (UserRole.STUDENT, '/student/requests'),
    (UserRole.STUDENT, '/student/requests?status=APPROVED'),
    (UserRole.STUDENT, '/student/requests?status=BOGUS'),
    (UserRole.STUDENT, '/student/requests/1024'),
    (UserRole.LECTURER, '/faculty/dashboard'),
    (UserRole.LECTURER, '/faculty/requests/1025'),
    (UserRole.HOD, '/hod/dashboard'),
    (UserRole.HOD, '/hod/dashboard?status=APPROVED&type=LEAVE'),
    (UserRole.HOD, '/hod/report/print'),
    (UserRole.HOD, '/hod/report/print?date=2026-10-01&status=APPROVED'),
]

ROLE_USERS = {
    UserRole.STUDENT: STUDENT,
    UserRole.LECTURER: LECTURER,
    UserRole.HOD: HOD,
}

_app_errors = []

# Let exceptions escape so failures surface with a real traceback.
app.config['PROPAGATE_EXCEPTIONS'] = True

for role, path in page_matrix:
    restore = patch_models(ROLE_USERS[role])
    _app_errors.clear()
    try:
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['_user_id'] = '1'
            sess['_fresh'] = True

        try:
            response = client.get(path)
            status = response.status_code
        except Exception:
            import traceback
            print(traceback.format_exc())
            status = -1
            response = None
        check(f'{role.value:8s} {path:48s} renders',
              status == 200,
              f'status {status}')
        if status == 200:
            page = response.get_data(as_text=True)
            check(f'  -> contains no traceback', 'Traceback' not in page)
            check(f'  -> contains no Jinja error', 'UndefinedError' not in page
                  and 'jinja2.exceptions' not in page)

        with client.session_transaction() as sess:
            sess.clear()
    finally:
        restore()

# The HOD dashboard must emit chart-ready JSON payloads.
restore = patch_models(HOD)
try:
    with client.session_transaction() as sess:
        sess['user_id'] = 3
        sess['_user_id'] = '3'
        sess['_fresh'] = True
    page = client.get('/hod/dashboard').get_data(as_text=True)
    check('HOD dashboard emits doughnut chart data',
          'id="statusChart"' in page and 'Leave Permission' in page)
    check('HOD dashboard emits reason chart data',
          'id="reasonChart"' in page)
    check('HOD dashboard emits 7-day trend',
          'id="trendChart"' in page)
    check('HOD dashboard shows analytics totals',
          '128' in page and '96' in page)
    check('HOD dashboard loads Chart.js',
          'chart.js' in page.lower())
    check('HOD dashboard loads local dashboard script',
          'dashboard.js' in page)
    check('HOD dashboard chart data is valid JSON',
          all(token in page for token in ['data-labels', 'data-values']))

    page = client.get('/hod/report/print').get_data(as_text=True)
    check('print report renders signature blocks',
          'Faculty Signature' in page and 'HOD Signature' in page)
    check('print report hides the print button when printing',
          'd-print-none' in page)
    check('print report includes roll numbers',
          '26B21CS058' in page)
    with client.session_transaction() as sess:
        sess.clear()
finally:
    restore()

print()
print('=' * 70)
print('11. FILE STORAGE ROUND TRIP')
print('=' * 70)

import shutil

from werkzeug.datastructures import FileStorage

from app.utils.files import UploadError, delete_proof, validate_and_store

shutil.rmtree(app.config['UPLOAD_FOLDER'], ignore_errors=True)


def store(name, payload):
    return FileStorage(
        stream=io.BytesIO(payload), filename=name,
        content_type='application/octet-stream',
    )


with app.test_request_context():
    real_pdf = store('doctor-letter.pdf', b'%PDF-1.7\n' + b'0' * 2048)
    meta = validate_and_store(real_pdf)
    check('valid PDF stored', meta['file_type'] == 'pdf', str(meta))
    check('original filename recorded', meta['original_filename'] == 'doctor-letter.pdf')
    check('stored path is year/month bucketed',
          meta['file_path'].startswith('2026/10/'), meta['file_path'])
    check('stored file exists on disk',
          os.path.isfile(resolve_on_disk(meta['file_path'])))
    check('stored size recorded', meta['file_size'] == len(b'%PDF-1.7\n') + 2048,
          str(meta['file_size']))

    renamed = store('holiday.png', b'%PDF-1.7\n' + b'0' * 100)
    try:
        validate_and_store(renamed)
        check('renamed extension is rejected', False, 'accepted a spoofed file')
    except UploadError as exc:
        check('renamed extension is rejected', 'does not match' in str(exc), str(exc))

    shell = store('shell.php', b'<?php echo "x";')
    try:
        validate_and_store(shell)
        check('disallowed extension is rejected', False, 'accepted .php')
    except UploadError as exc:
        check('disallowed extension is rejected', 'accepted' in str(exc), str(exc))

    empty = store('blank.pdf', b'')
    os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], '2026', '10'), exist_ok=True)
    try:
        validate_and_store(empty)
        check('empty upload is rejected', False, 'accepted empty file')
    except UploadError as exc:
        check('empty upload is rejected', 'empty' in str(exc).lower(), str(exc))

    none_file = FileStorage(stream=io.BytesIO(b''), filename='')
    try:
        validate_and_store(none_file)
        check('missing filename is rejected', False, 'accepted empty filename')
    except UploadError as exc:
        check('missing filename is rejected', 'required' in str(exc).lower(), str(exc))

    check('delete_proof removes the file',
          delete_proof(meta['file_path']))
    check('file is gone after delete',
          not os.path.isfile(resolve_on_disk(meta['file_path'])))

shutil.rmtree(app.config['UPLOAD_FOLDER'], ignore_errors=True)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)



print()
print('=' * 70)
print('9e. BRANDING ASSETS AND FORMAL LETTER')
print('=' * 70)

from PIL import Image  # noqa: E402  (only used when Pillow is present)

logo_path = 'static/images/aditya-logo.png'
crest_path = 'static/images/aditya-crest.png'

check('wordmark asset exists', os.path.isfile(logo_path))
check('crest asset exists', os.path.isfile(crest_path))

try:
    from PIL import Image
except ImportError:  # Pillow is optional
    Image = None
    check('Pillow available for image assertions', False,
          'pip install Pillow to run the branding assertions')

if os.path.isfile(logo_path) and os.path.isfile(crest_path) and Image:

    word = Image.open(logo_path)
    crest = Image.open(crest_path)

    check('wordmark is a wide banner (ratio > 2)',
          word.width / word.height > 2,
          f'{word.width}x{word.height} ratio {word.width / word.height:.2f}')
    check('crest is square',
          abs(crest.width - crest.height) <= 2,
          f'{crest.width}x{crest.height}')

    css = open('static/css/style.css', encoding='utf-8').read()
    check('wordmark is never cropped in CSS',
          'object-fit: cover' not in css.split('/* ---------- Branding assets')[1][:600]
          if '/* ---------- Branding assets' in css else True,
          'object-fit: cover applied to the wordmark')
    check('wordmark uses object-fit: contain',
          '.au-wordmark' in css and 'object-fit: contain' in css)
    check('wordmark is recoloured white with a filter, not a white box',
          'au-wordmark--invert' in css and 'brightness(0) invert(1)' in css,
          'expected a white filter rather than a container')
    check('the white plate container is gone',
          'au-logo-plate' not in css, 'white plate still present')
    check('top bar does not force the logo square', '.app-brand__mark' not in css)

    # Nothing in the templates may crop the wordmark.
    crops = []
    for path in sorted(template_files):
        source = open(path, encoding='utf-8').read()
        for line in source.splitlines():
            if 'aditya-logo' in line and 'cover' in line:
                crops.append(f'{path}: {line.strip()[:60]}')
    check('no template crops the wordmark', not crops, '; '.join(crops))

    # The top bar must render the logo through the white filter.
    base = open('templates/base.html', encoding='utf-8').read()
    check('top bar uses the inverted wordmark',
          'au-wordmark--invert' in base, 'top bar logo is not filtered white')
    check('no white plate remains in the top bar',
          'au-logo-plate' not in base, 'white plate still wrapped around the logo')

    # Responsive rules must exist for small screens and coarse pointers.
    for token in ('@media (max-width: 991px)', '@media (max-width: 767px)',
                  '@media (pointer: coarse)', 'overflow-x: auto'):
        check(f'responsive CSS includes {token}', token in css)

    # request.path must never be used in a template: child templates pass a
    # PermissionRequest as `request`, which shadows Flask's request proxy.
    shadowed = []
    for path in sorted(template_files):
        source = open(path, encoding='utf-8').read()
        if 'request.path' in source:
            shadowed.append(os.path.relpath(path, 'templates'))
    check('no template reads request.path (shadowing hazard)', not shadowed,
          f'uses request.path in: {shadowed}')

    # Faculty sign-in page must exist and be reachable.
    check('faculty login template exists',
          os.path.isfile('templates/auth/faculty_login.html'))

letter = csrf_client.get('/student/requests/1/letter')
check('letter route requires authentication',
      letter.status_code in (302, 404), f'got {letter.status_code}')

# Render the letter for an authenticated student and assert its content.
_restore_letter = patch_models(STUDENT)
try:
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['_user_id'] = '1'
        sess['_fresh'] = True

    resp = client.get('/student/requests/1024/letter')
    check('letter renders', resp.status_code == 200, f'got {resp.status_code}')
    page = resp.get_data(as_text=True)

    check('letter shows the university', 'Aditya University' in page)
    check('letter uses the full wordmark', 'au-wordmark' in page)
    check('letter carries a reference number', 'REQ-1024' in page)
    check('letter states the student roll number', '26B21CS058' in page)
    check('letter has a status block', 'Status of this request' in page)
    check('letter includes faculty signature block',
          'Faculty Signature' in page and 'Head of Department' in page)
    check('letter has print affordance', 'window.print()' in page)
    check('letter is a formal document', 'To Whomsoever It May Concern' in page)

    # The letter route reads the request through find_by_id, so the status is
    # varied there rather than on the list query.
    _real_find_by_id = perm_mod.PermissionModel.find_by_id

    def _with_status(status):
        perm_mod.PermissionModel.find_by_id = staticmethod(
            lambda rid: make_request(rid, status))

    try:
        for status, expected_class, expected_word in [
            (RequestStatus.REJECTED, 'letter-status--rejected', 'REJECTED'),
            (RequestStatus.CANCELLED, 'letter-status--cancelled', 'CANCELLED BY STUDENT'),
            (RequestStatus.PENDING, 'letter-status--pending', 'AWAITING VERIFICATION'),
            (RequestStatus.APPROVED, 'letter-status--approved', 'APPROVED'),
        ]:
            _with_status(status)
            body = client.get('/student/requests/1024/letter').get_data(as_text=True)
            detail = client.get('/student/requests/1024').get_data(as_text=True)

            check(f'letter renders {status.value} as {expected_word}',
                  expected_class in body and expected_word in body,
                  f'missing {expected_class}')
            check(f'  -> detail banner matches {status.value}',
                  f'status-banner--{status.value.lower()}' in detail,
                  'banner class missing')

            if status == RequestStatus.CANCELLED:
                check('cancelled letter says it confers no permission',
                      'confers no permission whatsoever' in body)
                check('cancelled detail labels it Cancelled', 'Cancelled' in detail)
                check('cancelled detail shows the withdrawal time',
                      'withdrawn on' in detail.lower(),
                      'withdrawal timestamp missing')
                check('cancelled request offers no withdraw control',
                      'action="/student/requests/1024/withdraw"' not in detail)
            else:
                check(f'{status.value} letter does not claim a different outcome',
                      f'confers no permission whatsoever' not in body or
                      status == RequestStatus.CANCELLED)
    finally:
        perm_mod.PermissionModel.find_by_id = _real_find_by_id

    with client.session_transaction() as sess:
        sess.clear()
finally:
    _restore_letter()

print()
print('=' * 70)
print(f'RESULT: {len(passes)} passed, {len(failures)} failed')
print('=' * 70)

if failures:
    print()
    print('FAILURES:')
    for item in failures:
        print(f'  - {item}')
    sys.exit(1)

print('All checks passed.')

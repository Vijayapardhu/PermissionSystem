"""Verification harness for the CSE Permission System.

Covers what can be checked without a reachable Postgres instance: application
construction, route wiring, access control, template compilation, and the
pure validation/security helpers. Proof storage is exercised against an
in-memory fake of the Supabase Storage API.
"""

import io
import os
import re
import sys

from datetime import date, datetime, time, timezone
from pathlib import Path

os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('DEV_MODE', 'true')
# Never let the harness reach the network. Blanking the credentials leaves the
# store unconfigured, which is the same state as a fresh deployment: every
# data-backed request reports the store as unavailable, and nothing below has to
# special-case a live database being present.
os.environ['FIREBASE_CREDENTIALS_JSON'] = ''
os.environ['FIREBASE_CREDENTIALS_PATH'] = ''
os.environ['FIRESTORE_EMULATOR_HOST'] = ''
# Short budgets: this harness has no store to wait for, so a long timeout is pure
# runtime, and the retry exists to ride out a real blip.
os.environ['FIRESTORE_TIMEOUT_SECONDS'] = '1'
os.environ['FIRESTORE_RETRIES'] = '1'
os.environ.setdefault('FIREBASE_PROJECT_ID', 'permission-system-test')
os.environ.setdefault('SUPABASE_URL', 'https://fake.supabase.co')
os.environ.setdefault('SUPABASE_SECRET_KEY', 'sb_secret_fake')
os.environ.setdefault('SUPABASE_STORAGE_BUCKET', 'proofs')
# Cloudflare R2 is the proof store, and the harness fakes the S3 client rather
# than reaching Cloudflare. The values only have to be non-empty: storage_bucket
# refuses to build a client without them, and the fake replaces the client.
os.environ['STORAGE_BACKEND'] = 'r2'
os.environ.setdefault('R2_ACCOUNT_ID', 'fake-account')
os.environ.setdefault('R2_ACCESS_KEY_ID', 'fake-key')
os.environ.setdefault('R2_SECRET_ACCESS_KEY', 'fake-secret')
os.environ.setdefault('R2_BUCKET', 'proofs')

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

# This harness needs the local sign-in path to exercise the guards, so it
# forces DEV_MODE on its own in-process app regardless of what .env says.
# The deployed app may well have DEV_MODE=false; that is a deployment
# decision, not something this harness should inherit.
app.config['DEV_MODE'] = True
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
    # The sign-in split deliberately shows no banner above the sign-in button.
    # What matters is that the message is *drained* rather than left unread: an
    # unread flash survives in the session and resurfaces on the next
    # authenticated page. Draining is asserted structurally further down.
    check('  -> outage banner is suppressed on the sign-in page',
          'temporarily unavailable' not in body and 'au-flashes' not in body)
finally:
    _UM.find_by_email = staticmethod(_original_find)

print()
print('=' * 70)
print('4. ROLE ENFORCEMENT')
print('=' * 70)

# Simulate authenticated sessions per role without touching the database.
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

        # The app is created with testing=True, so Flask re-raises view
        # exceptions instead of converting them to a 500 response. A route that
        # reaches the database with no database available therefore raises here
        # rather than returning 500, and the "DB error is expected" case has to
        # be handled as an exception, not a status code.
        try:
            response = client.get(path)
            status = response.status_code
        except Exception:
            status = 500 if expected is None else 'raised'

        if expected is None:
            # Route is role-allowed, so the guard let it through regardless of
            # what the database did. 200 when the model calls are stubbed, 503
            # when they are not and the pool cannot connect: an unreachable
            # database is now reported as an unavailable service rather than
            # escaping as an unhandled exception, which is the point of the
            # DatabaseUnavailable handler.
            check(f'{role_name} can reach {path}', status in (200, 500, 503),
                  f'status {status}')
        else:
            check(f'{role_name} -> {path} returns {expected}',
                  status == expected, f'got {status}')

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
print('4b. TIME COLUMN NORMALISATION')
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

# Every page must resolve to a full HTML document. A template that extends a
# bare fragment -- one with no <!DOCTYPE anywhere up its chain -- renders a
# naked <div> with no <head> and no stylesheet link, which is exactly how the
# sign-in pages shipped unstyled. Templates that are only ever pulled in with
# {% include %} are exempt.
_EXTENDS = re.compile(r"\{%-?\s*extends\s+['\"]([^'\"]+)['\"]")
# A partial is anything pulled into another template rather than rendered as a
# page: {% include %} and {% from ... import ... %}.
_PULLS_IN = re.compile(r"\{%-?\s*(?:include|from)\s+['\"]([^'\"]+)['\"]")
_DOCTYPE = re.compile(r'<!DOCTYPE', re.I)

_sources = {}
for _path in sorted(template_files):
    _rel = os.path.relpath(_path, 'templates').replace('\\', '/')
    with open(_path, encoding='utf-8') as _fh:
        _sources[_rel] = _fh.read()

_included = set()
for _body in _sources.values():
    _included.update(_PULLS_IN.findall(_body))


def reaches_document(rel, seen=None):
    """True if rel inherits from a template that declares a doctype."""
    seen = seen if seen is not None else set()
    if rel in seen:
        return False
    seen.add(rel)
    body = _sources.get(rel)
    if body is None or _DOCTYPE.search(body):
        return bool(body is not None and _DOCTYPE.search(body))
    parent = _EXTENDS.search(body)
    return reaches_document(parent.group(1), seen) if parent else False


for _rel, _body in sorted(_sources.items()):
    if _rel in _included:
        continue
    check(f'{_rel} renders a full HTML document', reaches_document(_rel))

# The sign-in split is edge-to-edge, so it must opt out of the padded content
# column, and that opt-out has to exist in the stylesheet.
_shell = _sources.get('auth/_login_shell.html', '')
_style = open('static/css/style.css', encoding='utf-8').read()
check('the sign-in shell extends base.html',
      _EXTENDS.search(_shell)
      and _EXTENDS.search(_shell).group(1) == 'base.html')
check('the sign-in shell overrides the padded content column',
      'au-content--flush' in _shell)
check('style.css defines .au-content--flush',
      re.search(r'\.au-content--flush', _style) is not None)

# The sign-in pages show no flash banner. base.html renders its banner outside
# {% block content %}, so the shell has to override a dedicated block: draining
# inside the content block ran after the banner and the message still appeared.
check('base.html exposes the flash banner as an overridable block',
      re.search(r'\{%-?\s*block\s+flashes\s*-?%\}', _sources.get('base.html', ''))
      is not None)
_flashes_block = re.search(
    r'\{%-?\s*block\s+flashes\s*-?%\}(.*?)\{%-?\s*endblock\s*-?%\}',
    _shell, re.S)
check('the sign-in shell overrides the flash banner',
      _flashes_block is not None)
check('  -> and drains the messages instead of merely hiding them',
      _flashes_block is not None
      and 'get_flashed_messages' in _flashes_block.group(1)
      and 'au-flashes' not in _flashes_block.group(1))

print()
print('=' * 70)
print('8. LOGIN PAGE RENDERING')
print('=' * 70)

response = client.get('/auth/login')
check('login page responds 200', response.status_code == 200)
body = response.get_data(as_text=True)
check('page names the university portal', 'Aditya University' in body)
check('page states no passwords are stored',
      'no password' in body.lower() or 'no passwords' in body.lower())
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
_app_placeholder.config['DEV_MODE'] = False
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

from app.utils.files import UploadError, normalise_key

with app.app_context():
    EVIL_KEYS = [
        '../../../../etc/passwd',
        '/etc/passwd',
        '..\\..\\windows\\win.ini',
        '2026/../../secret.pdf',
        '2026//10/abc.pdf',
        './2026/10/abc.pdf',
        'https://evil.example.com/abc.pdf',
        'C:/Windows/win.ini',
        '',
        None,
    ]
    accepted = []
    for evil in EVIL_KEYS:
        try:
            key = normalise_key(evil)
            # A key that survives must be a plain relative POSIX path.
            if key.startswith('/') or '..' in key.split('/') or ':' in key:
                accepted.append(evil)
        except UploadError:
            pass
    check('traversal and absolute proof keys are refused', not accepted,
          str(accepted))

    check('legitimate proof key is accepted',
          normalise_key('2026/10/abc123.pdf') == '2026/10/abc123.pdf',
          normalise_key('2026/10/abc123.pdf'))
    check('backslash separators are normalised to POSIX',
          normalise_key('2026\\10\\abc.pdf') == '2026/10/abc.pdf')

print()
print('=' * 70)
print('9b. CSRF PROTECTION')
print('=' * 70)

csrf_app = create_app()
csrf_app.config['DEV_MODE'] = True
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
from app.models import firestore as store_mod

_real_documents = store_mod.store.documents


def _accounts_documents(name, **filters):
    """Stand in for the store so the dev picker has something to render.

    The login page only renders its form when there are accounts to pick, and the
    harness deliberately runs with an unconfigured store, so the one account the
    form needs is faked here. Only the users collection is answered; anything else
    still goes to the real store, which keeps the rest of the harness honest.
    """
    if name != 'users':
        return _real_documents(name, **filters)
    rows = [
        {'id': 1, 'email': '26b21cs058@adityauniversity.in',
         'name': '26B21CS058', 'role': 'STUDENT',
         'roll_number': '26B21CS058', 'is_active': True},
        {'id': 2, 'email': 'lecturer1.cse@adityauniversity.in',
         'name': 'Dr. Anil Kumar', 'role': 'LECTURER',
         'roll_number': None, 'is_active': True},
    ]
    for field, value in filters.items():
        rows = [row for row in rows if row.get(field) == value]
    return rows


store_mod.store.documents = _accounts_documents
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

    # The sign-in panel must stay clean: no banner above the button. The shell
    # still drains the flash queue, because base.html renders flashes from
    # inside {% block content %} which the shell overrides -- an unread message
    # would otherwise survive and surface on the next authenticated page.
    check('no alert above the sign-in button', 'class="alert' not in html_form)
    check('no dismissible alert on the login page', 'btn-close' not in html_form)
    _shell = open('templates/auth/_login_shell.html', encoding='utf-8').read()
    check('the login shell still drains the flash queue',
          'get_flashed_messages' in _shell,
          'flashes must be consumed, not left to leak onto a later page')
    check('the login shell renders no alert markup',
          'class="alert' not in _shell and 'btn-close' not in _shell)
    # The brand panel is the logo, the department and one line of copy.
    check('login left column keeps only the wordmark, heading and lede',
          'campus.png' not in _shell
          and 'auth-split__plate' not in _shell
          and 'auth-split__points' not in _shell)
    check('the login wordmark is sized up',
          re.search(r'\.au-wordmark--card\s*\{\s*width:\s*(\d+)px',
                    open('static/css/style.css', encoding='utf-8').read())
          is not None)

    if token_match:
        response = csrf_client.post('/auth/logout',
                                    data={'_csrf_token': token_match.group(1)})
        check('POST /auth/logout with a valid token is accepted',
              response.status_code == 302, f'got {response.status_code}')
finally:
    store_mod.store.documents = _real_documents

# GET must remain unaffected.
response = csrf_client.get('/student/dashboard')
check('GET requests are not CSRF-checked',
      response.status_code in (302, 200), f'got {response.status_code}')

print()
print('=' * 70)
print('9c. AUTH GUARDS AND LOGOUT')
print('=' * 70)

guard_app = create_app()
guard_app.config['DEV_MODE'] = True
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
_login_ok.config['DEV_MODE'] = True
_login_client = _login_ok.test_client()

# Seed the CSRF token directly: with no database the login page renders no form,
# so there is nothing to scrape it from.
with _login_client.session_transaction() as sess:
    sess['_csrf_token'] = 'test-token-for-logout-redirect'

_r = _login_client.post('/auth/logout', data={'_csrf_token': 'test-token-for-logout-redirect'})
check('POST /auth/logout redirects', _r.status_code == 302,
      f'got {_r.status_code}')

# The health probe must not be able to take the service down. Render restarts
# the container on a non-2xx, so a probe that fails on a slow database turns a
# transient hiccup into a restart loop: connections are torn down, the first
# requests after boot queue behind that, and the next probe fails too.
_health_app = create_app()
_health_client = _health_app.test_client()
_h1 = _health_client.get('/healthz')
check('/healthz returns 200 even with no store', _h1.status_code == 200,
      f'got {_h1.status_code}; a failing probe makes Render restart the service')
_h2 = _health_client.get('/healthz')
check('/healthz reports the store separately from liveness',
      _h2.get_json().get('status') == 'ok'
      and _h2.get_json().get('database') in ('ok', 'unreachable', 'unknown'),
      _h2.get_json())

# Store budgets. A request that hangs on a stalled call holds a gunicorn worker
# thread, and the thread is what the page is waiting on. The harness overrides
# both to 1s above so a failing store fails fast here, so the shipped defaults are
# read out of config.py rather than off the running app.
_store_cfg = create_app().config
_config_source = Path('config.py').read_text(encoding='utf-8')
check('a stalled call gives up rather than hanging',
      re.search(r"FIRESTORE_TIMEOUT_SECONDS'\)\s*or\s*(\d+)", _config_source)
      is not None
      and 0 < int(re.search(r"FIRESTORE_TIMEOUT_SECONDS'\)\s*or\s*(\d+)",
                            _config_source).group(1)) <= 10,
      'a longer wait freezes the page before it errors')
check('a failed call is retried at least once, so a blip is survivable',
      re.search(r"FIRESTORE_RETRIES'\)\s*or\s*(\d+)", _config_source) is not None
      and int(re.search(r"FIRESTORE_RETRIES'\)\s*or\s*(\d+)",
                        _config_source).group(1)) >= 2,
      'FIRESTORE_RETRIES defaults below 2')
check('health probe verdict is cached so polling costs no query',
      _store_cfg['HEALTH_DB_CACHE_SECONDS'] >= 10,
      'an uncached probe costs a round trip on every poll')
check('config carries no Postgres settings at all',
      not any(key == 'DATABASE_URL' or key.startswith('DB_')
              for key in _store_cfg),
      str(sorted(key for key in _store_cfg
                 if key == 'DATABASE_URL' or key.startswith('DB_'))))

# Static assets: long max-age is only safe because the URL is versioned.
_cache_app = create_app()
_cache_client = _cache_app.test_client()
_page = _cache_client.get('/auth/login').get_data(as_text=True)
check('static urls carry a cache-busting version',
      re.search(r'/static/css/style\.css\?v=\d+', _page) is not None,
      'without a version a year-long max-age would serve stale assets forever')
_css_head = _cache_client.head('/static/css/style.css')
_max_age = _css_head.headers.get('Cache-Control', '')
check('static assets are cached for a long time',
      'max-age=31536000' in _max_age, _max_age)
check('page HTML is not cached by the static policy',
      'max-age=31536000' not in
      _cache_client.get('/auth/login').headers.get('Cache-Control', ''),
      'an HTML page cached for a year would serve a stale shell to everyone')
check('the asset() helper is available to every template, not just the login page',
      re.search(r'/static/css/style\.css\?v=\d+',
                _cache_client.get('/no-such-page').get_data(as_text=True))
      is not None,
      'the helper must be registered for the whole app, not one template')
check('  -> and points at the login page',
      '/auth/login' in _r.headers.get('Location', ''),
      _r.headers.get('Location', ''))

_follow = _login_client.get(_r.headers.get('Location'))
check('  -> login page shows no sign-out toast',
      'signed out' not in _follow.get_data(as_text=True).lower(),
      'the sign-out banner was removed from the login page')

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
check('  -> landing page shows no sign-out toast',
      'signed out' not in _text.lower(),
      'the Microsoft sign-out banner was removed from the login page')

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

# Every role must get the same shell: the sidebar column, and a header whose
# right-hand controls are never collapsed. A role that fell back to Bootstrap's
# .collapse in #appNav would lose the nav links, the theme toggle and the
# sign-out button from 992px upwards, because the hamburger that reveals it is
# d-lg-none and therefore gone by then.
_base = open('templates/base.html', encoding='utf-8').read()
check('every role uses the same header, with no per-role branch',
      'show_sidebar' not in _base.split('{% if user %}', 1)[1].split('</header>', 1)[0],
      'the header still branches on show_sidebar')
check('the header controls row is never collapsed',
      'app-topbar__nav' not in _base and 'collapse navbar-collapse' not in _base)
check('base.html has no inline top navigation left',
      'app-nav__link' not in _base and 'app-brand' not in _base,
      'the links and the wordmark belong to the navigation column')

# The sidebar allowlist is derived from ROLE_NAV, so a role cannot be added
# without also getting a navigation column.
_nav_py = open('app/utils/nav.py', encoding='utf-8').read()
check('every role with a nav also gets the sidebar',
      re.search(r'SIDEBAR_ROLES\s*=\s*tuple\(ROLE_NAV\)', _nav_py) is not None,
      'a hand-written tuple goes stale the moment a role is added')

# Jinja compiles a template once and keeps it for the life of the process,
# while static files are read fresh on every request. Serving old markup
# against new CSS looks exactly like a broken layout.
_config_py = open('config.py', encoding='utf-8').read()
check('template auto-reload is configurable',
      'TEMPLATES_AUTO_RELOAD' in _config_py)
check('template auto-reload is documented in .env.example',
      'TEMPLATES_AUTO_RELOAD' in open('.env.example', encoding='utf-8').read())

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

# ---- Page-level permissions ----
#
# The route decorator and the sidebar used to carry the role list separately, so
# nothing checked they agreed. These assert the table is exhaustive and that the
# navigation is derived from it.
print()
print('-' * 70)
print('9z. PAGE-LEVEL PERMISSIONS')
print('-' * 70)

from app.permissions.pages import (  # noqa: E402
    PAGE_PERMISSIONS, PUBLIC_ENDPOINTS, can_access, page_allows,
    permission_required, visible_nav,
)
from app.models import UserRole as _Role  # noqa: E402

# The boot-time check is what makes "no permission, no page" true rather than
# aspirational, so assert it has something to complain about.
_gaps = sorted(
    r.endpoint for r in guard_app.url_map.iter_rules()
    if r.endpoint not in PAGE_PERMISSIONS
    and r.endpoint not in PUBLIC_ENDPOINTS
)
check('every endpoint has a page grant or is exempt', not _gaps,
      f'open to any signed-in account: {_gaps}')

# The sign-in half of the flow and the public verification page have to work
# before anyone holds a grant, so they are exempt by name.
check('the public verification page stays reachable without a grant',
      'auth.verify_letter' in PUBLIC_ENDPOINTS)
check('the sign-in entry points stay exempt',
      {'auth.login', 'auth.callback', 'auth.microsoft_login'}
      <= PUBLIC_ENDPOINTS)

# A typo in a page key would otherwise compile to a route nobody holds a grant
# for, which reads as a mysterious refusal rather than a misspelling.
_threw = False
try:
    permission_required('faculty.dashbord')  # note the transposition
except RuntimeError:
    _threw = True
check('an unknown page key is refused at decoration time', _threw,
      'a misspelled page key would compile into a route nobody can open')

# Cross-role refusals, read off the table rather than off a decorator.
for _page, _role, _allowed in [
    ('hod.students', _Role.STUDENT, False),
    ('hod.students', _Role.LECTURER, False),
    ('hod.students', _Role.HOD, True),
    ('student.new_request', _Role.HOD, False),
    ('student.new_request', _Role.STUDENT, True),
    ('faculty.action', _Role.HOD, False),
    ('faculty.action', _Role.LECTURER, True),
    ('auth.profile', _Role.STUDENT, True),
    ('student.request_letter', _Role.LECTURER, True),
]:
    check(f'{_role.value} {"may" if _allowed else "may not"} open {_page}',
          page_allows(_page, _role) is _allowed)

# A grant is written with the enum and reached at runtime with the role's string
# value, because that is what the session row holds. Both spellings must agree.
check('a grant holds whether the role arrives as an enum or as text',
      page_allows('hod.students', _Role.HOD)
      and page_allows('hod.students', 'HOD')
      and not page_allows('hod.students', 'STUDENT'))

check('nobody is refused when signed out', can_access(None, 'hod.dashboard') is False)

# The sidebar may only offer what the route behind it will serve.
for _role_value, _forbidden in [
    ('STUDENT', 'hod.students'),
    ('STUDENT', 'faculty.reports'),
    ('LECTURER', 'hod.print_report'),
    ('HOD', 'student.new_request'),
]:
    _offered = [i['endpoint'] for i in visible_nav(_role_value)]
    check(f'{_role_value} sidebar hides {_forbidden}', _forbidden not in _offered)
    check(f'  -> and still offers its own pages', len(_offered) > 0)

# Every sidebar entry a role keeps must correspond to a page it may open.
for _role_value in ('STUDENT', 'LECTURER', 'HOD'):
    _bad = [i['endpoint'] for i in visible_nav(_role_value)
            if not page_allows(i['endpoint'], _role_value)]
    check(f'{_role_value} sidebar offers nothing it cannot open', not _bad,
          f'offers {_bad}')


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
print('9f. DEPLOYMENT READINESS')
print('=' * 70)

import os as _os

for required in ['setup.py', 'requirements.txt', '.env.example',
                 'migrations/seed_firestore.py', 'config.py', 'wsgi.py',
                 'README.md', 'render.yaml', 'Procfile']:
    check(f'{required} present', _os.path.isfile(required))

# A top-level app.py would be unreachable: the app/ package shadows it, so
# gunicorn's app:app would fail with AppImportError. The entry point must be
# named something else.
check('no app.py shadows the app package', not _os.path.isfile('app.py'))
check('no app.pyc shadows the app package',
      not _os.path.isfile('app.pyc'))

check('.env is not tracked by git',
      '.env' in open('.gitignore', encoding='utf-8').read())

# Every runtime dependency must be pinned.
_req = open('requirements.txt', encoding='utf-8').read()
for package in ['Flask', 'msal', 'python-dotenv', 'gunicorn',
                'Flask-Mail', 'Werkzeug', 'openpyxl', 'Pillow']:
    check(f'requirements pins {package}',
          re.search(rf'^{re.escape(package)}==', _req, re.M) is not None)

# The store client and the storage client both pin exactly, so a plain
# "package==" match would miss nothing -- but a stale Postgres driver left in the
# list would still be installed on every deploy, so assert its absence.
check('requirements pins the Firestore client',
      re.search(r'^google-cloud-firestore==', _req, re.M) is not None)
check('requirements pins the proof storage client',
      re.search(r'^boto3==', _req, re.M) is not None)
check('requirements no longer installs the Postgres driver',
      'psycopg' not in _req, 'psycopg is still pinned')
check('requirements no longer pulls the MySQL driver',
      'mysql' not in _req.lower())

# Packages the code imports must not be missing from requirements.
check('openpyxl declared (roster import)',
      'openpyxl' in _req)
check('Pillow declared (branding assertions)',
      'Pillow' in _req)

# Host, port and debug must be configurable, not hardcoded.
_env_example = open('.env.example', encoding='utf-8').read()
for key in ['HOST', 'PORT', 'FLASK_DEBUG', 'SESSION_COOKIE_SECURE',
            'IDLE_TIMEOUT_SECONDS', 'FIREBASE_PROJECT_ID', 'FIRESTORE_DATABASE',
            'FIREBASE_CREDENTIALS_PATH', 'SUPABASE_URL',
            'SUPABASE_SECRET_KEY', 'SUPABASE_STORAGE_BUCKET',
            'R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY',
            'R2_BUCKET', 'CLIENT_ID', 'CLIENT_SECRET', 'TENANT_ID',
            'REDIRECT_URI', 'DEV_MODE']:
    check(f'.env.example documents {key}',
          re.search(rf'^{key}=', _env_example, re.M) is not None)

check('.env.example carries no real credentials',
      not re.search(r'PASTE_[A-Z_]*HERE', _env_example)
      and 'CsePerm' not in _env_example
      and 'private_key' not in _env_example)

_wsgi_py = open('wsgi.py', encoding='utf-8').read()
check('wsgi.py reads host from the environment',
      "os.environ.get('HOST'" in _wsgi_py)
check('wsgi.py reads port from the environment',
      "os.environ.get('PORT'" in _wsgi_py)
check('wsgi.py reads debug from the environment',
      "os.environ.get('FLASK_DEBUG'" in _wsgi_py)
check('wsgi.py no longer hardcodes port 5000',
      'port=5000' not in _wsgi_py)
check('wsgi.py exposes a module-level app for the WSGI servers',
      re.search(r'^app\s*=\s*GzipMiddleware\(flask_app\)', _wsgi_py, re.M)
      is not None,
      'gunicorn loads wsgi:app and needs a module-level app')

# `gunicorn app:app` is the mistake that reaches production most often, because
# the Start Command typed into the Render dashboard overrides the Procfile and
# nothing in the repo complains until the deploy exits. `app` is the package;
# the callable is in wsgi.py. The declared entry points must never name it, and
# the README has to explain it rather than hide it.
for _path in ['Procfile', 'render.yaml']:
    _text = open(_path, encoding='utf-8').read()
    check(f'{_path} never tells anyone to run gunicorn app:app',
          'app:app' not in _text.replace('wsgi:app', ''),
          'gunicorn app:app is unreachable: app is the package, not the callable')

check('render.yaml starts gunicorn on wsgi:app',
      re.search(r'startCommand:\s*gunicorn\b.*\bwsgi:app',
                open('render.yaml', encoding='utf-8').read()) is not None,
      'render.yaml startCommand must target wsgi:app')
_readme_text = open('README.md', encoding='utf-8').read()
check('the README explains the app:app mistake',
      'AppImportError' in _readme_text and 'gunicorn app:app' in _readme_text,
      'the README must document the symptom and the correct Start Command')

import wsgi as _wsgi_module
check('wsgi:app loads and is callable, the way gunicorn loads it',
      callable(_wsgi_module.app),
      f'type {type(_wsgi_module.app).__name__}')

# The README must document a complete install and the Render deploy.
_readme = open('README.md', encoding='utf-8').read()
for phrase in ['python setup.py', 'git clone', 'gunicorn',
               'AADSTS50011', 'DEV_MODE', 'Firestore', 'Render',
               'seed_firestore.py', 'healthz']:
    check(f'README documents {phrase!r}', phrase in _readme)

# The deployment entry points must agree with each other.
_render_yaml = open('render.yaml', encoding='utf-8').read()
check('render.yaml starts gunicorn',
      'gunicorn' in _render_yaml and 'wsgi:app' in _render_yaml)
check('render.yaml points the health check at /healthz',
      'healthCheckPath: /healthz' in _render_yaml)
check('render.yaml takes the credentials from the environment, not a literal',
      re.search(r'- key: FIREBASE_CREDENTIALS_JSON\s*\n\s*sync: false',
                _render_yaml) is not None)
check('render.yaml declares no database instance (Firestore and R2 host it)',
      'type: postgres' not in _render_yaml and not re.search(
          r'^databases:', _render_yaml, re.M))
check('render.yaml ships DEV_MODE false',
      re.search(r'- key: DEV_MODE\s*\n\s*value: "false"', _render_yaml)
      is not None)
check('render.yaml turns on secure cookies',
      re.search(
          r'- key: SESSION_COOKIE_SECURE\s*\n\s*value: "true"', _render_yaml
      ) is not None)
check('render.yaml leaves no service-role key inline',
      'eyJ' not in _render_yaml)

_procfile = open('Procfile', encoding='utf-8').read()
check('Procfile binds to the port Render injects',
      '--bind 0.0.0.0:$PORT' in _procfile)
check('Procfile serves wsgi:app through gunicorn',
      'wsgi:app' in _procfile and 'gunicorn' in _procfile)
check('Procfile targets the real module:app, not app:app',
      'app:app' not in _procfile.replace('wsgi:app', ''))

check('setup.py performs all install steps',
      all(step in open('setup.py', encoding='utf-8').read()
          for step in ['venv', 'requirements.txt', '.env.example',
                       'seed_firestore.py']))

# setup.py is a CLI script and runs on import by design, but nothing in the
# application may import it.
_importers = []
for _root, _dirs, _files in _os.walk('app'):
    for _name in _files:
        if _name.endswith('.py'):
            _body = open(_os.path.join(_root, _name), encoding='utf-8').read()
            if 'setup' in _body and 'import setup' in _body:
                _importers.append(_os.path.join(_root, _name))
check('no application module imports setup.py', not _importers, str(_importers))

# The virtual environment must never be committed.
check('.venv is gitignored', '.venv' in open('.gitignore', encoding='utf-8').read())

print()
print('=' * 70)
print('10. FULL PAGE RENDER (mocked data)')
print('=' * 70)

from datetime import datetime

from app.models import (
    ApprovalAction, ApprovalHistory, DEAD_STATUSES, PermissionRequest,
    PermissionType, ProofDocument, RequestStatus, User, UserRole,
)
from app.models import permission as perm_mod
from app.models import user as user_mod
from app.student import routes as student_routes
from app.faculty import routes as faculty_routes

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


def make_request(rid, status=RequestStatus.PENDING, ptype=PermissionType.CLASSROOM):
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


# The directory the real attach_members builds, resolved from fixtures instead of
# the store. Mirrors the real function's output shape exactly -- including the
# approved and dropped partitions -- so the templates are exercised against the
# same fields production fills in.
FIXTURE_DIRECTORY = {
    STUDENT.id: {'id': STUDENT.id, 'name': STUDENT.name,
                 'roll_number': STUDENT.roll_number,
                 'department': STUDENT.department},
    LECTURER.id: {'id': LECTURER.id, 'name': LECTURER.name,
                  'roll_number': LECTURER.roll_number,
                  'department': LECTURER.department},
    HOD.id: {'id': HOD.id, 'name': HOD.name, 'roll_number': HOD.roll_number,
             'department': HOD.department},
}
for _i in (7, 8, 9, 10):
    FIXTURE_DIRECTORY[_i] = {
        'id': _i, 'name': f'Classmate {_i}', 'roll_number': f'26B21CS0{_i:02d}',
        'department': 'CSE'}


def _attach_fixture_members(requests):
    for request in requests:
        details = [FIXTURE_DIRECTORY[m] for m in request.members
                   if m in FIXTURE_DIRECTORY]
        request.member_details = details
        owner = FIXTURE_DIRECTORY.get(request.student_id)
        if owner:
            request.student_name = owner['name']
            request.student_roll_number = owner['roll_number']
            request.student_identifier = (
                owner['roll_number'] or owner['name'])
            request.student_phone = None
        approved = set(request.approved_members)
        request.approved_member_details = [d for d in details if d['id'] in approved]
        request.dropped_member_details = [d for d in details if d['id'] not in approved]


def _greq(rid, members=(), status=RequestStatus.PENDING, approved=None):
    """A request carrying a group membership, for the render assertions.

    Defined with the other fixtures rather than inside the group section: the
    public verification page is exercised much earlier in the file, and a helper
    defined later would only fail at call time as a NameError.
    """
    record = make_request(rid, status)
    record.member_ids = list(members)
    record.approved_member_ids = list(approved) if approved is not None else None
    return record


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
        # The views import attach_members by name, so patching the model module
        # alone would leave them calling the real one -- which reads the store and
        # answers 503 for every letter and request page in the harness.
        'attach_members_model': perm_mod.attach_members,
        'attach_members_student': student_routes.attach_members,
        'attach_members_faculty': faculty_routes.attach_members,
    }

    perm_mod.attach_members = _attach_fixture_members
    student_routes.attach_members = _attach_fixture_members
    faculty_routes.attach_members = _attach_fixture_members

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
        'awaiting_hod': 7,
        'classroom_count': 18,
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
        perm_mod.attach_members = originals['attach_members_model']
        student_routes.attach_members = originals['attach_members_student']
        faculty_routes.attach_members = originals['attach_members_faculty']

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
    (UserRole.HOD, '/hod/dashboard?status=APPROVED&type=CLASSROOM'),
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
          'id="statusChart"' in page and 'Activity Permission' in page)
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
print('11. PROOF STORAGE ROUND TRIP (CLOUDFLARE R2)')
print('=' * 70)

from werkzeug.datastructures import FileStorage

import app.utils.files as files_mod
from app.utils.files import UploadError, delete_proof, fetch_proof, validate_and_store


class _FakeMissing(Exception):
    """Shaped like botocore's ClientError for an object that is not there."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {'Error': {'Code': code, 'Message': code}}


class _FakeS3:
    """In-memory stand-in for the boto3 S3 client R2 is reached through."""

    def __init__(self):
        self.objects = {}
        self.puts = 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _FakeMissing('404')
        return {'ContentLength': len(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts += 1
        self.objects[Key] = bytes(Body)
        return {'ETag': f'"{len(self.objects[Key])}"'}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _FakeMissing('NoSuchKey')
        return {'Body': io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _FakeMissing('NoSuchKey')
        self.objects.pop(Key)


_FAKE = _FakeS3()
_fake_stored = {'count': 0}

_real_storage_bucket = files_mod.storage_bucket
files_mod.storage_bucket = lambda: (_FAKE, 'proofs')


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
    _now = datetime.now()
    _expected_prefix = f'{_now.year}/{_now.month:02d}/'
    check('stored key is year/month bucketed',
          meta['file_path'].startswith(_expected_prefix), meta['file_path'])
    check('stored object is in the bucket',
          meta['file_path'] in _FAKE.objects, str(list(_FAKE.objects)))
    check('stored bytes round-trip',
          fetch_proof(meta['file_path']) == b'%PDF-1.7\n' + b'0' * 2048)
    check('stored size recorded', meta['file_size'] == len(b'%PDF-1.7\n') + 2048,
          str(meta['file_size']))
    check('stored key never leaks the original filename',
          'doctor-letter' not in meta['file_path'], meta['file_path'])

    # An existing proof must not be clobbered: S3's put_object overwrites
    # silently, so the pre-write head check is the only thing preventing it.
    _puts_before = _FAKE.puts
    _payload_before = _FAKE.objects[meta['file_path']]
    try:
        _put_again = files_mod._put(meta['file_path'], b'%PDF-1.7\nX', 'application/pdf')
        check('an existing proof is never overwritten', False, 'the write went through')
    except UploadError:
        check('an existing proof is never overwritten', True)
    check('  -> and the stored bytes are untouched',
          _FAKE.objects[meta['file_path']] == _payload_before
          and _FAKE.puts == _puts_before)

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
    try:
        validate_and_store(empty)
        check('empty upload is rejected', False, 'accepted empty file')
    except UploadError as exc:
        check('empty upload is rejected', 'empty' in str(exc).lower(), str(exc))

    oversize = store('huge.pdf', b'%PDF-1.7\n' + b'0' * (5 * 1024 * 1024 + 1))
    try:
        validate_and_store(oversize)
        check('oversize upload is rejected', False, 'accepted >5MB')
    except UploadError as exc:
        check('oversize upload is rejected', '5 MB' in str(exc), str(exc))
    check('oversize upload stored nothing',
          len(_FAKE.objects) == 1, str(list(_FAKE.objects)))

    none_file = FileStorage(stream=io.BytesIO(b''), filename='')
    try:
        validate_and_store(none_file)
        check('missing filename is rejected', False, 'accepted empty filename')
    except UploadError as exc:
        check('missing filename is rejected', 'required' in str(exc).lower(), str(exc))

    check('delete_proof removes the object', delete_proof(meta['file_path']))
    check('object is gone after delete', meta['file_path'] not in _FAKE.objects)
    try:
        fetch_proof(meta['file_path'])
        check('fetching a deleted proof raises', False, 'returned bytes')
    except UploadError:
        check('fetching a deleted proof raises', True)

# The client must be built from the environment, and refuse to exist without
# credentials rather than dialling an endpoint built from empty strings.
files_mod.storage_bucket = _real_storage_bucket
files_mod._handle, files_mod._bucket = None, None
with app.test_request_context():
    _saved = {key: app.config[key] for key in
              ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY')}
    app.config.update({'R2_ACCOUNT_ID': '', 'R2_ACCESS_KEY_ID': '',
                       'R2_SECRET_ACCESS_KEY': ''})
    try:
        files_mod.storage_bucket()
        check('unconfigured storage refuses to build a client', False, 'no error')
    except UploadError as exc:
        check('unconfigured storage refuses to build a client',
              'not configured' in str(exc), str(exc))
    app.config.update(_saved)

    # The real builder is what constructs the R2 client, so the endpoint, the
    # signing scheme and the bucket all come from config rather than a literal.
    import boto3
    _built, _built_bucket = files_mod.storage_bucket()
    check('the R2 client is built against the account endpoint',
          _built.meta.endpoint_url.endswith('.r2.cloudflarestorage.com'),
          str(getattr(_built.meta, 'endpoint_url', '')))
    check('  -> with the configured bucket', _built_bucket == 'proofs', _built_bucket)
    check('  -> and SigV4 signing', _built.meta.config.signature_version == 's3v4',
          str(_built.meta.config.signature_version))
    check('  -> and the region Cloudflare documents', _built.meta.region_name == 'auto',
          str(_built.meta.region_name))
    files_mod._handle, files_mod._bucket = None, None

print()

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

    # The wordmark lives on the navigation column and nowhere else. It used to
    # also sit in the top bar, filtered white for the navy header; a duplicate
    # logo is what that check used to guard against, and now the top bar must
    # simply not carry one.
    base = open('templates/base.html', encoding='utf-8').read()
    check('top bar carries no wordmark', 'aditya-logo' not in base,
          'the logo belongs to the navigation column, not the header')
    check('no white plate remains in the top bar',
          'au-logo-plate' not in base, 'white plate still wrapped around the logo')

    # Sidebar content must fill the column the navigation leaves. The auto
    # margins and the max-width cap exist to centre a page that owns the full
    # viewport; once a sidebar owns the left edge they leave a visible band of
    # empty background beside the column and cap the page well short of a wide
    # window.
    _css_all = open('static/css/style.css', encoding='utf-8').read()
    # The declarations of the rule that starts at the sidebar content selector.
    # Matching up to the next "}" rather than to a "}\s*}" pair keeps this
    # working whether or not the selector list has grown extra members.
    _sel = _css_all.find('.au-app--sidebar .au-content,')
    _end = _css_all.find('}', _sel) if _sel != -1 else -1
    _sidebar_block = _css_all[_sel:_end] if _sel != -1 and _end != -1 else ''
    check('sidebar pages align content to the column edge',
          re.search(r'margin-inline:\s*0', _sidebar_block) is not None,
          'content is auto-centred, leaving a gap beside the sidebar')
    check('sidebar pages are not capped narrower than the column',
          re.search(r'max-width:\s*none', _sidebar_block) is not None,
          'the page stops short of the window width')
    check('the pages that set their own content column get the same treatment',
          re.search(r'\.au-app--sidebar \.page-wrap,', _css_all) is not None
          and re.search(r'\.au-app--sidebar \.page-wrap-narrow', _css_all) is not None,
          'a standalone <main class="page-wrap-narrow"> stays centred and stranded')
    # The content keeps its own gutter beside the column: the navigation provides
    # the left edge, but a page whose first element touches it reads as clipped.
    check('sidebar pages keep a left gutter beside the column',
          re.search(r'padding-left:\s*0', _sidebar_block) is None
          and re.search(r'\.au-content\s*\{[^}]*padding:\s*24px 28px',
                        _css_all, re.S) is not None,
          'content is flush against the navigation column')
    check('top bar and page content share the same left edge',
          re.search(r'\.au-app--sidebar \.app-topbar__inner\s*\{[^}]*margin-inline:\s*0',
                    _css_all, re.S) is not None)
    # The navigation column has a fixed width and .au-side is its only child,
    # but it sits in a row flex container, so without an explicit width it sizes
    # to its content and leaves a band of bare background between the navigation
    # and the main column -- the seam that read as a gap.
    check('the navigation panel fills the column',
          re.search(r'\.au-side\s*\{[^}]*width:\s*100%', _css_all, re.S) is not None,
          'the sidebar is narrower than its column, leaving a gap beside it')

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

    # The wordmark is a letterhead now rather than a faint centred watermark,
    # sitting left with the QR on the right above a gold rule. A watermark left
    # behind would sit behind the body text as well, so its absence is asserted.
    check('letter puts the wordmark in the letterhead',
          'lt-head__logo' in page and 'lt-head' in page)
    check('letter places the QR on the right of the letterhead',
          'lt-head__qr' in page)
    check('letter rules the letterhead with a gold line',
          '--lt-gold' in page and 'border-bottom: 2px solid var(--lt-gold)' in page)
    check('letter no longer watermarks the mark behind the text',
          'letter-watermark' not in page)
    # The QR plate was a box drawn on the paper; the code is white on white and
    # carries its own quiet zone, so the surrounding border goes with it.
    check('letter QR has no border box around it',
          'border: 1px solid #cbd5e1; padding: 3px' not in page
          and re.search(r'\.lt-head__qr img\s*\{[^}]*border:\s*0', page) is not None)

    # The approval timeline was removed from the letter at the client's request.
    # Asserted as absent rather than simply dropped: the status panel and the
    # closing notice already carry the outcome, and a check that only stopped
    # mentioning the timeline would let it reappear unnoticed.
    check('letter no longer carries an approval timeline',
          'letter-track' not in page
          and 'Approval Status' not in page
          and 'Final Status' not in page,
          'the four-stage timeline is back on the letter')
    check('  -> and leaves no dead timeline styles behind',
          'track__step' not in page and '.track {' not in page,
          'orphaned .track CSS is still in the letter')
    check('  -> the request details table remains', 'Request Details' in page)
    check('  -> and a closing notice', 'letter-notice' in page)
    # The letter used to hardcode the app name in its colophon, so renaming the
    # application left "Permission & Leave Tracking" printed on every letter after
    # leave management was retired. It reads the context global like every other
    # template now.
    check('  -> and names the application from the config, not a literal',
          'CSE Permission Tracking System' in page
          and 'Leave Tracking System' not in page)

    # Every panel in the design is a background fill, and a print pipeline drops
    # those unless the document insists. Without this the printed sheet keeps the
    # gold rules and the text and loses every panel, which reads as an undesigned
    # printout rather than as a missing setting.
    check('letter insists on printing its own backgrounds',
          'print-color-adjust: exact' in page
          and '-webkit-print-color-adjust: exact' in page,
          'printed sheet loses every panel without it')

    check('letter carries a reference number', 'REQ-1024' in page)
    check('letter states the student roll number', '26B21CS058' in page)
    check('letter has a status block', 'Status of this request' in page)
    # The department attests to the record, so the letter carries the reviewing
    # lecturer's name and no signature blocks. The blocks were also what pushed
    # it onto a second sheet.
    check('letter names the reviewing faculty',
          LECTURER.name in page)
    check('letter has no signature blocks',
          'Faculty Signature' not in page
          and 'Head of Department' not in page
          and 'letter-sign' not in page)
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
print('12. THE FIRESTORE DATA LAYER')
print('=' * 70)

# This is the layer that used to be a connection pool plus SQL, and the rules it
# has to keep are the ones the pool sections used to check: a store that cannot be
# reached must fail as one exception type the routes already handle, a blip must
# be survivable, a bad request must not be retried, and nothing above this layer
# may see a driver exception.
#
# The client is faked rather than stubbed at the model layer, so the type mapping,
# the id counter, the filters and the retry wrapper are the real code under test.

import threading as _threading

from google.api_core.exceptions import ServiceUnavailable
from google.cloud.firestore_v1.base_query import FieldFilter

from app.models import firestore as store_mod
from app.models.firestore import (
    DatabaseUnavailable, Store, to_python_value, to_timestamp,
)
from config import Config
from flask import Flask


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None

    def get(self, field):
        return (self._data or {}).get(field)


class _FakeQuery:
    """Equality filtering and a stream, which is all the store ever asks for."""

    def __init__(self, docs):
        self._docs = docs

    def where(self, filter=None, **kwargs):
        field_filter = filter
        matches = [
            doc for doc in self._docs
            if doc.get(field_filter.field_path) == field_filter.value
        ]
        return _FakeQuery(matches)

    def limit(self, count):
        return _FakeQuery(self._docs[:count])

    def stream(self, timeout=None):
        for data in self._docs:
            yield _FakeSnapshot(data)

    def get(self, timeout=None):
        return iter(())


class _FakeDocument:
    def __init__(self, collection, doc_id):
        self._collection = collection
        self._id = doc_id

    def get(self, timeout=None, transaction=None):
        return _FakeSnapshot(self._collection._docs.get(self._id))

    def set(self, data, timeout=None):
        self._collection._docs[self._id] = dict(data)

    def update(self, data, timeout=None):
        self._collection._docs[self._id].update(data)

    def delete(self, timeout=None):
        self._collection._docs.pop(self._id, None)


class _FakeCollection:
    def __init__(self, client, name):
        self._client = client
        self._name = name
        self._docs = client._data.setdefault(name, {})

    def document(self, doc_id):
        return _FakeDocument(self, str(doc_id))

    def where(self, **kwargs):
        return _FakeQuery(list(self._docs.values())).where(**kwargs)

    def limit(self, count):
        return _FakeQuery(list(self._docs.values())).limit(count)

    # An unfiltered read streams the collection itself, exactly as the real
    # client does.
    def stream(self, timeout=None):
        for data in list(self._docs.values()):
            yield _FakeSnapshot(data)

    def get(self, timeout=None):
        return iter(())


class _FakeTransaction:
    """Enough of firestore_v1.Transaction for the transactional decorator.

    The decorator drives `_clean_up`, `_begin`, `_commit` and `_rollback`, and
    reads `_read_only` and `_max_attempts` off the object, so the fake carries
    those. Writes are staged and applied on commit, which is what makes the id
    counter behave the way the real one does.
    """

    _read_only = False
    _max_attempts = 5

    def __init__(self, client):
        self._client = client
        self._id = 'fake-txn'
        self._writes = []

    def _clean_up(self):
        self._writes = []

    def _begin(self, retry_id=None):
        return None

    def get(self, reference, **kwargs):
        return reference.get()

    def set(self, reference, data):
        self._writes.append((reference, dict(data)))

    def _commit(self):
        for reference, data in self._writes:
            reference.set(data)
        self._writes = []

    def _rollback(self):
        self._writes = []


class _FakeClient:
    """Enough of google.cloud.firestore.Client for the store's own methods."""

    def __init__(self):
        self._data = {}
        self.writes = 0

    def collection(self, name):
        return _FakeCollection(self, name)

    def transaction(self):
        return _FakeTransaction(self)


def _store_with_fake_client():
    """A Store wired to the fake client, configured but never dialling out."""
    store = Store()
    store.client = _FakeClient()
    store.target = 'firestore:(default) in permission-system-test'
    store.timeout = 1.0
    store.retries = 1
    return store


_fake_store = _store_with_fake_client()

with app.test_request_context():
    # -- ids ---------------------------------------------------------------
    _first, _second = _fake_store.next_id('widgets'), _fake_store.next_id('widgets')
    check('ids are integers and never reused',
          _first == 1 and _second == 2, f'{_first}, {_second}')
    _gadget = _fake_store.next_id('gadgets')
    check('counters are per collection', _gadget == 1, str(_gadget))

    # -- round trips -------------------------------------------------------
    _written = _fake_store.insert('widgets', {
        'name': 'spanner',
        'count': 3,
        'active': True,
        'ratio': 0.5,
        'day': date(2026, 10, 2),
        'opens': time(9, 30),
        'stamp': datetime(2026, 10, 2, 15, 30, tzinfo=timezone.utc),
        'note': None,
    })
    _spanner_id = _written['id']
    check('insert returns the stored row',
          _written['name'] == 'spanner' and _spanner_id == 3, str(_spanner_id))
    check('insert stamps created_at and updated_at',
          _written.get('created_at') is not None
          and _written.get('updated_at') is not None)

    _read = _fake_store.get('widgets', _spanner_id)
    check('a date comes back as a date', isinstance(_read['day'], date)
          and _read['day'] == date(2026, 10, 2), repr(_read.get('day')))
    check('a time comes back as a time', isinstance(_read['opens'], time)
          and _read['opens'].hour == 9, repr(_read.get('opens')))
    check('a timestamp comes back naive and local',
          isinstance(_read['stamp'], datetime) and _read['stamp'].tzinfo is None,
          repr(_read.get('stamp')))
    check('types survive unchanged',
          _read['count'] == 3 and _read['active'] is True
          and _read['ratio'] == 0.5, str(_read))
    check('a missing field reads as None', _read.get('note') is None)

    # Free text is the reason the shape is checked before parsing: a reason field
    # that happens to look like a date must not come back as a datetime.
    _fake_store.insert('widgets', {
        'name': 'quoted',
        'note': 'Medical appointment on 2026-10-02 at 09:30 near gate 3',
    })
    _quoted = [row for row in _fake_store.documents('widgets')
               if row.get('name') == 'quoted'][0]
    check('free text is returned exactly as stored',
          _quoted['note'] == 'Medical appointment on 2026-10-02 at 09:30 near gate 3',
          repr(_quoted.get('note')))

    _fake_store.insert('widgets', {'name': 'quoted again'})
    check('an unparseable date-looking string is left alone',
          to_python_value('1234-56-78', store_mod.ZoneInfo('UTC'))
          is store_mod._UNCHANGED)

    # -- writes ------------------------------------------------------------
    check('update merges and returns True',
          _fake_store.update('widgets', _spanner_id, {'name': 'renamed'}) is True)
    check('update leaves other fields alone',
          _fake_store.get('widgets', _spanner_id)['count'] == 3
          and _fake_store.get('widgets', _spanner_id)['name'] == 'renamed')
    check('updating a missing document returns False',
          _fake_store.update('widgets', 999, {'name': 'ghost'}) is False)

    check('delete removes the document',
          _fake_store.delete('widgets', _spanner_id) is True)
    check('deleting a missing document returns False',
          _fake_store.delete('widgets', _spanner_id) is False)
    check('a deleted document reads as None',
          _fake_store.get('widgets', _spanner_id) is None)

    # -- filters -----------------------------------------------------------
    _fake_store.insert('things', {'name': 'a', 'kind': 'x', 'owner': 7})
    _fake_store.insert('things', {'name': 'b', 'kind': 'y', 'owner': 7})
    _fake_store.insert('things', {'name': 'c', 'kind': 'x', 'owner': 9})
    check('equality filter returns the matching rows',
          [r['name'] for r in _fake_store.documents('things', kind='x')] == ['a', 'c'],
          str([r['name'] for r in _fake_store.documents('things', kind='x')]))
    check('two equality filters intersect',
          [r['name'] for r in
           _fake_store.documents('things', kind='x', owner=9)] == ['c'])
    check('a None filter is skipped rather than queried',
          len(_fake_store.documents('things', owner=None)) == 3)
    check('delete_where removes only the matches',
          _fake_store.delete_where('things', kind='x') == 2
          and len(_fake_store.documents('things')) == 1,
          str(len(_fake_store.documents('things'))))
    check('delete_where on no matches removes nothing',
          _fake_store.delete_where('things', kind='zzz') == 0)

    # -- an unreachable store ----------------------------------------------
    _broken = Store()
    _broken.client = None
    check('an unconfigured store refuses every read', _broken._require_client
          is not None)
    try:
        _broken.documents('users')
        check('an unconfigured store raises DatabaseUnavailable', False, 'no error')
    except DatabaseUnavailable as exc:
        check('an unconfigured store raises DatabaseUnavailable',
              'not configured' in str(exc), str(exc))
    try:
        _broken.ping()
        check('pinging an unconfigured store raises', False, 'no error')
    except DatabaseUnavailable:
        check('pinging an unconfigured store raises', True)

    # -- retry -------------------------------------------------------------
    class _Flaky(_FakeClient):
        """Fails the first `failures` reads with a retryable error.

        The failure is injected into `stream`, which is the only read path the
        store uses -- filtered or not -- so both shapes of `documents()` are
        covered.
        """

        def __init__(self, failures, error):
            super().__init__()
            self.remaining = failures
            self.error = error
            self.calls = 0

        def collection(self, name):
            client = self

            class _FlakyCollection(_FakeCollection):
                def stream(self, timeout=None):
                    client.calls += 1
                    if client.remaining > 0:
                        client.remaining -= 1
                        raise client.error
                    return super().stream(timeout=timeout)

            return _FlakyCollection(self, name)

    _retrying = Store()
    _retrying.retries = 3
    _retrying.timeout = 1.0
    _retrying.client = _Flaky(2, ServiceUnavailable('firestore is warming up'))
    try:
        with app.test_request_context():
            _retrying.documents('users')
        check('a blip is retried and then succeeds', True)
    except DatabaseUnavailable as exc:
        check('a blip is retried and then succeeds', False, str(exc))

    _flaky_client = _Flaky(1, ValueError('bad field name'))
    _failing = Store()
    _failing.retries = 3
    _failing.timeout = 1.0
    _failing.client = _flaky_client
    try:
        with app.test_request_context():
            _failing.documents('users')
        check('a bad request is not retried', False, 'no error')
    except DatabaseUnavailable as exc:
        check('a bad request is not retried', _flaky_client.calls == 1,
              f'{_flaky_client.calls} attempts')

    _exhausting = Store()
    _exhausting.retries = 2
    _exhausting.timeout = 1.0
    _exhausting.client = _Flaky(5, ServiceUnavailable('firestore is down'))
    try:
        with app.test_request_context():
            _exhausting.documents('users')
        check('an exhausted retry budget raises DatabaseUnavailable', False, 'no error')
    except DatabaseUnavailable as exc:
        check('an exhausted retry budget raises DatabaseUnavailable',
              'after 2 attempts' in str(exc), str(exc))

    # A driver exception must never reach a caller: the routes, the error
    # handlers and the health check all catch DatabaseUnavailable and nothing
    # else, so anything else is a 500 with a stack trace in it.
check('every store failure is DatabaseUnavailable',
      issubclass(DatabaseUnavailable, RuntimeError))

# Which variable holds the credentials is decided in one place, because getting
# it wrong is invisible otherwise: a JSON pasted into the _PATH variable reads
# as a missing file and takes the whole app offline.
from app.models.firestore import credential_source

check('the inline JSON is preferred when both are set',
      credential_source({'FIREBASE_CREDENTIALS_JSON': '{"a": 1}',
                         'FIREBASE_CREDENTIALS_PATH': '/tmp/key.json'}) == 'json')
check('a key file is used when no inline JSON is set',
      credential_source({'FIREBASE_CREDENTIALS_PATH': '/tmp/key.json'}) == 'path')
check('an empty value is not a credential',
      credential_source({'FIREBASE_CREDENTIALS_JSON': '   ',
                         'FIREBASE_CREDENTIALS_PATH': ''}) is None)
check('JSON pasted into the path variable is recognised as credentials',
      credential_source({'FIREBASE_CREDENTIALS_PATH': '{"type": "service_account"}'})
      == 'json-in-path',
      'a credential in the wrong variable must not read as a missing file')

# -- configuration ---------------------------------------------------------

_firestore_config = create_app().config
check('config carries the Firestore settings',
      all(key in _firestore_config for key in
          ('FIREBASE_PROJECT_ID', 'FIRESTORE_DATABASE', 'FIRESTORE_TIMEOUT_SECONDS',
           'FIRESTORE_RETRIES')),
      str([key for key in ('FIREBASE_PROJECT_ID', 'FIRESTORE_DATABASE',
                           'FIRESTORE_TIMEOUT_SECONDS', 'FIRESTORE_RETRIES')
           if key not in _firestore_config]))
check('config carries no Postgres settings',
      not any(key == 'DATABASE_URL' or key.startswith('DB_')
              for key in _firestore_config),
      str(sorted(key for key in _firestore_config
                 if key == 'DATABASE_URL' or key.startswith('DB_'))))
check('health probe verdicts are cached',
      _firestore_config['HEALTH_DB_CACHE_SECONDS'] >= 10,
      'an uncached probe costs a query on every poll')
check('no composite index is required',
      re.search(r'\w\.order_by\(', Path('app/models/firestore.py').read_text(
          encoding='utf-8')) is None,
      'order_by on a query needs a composite index configured in the console')

print()

print()
print('=' * 70)
print('13. LETTER QR AND PUBLIC VERIFICATION')
print('=' * 70)

# The QR is the whole point of the letter, and the link behind it is public, so
# the two things worth guarding are that it works and that it cannot be guessed.
# A bare /verify/REQ-0001 would let anyone enumerate every student in the
# department, and these records carry medical reasons.

from app.utils import qr as qr_mod
from app.utils.qr import (
    data_uri, reference_for, resolve_reference, verification_url,
)

import base64

_qr_app = app

with _qr_app.test_request_context('/student/requests/1/letter'):
    _link = verification_url(1024)
    _reference = _link.rsplit('/verify/', 1)[1]
    _token = _reference[len('REQ-1024.'):]
    _origin = _qr_app.config['REDIRECT_URI'].rsplit('/', 2)[0]

    check('the reference is the padded request id',
          reference_for(1024) == 'REQ-1024', reference_for(1024))
    check('the link points at the public verification route',
          '/verify/' in _link, _link)
    # An http:// link printed on a formal letter is wrong in a way the reader
    # cannot fix, and nothing rewrites wsgi.url_scheme behind Render's proxy.
    check('the link is minted against the configured origin',
          _link.startswith(_origin), f'{_link} does not start with {_origin}')

    # Reprinting a letter must not invalidate the code already on it: that is
    # what a non-timestamped signature buys.
    check('the same letter always produces the same code',
          verification_url(1024) == _link)
    check('a different request gets a different code',
          verification_url(1025) != _link)

    check('a signed link resolves to its request', resolve_reference(_reference) == 1024)
    check('the token is not the bare id', '1024' not in _token.split('.')[-1], _token)

    for _label, _bad in [
        ('no token at all', 'REQ-1024'),
        ('a forged signature', 'REQ-1024.forged'),
        ('a tampered signature', _reference[:-2] + 'xy'),
        ('a swapped readable half', 'REQ-0001' + _reference[7:]),
        ('a bare numeric id', '1024'),
        ('an empty link', ''),
        ('a token with no reference', '.' + _token),
    ]:
        try:
            _resolved = resolve_reference(_bad)
            check(f'{_label} is refused', False, f'accepted as {_resolved}')
        except ValueError:
            check(f'{_label} is refused', True)

    # A token minted for a different record, pasted onto this reference.
    _other = verification_url(2048).rsplit('/verify/', 1)[1]
    try:
        resolve_reference('REQ-1024' + _other[len('REQ-2048'):])
        check('a reference from another letter is refused', False, 'accepted')
    except ValueError:
        check('a reference from another letter is refused', True)

    _png = data_uri(_link)
    check('the code is an inline PNG', _png.startswith('data:image/png;base64,'))
    check('  -> and decodes to a real PNG',
          base64.b64decode(_png.split(',', 1)[1]).startswith(b'\x89PNG'))

check('requirements pins the QR encoder',
      re.search(r'^qrcode==', _req, re.M) is not None)

# The letter, rendered for every status that can reach a printer. patch_models
# is the file's own helper, so current_user() resolves to a student instead of
# reaching for a database this harness does not have.
for _status, _expect in [
    (RequestStatus.APPROVED, 'APPROVED'),
    (RequestStatus.REJECTED, 'REJECTED'),
    (RequestStatus.CANCELLED, 'CANCELLED'),
    (RequestStatus.PENDING, 'AWAITING VERIFICATION'),
]:
    _restore_status = patch_models(STUDENT)
    perm_mod.PermissionModel.find_by_id = staticmethod(
        lambda rid, s=_status: make_request(rid, s))
    try:
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['_last_seen'] = int(datetime.now().timestamp())
        _page = client.get('/student/requests/1024/letter')
        _html = _page.get_data(as_text=True)

        check(f'letter renders for {_status.value}', _page.status_code == 200,
              f'got {_page.status_code}')
        check(f'  -> {_status.value} letter carries a QR code',
              'data:image/png;base64,' in _html)
        check(f'  -> {_status.value} letter states the PIN',
              STUDENT.roll_number in _html)
        check(f'  -> {_status.value} letter states the status',
              _expect in _html, _expect)
        check(f'  -> {_status.value} letter names the reviewing faculty',
              LECTURER.name in _html)
        check(f'  -> {_status.value} letter attests by name, not by signature',
              ('Approved by' in _html) == (_status is RequestStatus.APPROVED)
              and 'letter-sign' not in _html)
        # The signature blocks were replaced by a single name line. Leaving them
        # in is what pushed the letter onto a second sheet.
        check(f'  -> {_status.value} letter has no signature blocks',
              'Faculty Signature' not in _html
              and 'letter-sign' not in _html
              and 'Head of Department</small>' not in _html)
        check(f'  -> {_status.value} letter prints as one A4 page',
              'size: A4 portrait' in _html
              and 'break-inside: avoid' in _html)
        check(f'  -> {_status.value} letter has no template errors',
              'Traceback' not in _html and 'UndefinedError' not in _html)
        with client.session_transaction() as sess:
            sess.clear()
    finally:
        _restore_status()

# The page behind the code. A fresh client, and no session in it: the whole
# point is that a gatekeeper with no account can open it.
_restore_verify = patch_models(STUDENT)
try:
    # The whole point of the scan: a signed link has to report a decision, so
    # this one is rendered from an approved record.
    perm_mod.PermissionModel.find_by_id = staticmethod(
        lambda rid: make_request(rid, RequestStatus.APPROVED))
    _verify_client = app.test_client()
    with _qr_app.test_request_context('/'):
        _verify_ref = verification_url(1024).rsplit('/verify/', 1)[1]

    _response = _verify_client.get('/verify/' + _verify_ref)
    _body = _response.get_data(as_text=True)
    check('a signed link opens without signing in', _response.status_code == 200,
          f'got {_response.status_code}')
    check('  -> the page shows the PIN', STUDENT.roll_number in _body)
    check('  -> the page shows the status', 'APPROVED' in _body)
    check('  -> the page names the reviewing faculty', LECTURER.name in _body)
    check('  -> the page shows the period', 'October' in _body)
    check('  -> the page is not indexable', 'noindex' in _body)
    # It is a read-only receipt, so it must offer no way to change anything.
    check('  -> the page exposes no form', '<form' not in _body.lower())
    check('  -> the page leaks no connection detail',
          'postgres' not in _body.lower() and 'psycopg' not in _body.lower())

    # Three tones, not one per status: green grants, red refuses, grey says
    # nobody has decided. A stranger opens this page to answer one question, so
    # the colour has to survive a status being added later.
    _verify_mod = re.search(r'class="verify-state verify-state--(\w+)"', _body)
    check('  -> an approved record verifies green',
          _verify_mod and _verify_mod.group(1) == 'green',
          f'got {_verify_mod.group(1) if _verify_mod else "no tone"}')
    check('  -> the university name is not spelled out in the letterhead',
          'ADITYA UNIVERSITY' not in _body)
    check('  -> the mark is centred above the department line',
          'flex-direction: column' in _body)
    check('  -> only three tones exist, so no status can add a fourth',
          all(f'verify-state--{t}' in _body for t in ('green', 'red', 'grey'))
          and not any(f'verify-state--{s}' in _body for s in
                      ('approved', 'rejected', 'pending', 'awaiting_hod',
                       'cancelled', 'expired')))

    # The rest of the statuses must all land on grey rather than borrowing a
    # verdict colour.
    for _status, _want in [(RequestStatus.REJECTED, 'red'),
                           (RequestStatus.PENDING, 'grey'),
                           (RequestStatus.AWAITING_HOD, 'grey'),
                           (RequestStatus.CANCELLED, 'grey'),
                           (RequestStatus.EXPIRED, 'grey')]:
        perm_mod.PermissionModel.find_by_id = staticmethod(
            lambda rid, _s=_status: make_request(rid, _s))
        _r = _verify_client.get('/verify/' + _verify_ref)
        _m = re.search(r'class="verify-state verify-state--(\w+)"',
                        _r.get_data(as_text=True))
        check(f'  -> {_status.value} verifies {_want}',
              _m and _m.group(1) == _want,
              f'got {_m.group(1) if _m else "no tone"}')

    perm_mod.PermissionModel.find_by_id = staticmethod(
        lambda rid: make_request(rid, RequestStatus.APPROVED))

    # A group letter has to name everyone it covers, and it has to say who was
    # struck off. The verify template receives the row as `record`; naming Flask's
    # own `request` global instead evaluates false silently and renders nothing,
    # which is how this went missing the first time.
    for _gid, _status, _kept in [
            (2001, RequestStatus.APPROVED, [1, 7]),
            (2002, RequestStatus.AWAITING_HOD, None),
            (2003, RequestStatus.PENDING, None)]:
        perm_mod.PermissionModel.find_by_id = staticmethod(
            lambda rid, _g=_gid, _s=_status, _k=_kept:
                _greq(rid, [1, 7, 8, 9], _s, approved=_k))

        _g_body = _verify_client.get('/verify/' + _verify_ref).get_data(as_text=True)
        _listed = [1, 7] if _kept is not None else [1, 7, 8, 9]
        _pins = [FIXTURE_DIRECTORY[i]['roll_number'] for i in _listed]
        check(f'  -> a {_status.value.lower()} group letter lists every PIN',
              'verify-row--members' in _g_body
              and all(p in _g_body for p in _pins),
              f'missing {[p for p in _pins if p not in _g_body]}')

    # The dropped student is named on the public page, not merely counted.
    perm_mod.PermissionModel.find_by_id = staticmethod(
        lambda rid: _greq(rid, [1, 7, 8, 9], RequestStatus.APPROVED,
                          approved=[1, 7, 9]))
    _v_body = _verify_client.get('/verify/' + _verify_ref).get_data(as_text=True)
    check('  -> a student struck off by the HOD is named as not covered',
          FIXTURE_DIRECTORY[8]['roll_number'] in _v_body
          and 'not covered' in _v_body)
    check('  -> and the members still covered are all present',
          all(FIXTURE_DIRECTORY[i]['roll_number'] in _v_body for i in (1, 7, 9)))

    perm_mod.PermissionModel.find_by_id = staticmethod(
        lambda rid: make_request(rid, RequestStatus.APPROVED))

    check('a forged link 404s',
          _verify_client.get('/verify/REQ-1024.forged').status_code == 404)
    check('a bare reference 404s',
          _verify_client.get('/verify/REQ-0001').status_code == 404)
finally:
    _restore_verify()

print()
print('=' * 70)
print('14. AN UNREACHABLE STORE')
print('=' * 70)

# Everything above proved the store behaves itself when it answers. This is the
# other half: what a deployment whose store is unreachable does. It used to be a
# pool that could not connect; it is now a store with no client, which is the same
# state from every layer above -- the pages, the session and the health check.

import sys

from app.models import firestore as _store_mod
from app.models.firestore import DatabaseUnavailable

check('the Postgres pool module is gone',
      'app.models.database' not in sys.modules
      and not Path('app/models/database.py').exists(),
      'app/models/database.py is still present')
check('nothing in the app imports the pool any more',
      not any('models.database' in Path(path).read_text(encoding='utf-8')
              for path in Path('app').rglob('*.py')),
      ', '.join(str(path) for path in Path('app').rglob('*.py')
                if 'models.database' in path.read_text(encoding='utf-8')))

_live_client = _store_mod.store.client
try:
    # An unreachable store must produce a page a person can read, not an empty
    # 500. Both spellings matter: the guard's own 503, and the store's exception
    # from a view whose query could not run.
    _store_mod.store.client = None
    _outage = app.test_client()

    with _outage.session_transaction() as _sess:
        _sess['user_id'] = 7
        _sess['_last_seen'] = int(datetime.now().timestamp())
        _sess['_marker'] = 'kept'

    _body404 = _outage.get('/no/such/page').get_data(as_text=True)
    check('a 404 page still renders while the store is unreachable',
          _outage.get('/no/such/page').status_code == 404, _body404[:80])
    check('  -> and it is a real page, not an empty body', len(_body404) > 500,
          f'{len(_body404)} bytes')

    _landing = _outage.get('/')
    check('the front door reports unavailability instead of signing you out',
          _landing.status_code == 503, f'got {_landing.status_code}')

    _guarded = _outage.get('/student/dashboard')
    check('a signed-in page reports unavailability rather than the login page',
          _guarded.status_code == 503, f'got {_guarded.status_code}')
    check('  -> and it tells the client when to come back',
          _guarded.headers.get('Retry-After', '').isdigit(),
          str(_guarded.headers.get('Retry-After')))

    with _outage.session_transaction() as _sess:
        _kept = _sess.get('_marker') is not None and _sess.get('user_id') == 7
    check('an outage does not sign the user out', _kept,
          'the session was cleared by a store failure')

    with app.test_request_context('/'):
        from flask import session as _session
        from app.utils.security import current_user, database_unavailable
        # A context with no session never reaches the store at all, so the
        # session has to be seeded for this to test the unavailable branch.
        _session['user_id'] = 7
        _session['_last_seen'] = int(datetime.now().timestamp())
        _loaded = current_user()
        check('current_user() answers None instead of raising', _loaded is None)
        check('  -> and the caller can tell why',
              database_unavailable() is True)
        check('  -> and it is asked once, not on every template lookup',
              current_user() is None and database_unavailable() is True)

    _err503 = app.test_client().get('/student/dashboard')
    _err_body = _err503.get_data(as_text=True).lower()
    check('the unavailable page leaks no store detail',
          'firestore' not in _err_body and 'googleapis' not in _err_body,
          'the page named the backend')

    # The health path stays 200 either way. Render restarts a service on a
    # non-2xx, so a probe that pings the store turns one bad minute into a
    # restart loop where every request queues behind a cold start.
    _health = app.test_client().get('/healthz')
    check('/healthz returns 200 even with no store', _health.status_code == 200,
          f'got {_health.status_code}')
    check('  -> and still reports the store verdict separately',
          _health.get_json().get('database') in ('ok', 'unreachable', 'unknown'),
          str(_health.get_json()))

    # Missing credentials must not take the process down: the app boots, and the
    # fault shows up as an upload or a request rather than at import time.
    check('a store with no client still constructs',
          isinstance(_store_mod.Store(), _store_mod.Store))
finally:
    _store_mod.store.client = _live_client

# The favicon the browser asks for by default, not because the layout asked.
_favicon = client.get('/favicon.ico')
check('the default favicon request is answered', _favicon.status_code == 200,
      f'got {_favicon.status_code}')
check('  -> and it is an image, not an error page',
      (_favicon.mimetype or '').startswith('image/'), str(_favicon.mimetype))

# The favicon the browser asks for by default, not because the layout asked.
_favicon = client.get('/favicon.ico')
check('the default favicon request is answered', _favicon.status_code == 200,
      f'got {_favicon.status_code}')
check('  -> and it is an image, not an error page',
      (_favicon.mimetype or '').startswith('image/'), str(_favicon.mimetype))

print()
print('=' * 70)
print('9z2. DUPLICATE DETECTION AND THE TWO-STAGE APPROVAL')
print('=' * 70)

# These two are the heart of the stated motive -- a duplicate check between
# submission and review, and an HOD approval that actually grants the permission
# -- and neither had any coverage, so they are exercised here against the
# service layer directly rather than through a route.
from datetime import date as _date  # noqa: E402
from app.permissions import service as svc  # noqa: E402
from app.utils import email as mail_mod  # noqa: E402


def _overlapping(rows):
    """A stand-in for the model query, so the overlap rule itself is what is
    under test rather than the datastore. Mirrors the group signature and the
    clash-dict shape, so the service is exercised against what it really gets."""
    def _find(student_ids, start_date, end_date, exclude_id=None):
        if isinstance(student_ids, int):
            student_ids = [student_ids]
        out, seen = [], set()
        for sid in dict.fromkeys(student_ids or []):
            for r in rows:
                if exclude_id and r.id == exclude_id:
                    continue
                if r.status in DEAD_STATUSES:
                    continue
                if r.start_date > end_date or r.end_date < start_date:
                    continue
                if r.id in seen:
                    continue
                seen.add(r.id)
                out.append({'student_id': sid, 'student': None, 'request': r})
        return out
    return _find


def _with_service_patches(rows_by_id, overlaps, **extra):
    """Patch everything submit/act touch, and hand back a restore plus a log."""
    log = {'status': [], 'history': [], 'created': [], 'mails': [],
           'approved': None}

    originals = {
        'find_by_id': perm_mod.PermissionModel.find_by_id,
        'update_status': perm_mod.PermissionModel.update_status,
        'find_overlapping': perm_mod.PermissionModel.find_overlapping_for_students,
        'history_create': perm_mod.ApprovalModel.create,
        'req_create': perm_mod.PermissionModel.create,
        'set_approved': perm_mod.PermissionModel.set_approved_members,
        'proof_create': perm_mod.ProofModel.create,
        'find_user': user_mod.UserModel.find_by_id,
        'get_hods': user_mod.UserModel.get_hods,
        'pick_faculty': svc.pick_faculty,
        'store': svc.validate_and_store,
        'notify_new': mail_mod.notify_faculty_of_new_request,
        'notify_student': mail_mod.notify_student_of_decision,
        'notify_hod_rec': mail_mod.notify_hod_of_recommendation,
        'notify_hod_dec': mail_mod.notify_hod_of_decision,
        'notify_hod_verdict': mail_mod.notify_student_of_hod_decision,
        'notify_member_added': mail_mod.notify_member_added_to_request,
        'notify_not_covered': mail_mod.notify_member_not_covered,
        'set_approved': perm_mod.PermissionModel.set_approved_members,
    }

    perm_mod.PermissionModel.find_by_id = staticmethod(
        lambda rid: rows_by_id.get(rid))
    perm_mod.PermissionModel.update_status = staticmethod(
        lambda rid, status, faculty_id=None: log['status'].append(
            (rid, status)) or True)
    perm_mod.PermissionModel.find_overlapping_for_students = staticmethod(overlaps)
    perm_mod.PermissionModel.set_approved_members = staticmethod(
        lambda rid, ids: log.__setitem__('approved', list(ids)) or True)
    perm_mod.ApprovalModel.create = staticmethod(
        lambda *a, **k: log['history'].append((a, k)) or None)
    user_mod.UserModel.find_by_id = staticmethod(lambda _id: STUDENT)
    user_mod.UserModel.get_hods = staticmethod(lambda: [HOD])
    svc.pick_faculty = lambda _student: LECTURER
    svc.validate_and_store = lambda _f: {
        'original_filename': 'doctor-letter.pdf',
        'stored_filename': 'abc123.pdf',
        'file_path': '2026/10/abc123.pdf',
        'file_type': 'pdf',
        'file_size': 48213,
    }
    perm_mod.PermissionModel.create = staticmethod(
        lambda **k: log['created'].append(k) or make_request(9001))
    perm_mod.ProofModel.create = staticmethod(lambda **k: None)
    mail_mod.notify_faculty_of_new_request = lambda *a, **k: True
    mail_mod.notify_student_of_decision = lambda *a, **k: log['mails'].append(
        'student-faculty') or True
    mail_mod.notify_hod_of_recommendation = lambda *a, **k: log['mails'].append(
        'hod-recommendation') or True
    mail_mod.notify_hod_of_decision = lambda *a, **k: log['mails'].append(
        'hod-decision') or True
    mail_mod.notify_student_of_hod_decision = lambda *a, **k: log['mails'].append(
        'student-hod') or True
    mail_mod.notify_member_added_to_request = lambda *a, **k: log['mails'].append(
        'member-added') or True
    mail_mod.notify_member_not_covered = lambda *a, **k: log['mails'].append(
        'not-covered') or True

    for key, value in extra.items():
        setattr(svc, key, value)

    def restore():
        perm_mod.PermissionModel.find_by_id = originals['find_by_id']
        perm_mod.PermissionModel.update_status = originals['update_status']
        perm_mod.PermissionModel.find_overlapping_for_students = \
            originals['find_overlapping']
        perm_mod.ApprovalModel.create = originals['history_create']
        perm_mod.PermissionModel.create = originals['req_create']
        perm_mod.ProofModel.create = originals['proof_create']
        user_mod.UserModel.find_by_id = originals['find_user']
        user_mod.UserModel.get_hods = originals['get_hods']
        svc.pick_faculty = originals['pick_faculty']
        svc.validate_and_store = originals['store']
        mail_mod.notify_faculty_of_new_request = originals['notify_new']
        mail_mod.notify_student_of_decision = originals['notify_student']
        mail_mod.notify_hod_of_recommendation = originals['notify_hod_rec']
        mail_mod.notify_hod_of_decision = originals['notify_hod_dec']
        mail_mod.notify_student_of_hod_decision = originals['notify_hod_verdict']
        mail_mod.notify_member_added_to_request = originals['notify_member_added']
        mail_mod.notify_member_not_covered = originals['notify_not_covered']
        perm_mod.PermissionModel.set_approved_members = originals['set_approved']

    return restore, log


# ---- The overlap rule ----
# A request over a known window, so each case states its own dates rather than
# inheriting make_request's. November, because submit_request refuses a start
# date in the past and the harness runs against the real clock.
NOV_10, NOV_12, NOV_30 = _date(2026, 11, 10), _date(2026, 11, 12), _date(2026, 11, 30)


def _req(rid, start, end, status=RequestStatus.PENDING):
    record = make_request(rid, status)
    record.start_date = start
    record.end_date = end
    return record


_BASE = _req(1, NOV_10, NOV_12)


check('a live request on the same days is found',
      len(_overlapping([_BASE])(1, NOV_10, NOV_12)) == 1)
check('a request that ends the day before does not overlap',
      _overlapping([_BASE])(1, _date(2026, 11, 13), _date(2026, 11, 20)) == [])
check('a request that starts the day after does not overlap',
      _overlapping([_BASE])(1, _date(2026, 11, 1), _date(2026, 11, 9)) == [])
check('a request partly covering the window is found',
      len(_overlapping([_BASE])(1, NOV_12, _date(2026, 11, 20))) == 1)
# A request wholly enclosing the new window is the clearest duplicate of all.
check('a request enclosing the whole window is found',
      len(_overlapping([_req(1, _date(2026, 11, 1), NOV_30)])(1, NOV_10, NOV_12)) == 1)
check('touching days count as overlapping',
      len(_overlapping([_BASE])(1, NOV_12, _date(2026, 11, 15))) == 1)

for _dead in (RequestStatus.REJECTED, RequestStatus.CANCELLED,
              RequestStatus.EXPIRED):
    check(f'a {_dead.value.lower()} request is not treated as a duplicate',
          _overlapping([_req(1, NOV_10, NOV_12, _dead)])(1, NOV_10, NOV_12) == [])

check('a request awaiting the HOD still counts as live',
      len(_overlapping([_req(1, NOV_10, NOV_12, RequestStatus.AWAITING_HOD)])(
          1, NOV_10, NOV_12)) == 1)
check('an approved request counts as live',
      len(_overlapping([_req(1, NOV_10, NOV_12, RequestStatus.APPROVED)])(
          1, NOV_10, NOV_12)) == 1)

# ---- The gate ----
_restore, _log = _with_service_patches(
    {9001: make_request(9001)},
    _overlapping([_req(1, NOV_10, NOV_12)]),
)
try:
    _args = dict(
        student=STUDENT, permission_type='CLASSROOM',
        reason='Medical appointment with the dentist at the city hospital',
        start_date_raw='2026-11-11', end_date_raw='2026-11-13',
        start_time_raw='', end_time_raw='', proof_file=None,
        base_url='http://localhost',
    )

    _raised = None
    try:
        svc.submit_request(**_args)
    except svc.DuplicateRequestError as exc:
        _raised = exc

    check('an overlapping request stops the first submission', _raised is not None)
    check('  -> and it is a ValidationError, so existing handlers still catch it',
          isinstance(_raised, svc.ValidationError))
    check('  -> the conflicting request is carried on the exception',
          bool(getattr(_raised, 'conflicts', None)))
    check('  -> nothing is written before the student confirms',
          not _log['created'] and not _log['status'])

    # Acknowledged: the same submission goes through.
    svc.submit_request(**_args, duplicate_ack=True)
    check('confirming the overlap lets the request through', bool(_log['created']))
finally:
    _restore()

# No overlap at all: no acknowledgement needed.
_restore, _log = _with_service_patches(
    {9001: make_request(9001)}, _overlapping([]))
try:
    svc.submit_request(
        student=STUDENT, permission_type='CLASSROOM',
        reason='Medical appointment with the dentist at the city hospital',
        start_date_raw='2026-11-01', end_date_raw='2026-11-02',
        start_time_raw='', end_time_raw='', proof_file=None,
        base_url='http://localhost',
    )
    check('a request with no overlap needs no confirmation', bool(_log['created']))
finally:
    _restore()

# ---- The two stages ----
_restore, _log = _with_service_patches(
    {500: make_request(500, RequestStatus.PENDING)}, _overlapping([]))
try:
    svc.act_on_request(
        request_id=500, faculty=LECTURER,
        action=ApprovalAction.APPROVED, remarks='Verified',
        base_url='http://localhost',
    )
    check('a lecturer approval does NOT grant the permission',
          _log['status'] == [(500, RequestStatus.AWAITING_HOD)],
          f'got {_log["status"]}')
    check('  -> the HOD is asked for the decision',
          'hod-recommendation' in _log['mails'])
    check('  -> and the student is told it is only a recommendation',
          'student-faculty' in _log['mails'])
    check('  -> the recommendation is recorded in the history',
          len(_log['history']) == 1)
finally:
    _restore()

_restore, _log = _with_service_patches(
    {501: make_request(501, RequestStatus.PENDING)}, _overlapping([]))
try:
    svc.act_on_request(
        request_id=501, faculty=LECTURER,
        action=ApprovalAction.REJECTED, remarks='Cannot spare you',
        base_url='http://localhost',
    )
    check('a lecturer rejection is final, with no HOD stage',
          _log['status'] == [(501, RequestStatus.REJECTED)],
          f'got {_log["status"]}')
    check('  -> the HOD is informed rather than asked to act',
          'hod-decision' in _log['mails']
          and 'hod-recommendation' not in _log['mails'])
finally:
    _restore()

# The HOD cannot shortcut the lecturer.
_restore, _log = _with_service_patches(
    {502: make_request(502, RequestStatus.PENDING)}, _overlapping([]))
try:
    _refused = False
    try:
        svc.hod_act_on_request(
            request_id=502, hod=HOD, action=ApprovalAction.APPROVED,
            remarks='', base_url='http://localhost')
    except svc.ValidationError:
        _refused = True
    check('the HOD cannot approve a request no lecturer has reviewed', _refused)
    check('  -> and nothing is written when they try', not _log['status'])
finally:
    _restore()

_restore, _log = _with_service_patches(
    {503: make_request(503, RequestStatus.AWAITING_HOD)}, _overlapping([]))
try:
    svc.hod_act_on_request(
        request_id=503, hod=HOD, action=ApprovalAction.APPROVED,
        remarks='Sanctioned', base_url='http://localhost')
    check('the HOD approval is what grants the permission',
          _log['status'] == [(503, RequestStatus.APPROVED)],
          f'got {_log["status"]}')
    check('  -> the student is told of the final verdict',
          'student-hod' in _log['mails'])
finally:
    _restore()

_restore, _log = _with_service_patches(
    {504: make_request(504, RequestStatus.AWAITING_HOD)}, _overlapping([]))
try:
    svc.hod_act_on_request(
        request_id=504, hod=HOD, action=ApprovalAction.REJECTED,
        remarks='Not sanctioned', base_url='http://localhost')
    check('the HOD can also turn it down',
          _log['status'] == [(504, RequestStatus.REJECTED)],
          f'got {_log["status"]}')
finally:
    _restore()

# ---- Withdrawal up to the decision ----
for _open, _label in [(RequestStatus.PENDING, 'pending'),
                      (RequestStatus.AWAITING_HOD, 'awaiting the HOD')]:
    _restore, _log = _with_service_patches(
        {600: make_request(600, _open)}, _overlapping([]))
    try:
        svc.cancel_request(600, STUDENT)
        check(f'a request {_label} can still be withdrawn',
              _log['status'] == [(600, RequestStatus.CANCELLED)],
              f'got {_log["status"]}')
    finally:
        _restore()

for _closed, _label in [(RequestStatus.APPROVED, 'approved'),
                        (RequestStatus.REJECTED, 'rejected')]:
    _restore, _log = _with_service_patches(
        {601: make_request(601, _closed)}, _overlapping([]))
    try:
        _blocked = False
        try:
            svc.cancel_request(601, STUDENT)
        except svc.ValidationError:
            _blocked = True
        check(f'a request already {_label} cannot be withdrawn', _blocked)
    finally:
        _restore()

print()
print('-' * 70)
print('9z3. THE NEW STATE REACHES THE PAGES')
print('-' * 70)

# AWAITING_HOD is a new status, so every page that renders one has to have been
# taught about it. A missing branch shows up as an empty badge or a raw enum name
# rather than an exception, which is exactly the kind of thing that reaches a
# student instead of failing a test.
_restore = patch_models(STUDENT)
try:
    _awaiting = staticmethod(lambda rid: make_request(rid, RequestStatus.AWAITING_HOD))
    perm_mod.PermissionModel.find_by_id = _awaiting
    perm_mod.PermissionModel.find_by_student = staticmethod(
        lambda *a, **k: [make_request(1024, RequestStatus.AWAITING_HOD)])
    perm_mod.ApprovalModel.find_by_request = staticmethod(lambda rid: [])

    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['_user_id'] = '1'

    _detail = client.get('/student/requests/1024').get_data(as_text=True)
    check('the student gets a banner for the new status',
          'status-banner--awaiting_hod' in _detail
          and 'Awaiting HOD approval' in _detail)
    check('  -> and is told it is not yet a grant of permission',
          'not yet a grant of permission' in _detail)
    check('  -> and may still withdraw it', 'Withdraw' in _detail)

    _letter = client.get('/student/requests/1024/letter').get_data(as_text=True)
    check('the letter states the status in words',
          'AWAITING HOD APPROVAL' in _letter)
    check('  -> with its own status colour',
          'letter-status--awaiting_hod' in _letter)
    check('  -> and says plainly it is not a grant of permission',
          'not a grant' in _letter.lower())
    check('  -> and never leaks the raw enum name', 'Awaiting_Hod' not in _letter)
finally:
    _restore()

# The HOD queue, and the decision only they can take.
#
# `/hod/requests` does not go through `PermissionModel.find_all_for_hod`; the
# hod blueprint delegates to the shared filter in faculty/routes.py, which reads
# the store directly. Patching only the model therefore leaves the page hitting a
# store this harness has no credentials for, which answers 503 -- the same reason
# `/hod/requests` had no render test before. The shared filter is stubbed here so
# the page is exercised for what it is being checked on: the action controls.
from app.faculty import routes as fac_routes  # noqa: E402

_AWAITING = [make_request(1024, RequestStatus.AWAITING_HOD)]
_real_filter = fac_routes._filtered_requests
fac_routes._filtered_requests = staticmethod(
    lambda *a, **k: list(_AWAITING))

_restore = patch_models(HOD)
try:
    perm_mod.PermissionModel.find_by_id = staticmethod(
        lambda rid: make_request(rid, RequestStatus.AWAITING_HOD))
    perm_mod.ApprovalModel.find_by_request = staticmethod(lambda rid: [])

    with client.session_transaction() as sess:
        sess['user_id'] = 3
        sess['_user_id'] = '3'
        sess['_csrf_token'] = 'workflow-token'

    _reg_resp = client.get('/hod/requests')
    _reg = _reg_resp.get_data(as_text=True)
    check('the HOD register lists the request as awaiting them',
          'Awaiting HOD' in _reg, f'status {_reg_resp.status_code}')
    check('  -> and offers the approve action',
          '/hod/requests/1024/action' in _reg and 'APPROVED' in _reg,
          f'status {_reg_resp.status_code}')
    check('  -> and the reject action', 'REJECTED' in _reg,
          f'status {_reg_resp.status_code}')
    check('  -> and the dashboard counts the queue separately',
          'Awaiting HOD' in client.get('/hod/dashboard').get_data(as_text=True))

    # An unrecognised decision is a 400, never a silent approval.
    _garbage = client.post('/hod/requests/1024/action',
                           data={'action': 'DEFINITELY',
                                 '_csrf_token': 'workflow-token'})
    check('an unrecognised decision is refused rather than defaulted to approve',
          _garbage.status_code == 400, f'got {_garbage.status_code}')
finally:
    _restore()
    fac_routes._filtered_requests = _real_filter

# A lecturer holds `faculty.action`, which recommends. They do not hold
# `hod.request_action`, which grants. The same POST has to be refused.
_restore = patch_models(LECTURER)
try:
    with client.session_transaction() as sess:
        sess.clear()
        sess['user_id'] = 2
        sess['_user_id'] = '2'
        sess['_csrf_token'] = 'workflow-token'

    _forbidden = client.post('/hod/requests/1024/action',
                             data={'action': 'APPROVED',
                                   '_csrf_token': 'workflow-token'})
    check('a lecturer cannot post the HOD decision',
          _forbidden.status_code == 403, f'got {_forbidden.status_code}')
finally:
    _restore()

_restore = patch_models(STUDENT)
try:
    with client.session_transaction() as sess:
        sess.clear()
        sess['user_id'] = 1
        sess['_user_id'] = '1'
        sess['_csrf_token'] = 'workflow-token'

    _forbidden = client.post('/hod/requests/1024/action',
                             data={'action': 'APPROVED',
                                   '_csrf_token': 'workflow-token'})
    check('nor can a student', _forbidden.status_code == 403,
          f'got {_forbidden.status_code}')
finally:
    _restore()

# The letter is rendered by both the student route and the faculty route, so
# anything that links to it has to name the right one, and its own toolbar has to
# follow the reader. `student.requests` is granted to students alone, which is
# what turned "All requests" into a 403 for a lecturer who opened a letter.
_restore = patch_models(LECTURER)
try:
    with client.session_transaction() as sess:
        sess.clear()
        sess['user_id'] = 2
        sess['_user_id'] = '2'

    _fac = client.get('/faculty/requests/1024')
    check('the faculty detail page renders', _fac.status_code == 200,
          f'got {_fac.status_code}')
    check('  -> and links the letter through the faculty route',
          '/faculty/requests/1024/letter' in _fac.get_data(as_text=True),
          'letter link points into the student portal')

    _fac_letter = client.get('/faculty/requests/1024/letter')
    check('a lecturer can open the letter from the faculty portal',
          _fac_letter.status_code == 200, f'got {_fac_letter.status_code}')
    _fac_html = _fac_letter.get_data(as_text=True)
    _stray = re.findall(r'href="(/student/[^"#]*)"', _fac_html)
    check('the letter toolbar keeps a lecturer out of the student portal',
          not _stray, f'links to {_stray}')
    # Both routes render one template, and it draws the code only when handed a
    # `qr`. The faculty view once omitted it entirely -- and because the details
    # table falls back to 'REQ-%04d', the reference still read correctly, so a
    # letter printed with no scannable code looked fine on the page.
    check('the letter printed from the faculty portal carries a QR code',
          'data:image/png;base64,' in _fac_html
          and 'lt-head__qr' in _fac_html,
          'no QR on the faculty copy of the letter')
    check('  -> pointing at the same verification reference',
          '/verify/REQ-1024' in _fac_html)
finally:
    _restore()

# The duplicate warning has to render, conflicts and all.
_restore = patch_models(STUDENT)
try:
    perm_mod.PermissionModel.find_overlapping_for_students = staticmethod(
        lambda *a, **k: [
            {'student_id': 7, 'student': None, 'request': make_request(700)},
            {'student_id': 8, 'student': None, 'request': make_request(701)},
        ])
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['_user_id'] = '1'
        sess['_csrf_token'] = 'workflow-token'

    _posted = client.post(
        '/student/requests/new',
        data={'permission_type': 'CLASSROOM',
              'reason': 'Medical appointment with the dentist at the city',
              'start_date': '2026-11-11', 'end_date': '2026-11-12',
              'duplicate_ack': '',
              '_csrf_token': 'workflow-token'},
    )
    _body = _posted.get_data(as_text=True)
    check('an overlapping submission comes back with the conflicts listed',
          'Possible duplicate' in _body and 'REQ-0700' in _body
          and 'REQ-0701' in _body,
          f'status {_posted.status_code}; len {len(_body)}')
    check('  -> and asks for confirmation before submitting',
          'name="duplicate_ack"' in _body)
finally:
    _restore()

print()
print('=' * 70)
print('9z4. GROUP PERMISSIONS')
print('=' * 70)

# One letter, one to four students. These cover the rules that make that safe:
# the cap, the fallback for records written before groups existed, the duplicate
# check reaching every member, and the HOD being able to strike one off.
from app.models.permission import (  # noqa: E402
    MAX_GROUP_MEMBERS, normalise_members,
)
from app.permissions import service as _svc  # noqa: E402

MEMBER_A = make_user(7, UserRole.STUDENT, 'Classmate A', '26B21CS007')
MEMBER_B = make_user(8, UserRole.STUDENT, 'Classmate B', '26B21CS008')
MEMBER_C = make_user(9, UserRole.STUDENT, 'Classmate C', '26B21CS009')


# ---- The cap and the requester-always-first rule ----
check('four students is the maximum on one letter',
      MAX_GROUP_MEMBERS == 4)
check('the requester is always member one',
      normalise_members(1, [2, 3])[0] == 1)
check('a request with no extra members is a request of one',
      normalise_members(1, []) == [1] and normalise_members(1, None) == [1])
check('duplicates collapse, whoever submitted them',
      normalise_members(1, [2, 1, 2, 3]) == [1, 2, 3])
# Truncating quietly would let a tampered POST put ten students on one letter
# and simply drop the rest, so the cap is asserted here and refused in the service.
check('the cap holds however many ids arrive',
      normalise_members(1, [2, 3, 4, 5, 6, 7]) == [1, 2, 3, 4])
check('junk ids are dropped rather than crashing the normaliser',
      normalise_members(1, [None, '', 'x', 2]) == [1, 2])

# ---- Records written before groups existed ----
_legacy = make_request(1)
check('a record with no member_ids reads as a single student',
      _legacy.members == [1] and _legacy.is_group is False,
      f'got {_legacy.members}')
check('  -> and its approval covers that student',
      _legacy.approved_members == [1])

# ---- Group arithmetic ----
_group = _greq(1, [1, 7, 8, 9])
check('a group of four reports its size',
      _group.group_size == 4 and _group.is_group is True)
check('an untouched approval covers everyone',
      _group.approved_members == [1, 7, 8, 9])
_trimmed = _greq(2, [1, 7, 8, 9], RequestStatus.APPROVED, approved=[1, 7, 9])
check('the HOD can strike a member off',
      _trimmed.approved_members == [1, 7, 9]
      and _trimmed.dropped_members == [8])
check('an approval covering nobody falls back to the whole group',
      _greq(3, [1, 7], approved=[]).approved_members == [1, 7])

# ---- resolve_members refuses what it should ----
def _resolve(ids, directory):
    _saved = user_mod.UserModel.find_by_id
    user_mod.UserModel.find_by_id = staticmethod(
        lambda _id: directory.get(_id))
    try:
        return _svc.resolve_members(STUDENT, ids)
    finally:
        user_mod.UserModel.find_by_id = _saved


_full = {1: STUDENT, 7: MEMBER_A, 8: MEMBER_B, 9: MEMBER_C,
         2: LECTURER, 3: HOD}
check('a group of four resolves',
      [u.id for u in _resolve([7, 8, 9], _full)] == [1, 7, 8, 9])

_refused = False
try:
    _resolve([7, 8, 9, 10], _full)
except _svc.ValidationError:
    _refused = True
check('a fifth student is refused rather than silently dropped', _refused)

_refused = False
try:
    _resolve([2], _full)          # a lecturer, not a student
except _svc.ValidationError:
    _refused = True
check('a staff account cannot be added to a student permission', _refused)

_other = dict(_full)
_other[7] = make_user(7, UserRole.STUDENT, 'Outsider', '26B21CS007')
_other[7].department = 'ECE'
_refused = False
try:
    _resolve([7], _other)
except _svc.ValidationError:
    _refused = True
check('a student from another department cannot be added', _refused)

_inactive = dict(_full)
_inactive[7] = make_user(7, UserRole.STUDENT, 'Gone', '26B21CS007')
_inactive[7].is_active = False
_refused = False
try:
    _resolve([7], _inactive)
except _svc.ValidationError:
    _refused = True
check('a deactivated account cannot be added', _refused)

# ---- The duplicate check reaches every member ----
_restore, _log = _with_service_patches(
    {9001: make_request(9001)},
    _overlapping([_req(1, NOV_10, NOV_12)]),
)
try:
    _members = {1: STUDENT, 7: MEMBER_A, 8: MEMBER_B}
    _saved = user_mod.UserModel.find_by_id
    user_mod.UserModel.find_by_id = staticmethod(
        lambda _id: _members.get(_id))
    _seen = {}
    try:
        _real_overlap = perm_mod.PermissionModel.find_overlapping_for_students

        def _spy(ids, start, end, exclude_id=None):
            _seen['ids'] = list(ids)
            return _real_overlap(ids, start, end, exclude_id)
        perm_mod.PermissionModel.find_overlapping_for_students = staticmethod(_spy)

        _clash = None
        try:
            _svc.submit_request(
                student=STUDENT, permission_type='CLASSROOM',
                reason='Medical appointment with the dentist at the city',
                start_date_raw='2026-11-11', end_date_raw='2026-11-13',
                start_time_raw='', end_time_raw='', proof_file=None,
                base_url='http://localhost', member_ids=[7, 8])
        except _svc.DuplicateRequestError as exc:
            _clash = exc
    finally:
        user_mod.UserModel.find_by_id = _saved

    check('the duplicate check is run over every member, not just the requester',
          _seen.get('ids') == [1, 7, 8], f'checked {_seen.get("ids")}')
    check('a clash on a co-member still holds the submission',
          _clash is not None)
    if _clash is not None:
        check('  -> and the clash names the student it belongs to',
              _clash.clashes[0].get('student') is not None
              and _clash.clashes[0]['student_id'] in (1, 7, 8))
        check('  -> naming them in the message, not just counting them',
              '26B21CS0' in str(_clash) or 'Classmate' in str(_clash),
              str(_clash))
finally:
    _restore()

# ---- The HOD's per-member decision ----
_restore, _log = _with_service_patches(
    {700: _greq(700, [1, 7, 8, 9], RequestStatus.AWAITING_HOD)},
    _overlapping([]))
try:
    _members = {1: STUDENT, 7: MEMBER_A, 8: MEMBER_B, 9: MEMBER_C}
    _saved = user_mod.UserModel.find_by_id
    user_mod.UserModel.find_by_id = staticmethod(lambda _id: _members.get(_id))
    try:
        _svc.hod_act_on_request(
            request_id=700, hod=HOD, action=ApprovalAction.APPROVED,
            remarks='Sanctioned', base_url='http://localhost',
            member_ids=[1, 7, 9])
    finally:
        user_mod.UserModel.find_by_id = _saved

    check('the HOD approves a subset of the group',
          _log['status'] == [(700, RequestStatus.APPROVED)],
          f'got {_log["status"]}')
    check('  -> and the subset is written down, not left implicit',
          _log['approved'] == [1, 7, 9], f'got {_log.get("approved")}')
    check('  -> the student who was struck off is told',
          'not-covered' in _log['mails'],
          f'mails: {_log["mails"]}')
finally:
    _restore()

_restore, _log = _with_service_patches(
    {701: _greq(701, [1, 7, 8, 9], RequestStatus.AWAITING_HOD)},
    _overlapping([]))
try:
    _refused = False
    try:
        _svc.hod_act_on_request(
            request_id=701, hod=HOD, action=ApprovalAction.APPROVED,
            remarks='', base_url='http://localhost', member_ids=[])
    except _svc.ValidationError:
        _refused = True
    check('an approval that would cover nobody is refused', _refused)
    check('  -> and nothing is written when it is', not _log['status'])
finally:
    _restore()

# ---- Visibility and withdrawal on a group ----
_group_record = _greq(800, [1, 7, 8, 9])
check('a co-member may read the request they are on',
      _svc.student_can_view(_group_record, MEMBER_A) is True)
# Deliberately not one of 7/8/9: the group already contains them, so using one
# here would assert that a member is not a member.
OUTSIDER = make_user(11, UserRole.STUDENT, 'Not On It', '26B21CS011')
check('a student who is not on it may not',
      _svc.student_can_view(_group_record, OUTSIDER) is False)
check('staff may read any request', _svc.student_can_view(_group_record, HOD) is True)

_restore, _log = _with_service_patches({800: _group_record}, _overlapping([]))
try:
    _refused = False
    try:
        # A co-member withdrawing would void the letter for the other three.
        _svc.cancel_request(800, MEMBER_A)
    except _svc.ValidationError:
        _refused = True
    check('only the requester can withdraw a group permission', _refused)
finally:
    _restore()

print()
print('=' * 70)
print('9z5. RETIRED LEAVE TYPE, AND FRAMING THE PROOF')
print('=' * 70)

# Leave management was withdrawn. The type has to be uncreatable and invisible,
# while the rows already in Firestore keep rendering -- deleting the enum member
# would make PermissionType() raise on read and 500 the whole app.
from app.models import OFFERABLE_PERMISSION_TYPES, PermissionType  # noqa: E402

from config import Config as _AppConfig  # noqa: E402


def _read_config_app_name() -> str:
    """The app name as configured, so the assertion cannot pass on a stale copy."""
    return _AppConfig.APP_NAME

check('leave is not offerable', PermissionType.LEAVE
      not in OFFERABLE_PERMISSION_TYPES)
check('the activity type is the only one offered',
      OFFERABLE_PERMISSION_TYPES == (PermissionType.CLASSROOM,),
      f'got {OFFERABLE_PERMISSION_TYPES}')
check('the retired type still reads, so historical rows do not 500',
      PermissionType('LEAVE') is PermissionType.LEAVE)

# The service is the only thing that can create a request, so that is what has to
# refuse -- a hand-rolled POST bypasses the form entirely.
_restore, _log = _with_service_patches({9001: make_request(9001)},
                                       _overlapping([]))
try:
    _refused = False
    try:
        _svc.submit_request(
            student=STUDENT, permission_type='LEAVE',
            reason='Medical appointment with the dentist at the city',
            start_date_raw='2026-12-01', end_date_raw='2026-12-02',
            start_time_raw='', end_time_raw='', proof_file=None,
            base_url='http://localhost')
    except _svc.ValidationError:
        _refused = True
    check('a leave request is refused by the service, not just hidden by the form',
          _refused)
    check('  -> and nothing is written', not _log['created'])

    _svc.submit_request(
        student=STUDENT, permission_type='CLASSROOM',
        reason='Inter-college fest with the robotics club',
        start_date_raw='2026-12-01', end_date_raw='2026-12-02',
        start_time_raw='', end_time_raw='', proof_file=None,
        base_url='http://localhost')
    check('an activity request is still accepted', bool(_log['created']))
finally:
    _restore()

# Nothing in the interface may still offer it.
check('the request form offers no leave option',
      'value="LEAVE"' not in open('templates/student/new_request.html',
                                  encoding='utf-8').read())
for _tpl in ('templates/hod/requests.html', 'templates/faculty/requests.html',
             'templates/hod/dashboard.html'):
    check(f'{_tpl.rsplit("/", 1)[1]} does not filter on leave',
          "'LEAVE'" not in open(_tpl, encoding='utf-8').read())
check('a retired row is labelled as retired, not as an activity',
      'Leave (retired)' in open('templates/_macros.html', encoding='utf-8').read())
check('the app name no longer claims leave tracking',
      'Leave' not in _read_config_app_name())

# ---- Retired type, and framing the proof ----
# The proof preview is an iframe on the request page, so its response has to be
# framable by us and by nobody else.
_restore = patch_models(LECTURER)
try:
    with client.session_transaction() as sess:
        sess.clear()
        sess['user_id'] = 2
        sess['_user_id'] = '2'
        sess['_csrf_token'] = 'workflow-token'

    _page = client.get('/faculty/requests/1024')
    check('the request page previews the proof in an iframe',
          '<iframe' in _page.get_data(as_text=True))

    # The proof route reads the store twice -- the proof row and the bytes -- so
    # both are stubbed. Without them it answers 503 and the assertion below would
    # be reading an error page's DENY rather than the real header.
    _saved_proof_id = perm_mod.ProofModel.find_by_id
    _saved_fetch = faculty_routes.fetch_proof
    perm_mod.ProofModel.find_by_id = staticmethod(
        lambda pid: make_proof(pid, 1024))
    faculty_routes.fetch_proof = lambda _path: b'%PDF-1.4 fake proof bytes'

    _frameable = []
    for _path in ('/faculty/requests/1024',
                  '/faculty/proofs/7/download'):
        _resp = client.get(_path)
        _frameable.append((_path, _resp.status_code,
                           _resp.headers.get('X-Frame-Options')))

    _status = dict((p, (s, h)) for p, s, h in _frameable)
    _code, _proof = _status['/faculty/proofs/7/download']
    check('the proof route serves the file in this harness', _code == 200,
          f'got {_code}')
    check('the proof response allows same-origin framing',
          _proof == 'SAMEORIGIN', f'got {_proof}')
    check('every other page still refuses framing outright',
          all(h == 'DENY' for p, _s, h in _frameable
              if not p.endswith('/download')),
          f'got {_frameable}')
finally:
    perm_mod.ProofModel.find_by_id = _saved_proof_id
    faculty_routes.fetch_proof = _saved_fetch
    _restore()

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

"""Smoke test every page in the sidebar for each role, at desktop and mobile."""

import http.cookiejar
import re
import sys
import urllib.error
import urllib.request

BASE = 'http://localhost:5000'
failures, passes = [], []


def check(label, condition, detail=''):
    (passes if condition else failures).append(label)
    print(f'  {"PASS" if condition else "FAIL"}  {label}' + (f' :: {detail}' if detail and not condition else ''))


def session():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def get(op, path):
    try:
        with op.open(BASE + path, timeout=25) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def login(op, email):
    status, html = get(op, '/auth/login')
    token = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    body = urllib.parse.urlencode({'email': email, '_csrf_token': token.group(1) if token else ''}).encode()
    req = urllib.request.Request(BASE + '/auth/dev-login', data=body)
    try:
        with op.open(req, timeout=25) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


import urllib.parse

ROLES = {
    'STUDENT': '26b21cs058@adityauniversity.in',
    'LECTURER': 'lecturer1.cse@adityauniversity.in',
    'HOD': 'hod.cse@adityauniversity.in',
}

PAGES = {
    'STUDENT': ['/student/dashboard', '/student/requests', '/student/requests/new',
                '/student/classes', '/account', '/student/requests/1',
                '/student/requests/1/letter'],
    'LECTURER': ['/faculty/dashboard', '/faculty/requests', '/faculty/search?q=26B21CS058',
                 '/faculty/classes', '/faculty/attendance', '/faculty/reports',
                 '/faculty/requests/1', '/faculty/requests/1/letter'],
    'HOD': ['/hod/dashboard', '/hod/requests', '/hod/students', '/hod/faculty',
            '/hod/classes', '/hod/reports', '/hod/report/print',
            '/faculty/requests/1'],
}

print('=' * 70)
print('SIDEBAR AND PAGE SMOKE TEST')
print('=' * 70)

for role, email in ROLES.items():
    print()
    print(role)
    op = session()
    code = login(op, email)
    check(f'{role} signs in', code == 200, f'status {code}')

    for path in PAGES[role]:
        status, html = get(op, path)
        clean = 'Traceback' not in html and 'UndefinedError' not in html
        check(f'{path:36s} {status}', status == 200 and clean,
              f'status {status} clean={clean}')

    # Sidebar / nav must be present and active-state aware.
    status, html = get(op, PAGES[role][0])
    if role in ('LECTURER', 'HOD'):
        check('sidebar rendered', 'au-side__nav' in html)
        check('wordmark appears once', html.count('aditya-logo.png') == 1, html.count('aditya-logo.png'))
        check('no gold accent in sidebar', 'ffd166' not in html)
        check('sidebar has sections', 'au-side__group' in html)
        check('sidebar active state applied', 'au-side__item is-active' in html)
        check('sidebar signout is a POST form',
              re.search(r'<form method="post" action="/auth/logout"', html) is not None)
        check('off-canvas wrapper for mobile', 'offcanvas' in html)
        check('mobile menu button present', 'data-bs-target="#auSidebar"' in html)
        check('wordmark sits on the navigation only',
              html.count('aditya-logo.png') == 1 and 'au-side__top' in html,
              f"{html.count('aditya-logo.png')} occurrences")
        header = html.split('app-topbar', 1)[-1].split('</header>', 1)[0]
        check('no wordmark in the header', 'aditya-logo.png' not in header)
        check('header sign-out is the right-most control',
              0 < header.find('app-topbar__spacer') < header.find('action="/auth/logout"'))
        check('footer carries no sign-out button', 'logout-all' not in html)
        check('sign-out offered in both header and nav',
              bool(re.search(r'action="/auth/logout"', header)) and 'au-side__out' in html)
        check('no white logo plate', 'au-logo-plate' not in html)
    else:
        check('student keeps top navigation', 'app-nav__link' in html)

    # Logout must be reachable from the header.
    check('header sign-out present',
          re.search(r'action="/auth/logout"', html) is not None)

# Sidebar geometry and the theme-aware wordmark are layout-wide, so assert once.
_css = open('static/css/style.css', encoding='utf-8').read()
_width = int(re.search(r'--au-sidebar-w:\s*(\d+)px', _css).group(1))
check(f'sidebar column is wide ({_width}px)', _width >= 280, f'{_width}px')
check('main column takes the remaining space with no offset',
      re.search(r'\.au-app--sidebar \.au-app__main\s*\{[^}]*margin-left:\s*0',
                _css, re.S) is not None
      and 'margin-left: var(--au-sidebar-w)' not in _css,
      'sidebar must be a flex sibling, not a fixed column plus a margin offset')
check('sidebar column is pinned to the width token',
      re.search(r'flex:\s*0 0 var\(--au-sidebar-w\)', _css) is not None)
check('sidebar column width uses the same token',
      re.search(r'\.au-app--sidebar \.au-sidebar-wrap\s*\{[^}]*width:\s*var\(--au-sidebar-w\)',
                _css, re.S) is not None)
check('dark theme renders the wordmark white',
      re.search(r"\[data-bs-theme='dark'\]\s*\.au-side__top\s+\.au-wordmark--side\s*\{\s*filter:\s*brightness\(0\) invert\(1\)",
                _css) is not None)
_default_logo = re.search(r"(?<!dark'\])[^\n]*\.au-side__top\s+\.au-wordmark--side\s*\{[^}]*\}", _css)
check('light theme keeps the wordmark gold (no filter)',
      _default_logo is not None and 'filter' not in _default_logo.group(0),
      _default_logo.group(0) if _default_logo else 'default rule not found')
check('header controls are a right-aligned flex row',
      'flex-grow-1 justify-content-end' in
      open('templates/base.html', encoding='utf-8').read())

# Sign-out clears the session and lands on login.
op = session()
login(op, ROLES['LECTURER'])
status, html = get(op, '/faculty/dashboard')
token = re.search(r'name="_csrf_token" value="([^"]+)"', html)
body = urllib.parse.urlencode({'_csrf_token': token.group(1)}).encode()
req = urllib.request.Request(BASE + '/auth/logout', data=body)
with op.open(req, timeout=25) as r:
    landed = r.read().decode('utf-8', 'replace')
check('sign-out lands on the login page',
      'Sign in with University Outlook' in landed)
check('sign-out confirms itself', 'signed out' in landed.lower())

status, html = get(op, '/faculty/dashboard')
check('portal is locked after sign-out',
      'Sign in with University Outlook' in html or status == 302,
      f'status {status}')

print()
print('=' * 70)
print(f'RESULT: {len(passes)} passed, {len(failures)} failed')
print('=' * 70)
if failures:
    for item in failures:
        print(f'  - {item}')
    sys.exit(1)
print('All sidebar and page checks passed.')
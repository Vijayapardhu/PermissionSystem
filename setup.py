"""One-command project setup.

Checks the prerequisites, creates a virtual environment, installs
dependencies, prepares .env and loads the database schema.

    python setup.py

Re-running is safe: it skips anything already in place.
"""

import getpass
import os
import platform
import secrets
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(ROOT, '.env')
EXAMPLE = os.path.join(ROOT, '.env.example')
SCHEMA = os.path.join(ROOT, 'migrations', 'schema.sql')
PROOF_DIR = os.path.join(ROOT, 'storage', 'proofs')

MIN_PYTHON = (3, 9)

ok = []
warn = []
fail = []


def say(msg):
    print(msg)


def step(n, text):
    say(f'\n[{n}] {text}')


def have(cmd):
    """True if an executable is on PATH."""
    from shutil import which
    return which(cmd) is not None


def run(args, **kw):
    return subprocess.run(args, cwd=ROOT, **kw)


# ---------------------------------------------------------------- 1
step(1, 'Checking prerequisites')

if sys.version_info < MIN_PYTHON:
    fail.append(
        f'Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found '
        f'{platform.python_version()}. Install from python.org.'
    )
else:
    ok.append(f'Python {platform.python_version()}')

if have('mysql'):
    ok.append('MySQL client (mysql) found on PATH')
else:
    warn.append(
        'mysql command not found. Install the MySQL server, then re-run, or '
        'import migrations/schema.sql from a MySQL Workbench / phpMyAdmin session.'
    )

if platform.system() == 'Windows' and not have('mysql'):
    say('       Windows installer: https://dev.mysql.com/downloads/installer/')

# ---------------------------------------------------------------- 2
step(2, 'Virtual environment')

if not have('python'):
    fail.append('python is not on PATH')

VENV_DIR = os.path.join(ROOT, '.venv')
VENV_PY = os.path.join(VENV_DIR, 'Scripts' if platform.system() == 'Windows' else 'bin',
                       'python.exe' if platform.system() == 'Windows' else 'python')

if os.path.isdir(VENV_DIR):
    ok.append('.venv already exists')
else:
    say('    creating .venv (this takes a few seconds)...')
    result = run([sys.executable, '-m', 'venv', VENV_DIR],
                capture_output=True, text=True)
    if result.returncode == 0:
        ok.append('.venv created')
    else:
        fail.append(f'Could not create .venv: {result.stderr.strip()[:200]}')

if os.path.isfile(VENV_PY):
    pip = os.path.join(os.path.dirname(VENV_PY), 'pip.exe' if platform.system() == 'Windows' else 'pip')
    say('    installing dependencies...')
    result = run([pip, 'install', '-q', '-r', 'requirements.txt'],
                 capture_output=True, text=True)
    if result.returncode == 0:
        ok.append('dependencies installed')
    else:
        fail.append(f'pip install failed: {result.stderr.strip()[-300:]}')
else:
    fail.append('virtual environment python not found after creation')

# ---------------------------------------------------------------- 3
step(3, 'Configuration file (.env)')

if os.path.isfile(ENV_FILE):
    ok.append('.env already exists, left untouched')
else:
    with open(EXAMPLE, encoding='utf-8') as fh:
        template = fh.read()

    secret = secrets.token_hex(32)
    template = template.replace('your-secret-key-here', secret)

    with open(ENV_FILE, 'w', encoding='utf-8') as fh:
        fh.write(template)
    os.chmod(ENV_FILE, 0o600)  # noqa: E115  (best effort on POSIX)
    ok.append(f'.env created with a generated SECRET_KEY')

say('')
say('    Fill these in before signing in with Microsoft:')
say('      CLIENT_ID      Entra ID -> App registrations -> Overview')
say('      TENANT_ID      Entra ID -> App registrations -> Overview')
say('      CLIENT_SECRET  Certificates & secrets -> New client secret')
say('      MYSQL_USER / MYSQL_PASSWORD   the database account')
say('')
say('    Leave DEV_MODE=true to use the local account picker while testing.')
say('    Set DEV_MODE=false once Outlook sign-in works.')

# ---------------------------------------------------------------- 4
step(4, 'Upload folder')

os.makedirs(PROOF_DIR, exist_ok=True)
gitkeep = os.path.join(PROOF_DIR, '.gitkeep')
if not os.path.isfile(gitkeep):
    open(gitkeep, 'w').close()
ok.append('storage/proofs ready (uploaded proofs land here)')

# ---------------------------------------------------------------- 5
step(5, 'Database schema')

if not have('mysql'):
    warn.append('skipped: mysql client not on PATH')
else:
    answer = input('    Load migrations/schema.sql into MySQL now? [y/N] ').strip().lower()
    if answer in ('y', 'yes'):
        user = os.environ.get('MYSQL_USER') or input('    MySQL user [root]: ').strip() or 'root'
        host = os.environ.get('MYSQL_HOST') or '127.0.0.1'
        password = os.environ.get('MYSQL_PASSWORD') or getpass.getpass('    MySQL password: ')

        # Read defaults from .env so the answer is not asked twice.
        if os.path.isfile(ENV_FILE):
            for line in open(ENV_FILE, encoding='utf-8'):
                if line.startswith('MYSQL_PASSWORD='):
                    password = password or line.split('=', 1)[1].strip()

        args = ['mysql', '-u', user, '-h', host]
        if password:
            args.append(f'-p{password}')

        with open(SCHEMA, encoding='utf-8') as fh:
            payload = fh.read()

        proc = subprocess.run(args, input=payload, capture_output=True, text=True)
        if proc.returncode == 0:
            ok.append('schema loaded (tables created, sample accounts added)')
        else:
            fail.append(f'schema import failed: {proc.stderr.strip()[-300:]}')
    else:
        warn.append('skipped: answer was not "y"')

# ---------------------------------------------------------------- summary
say('\n' + '=' * 62)
say('SETUP SUMMARY')
say('=' * 62)

for item in ok:
    say(f'  [ok]   {item}')
for item in warn:
    say(f'  [warn] {item}')
for item in fail:
    say(f'  [FAIL] {item}')

say('')
if fail:
    say('Setup did not finish cleanly. Fix the items above and re-run.')
    sys.exit(1)

say('Next steps:')
say('  1. Edit .env and enter CLIENT_ID, TENANT_ID, CLIENT_SECRET and the')
say('     MySQL credentials.')
say('  2. Start the app:')
say('       Windows   .venv\\Scripts\\activate     then  python app.py')
say('       mac/Linux source .venv/bin/activate   then  python app.py')
say('  3. Open http://127.0.0.1:5000')
say('')
say('Sign-in accounts while DEV_MODE=true:')
say('  hod.cse@adityauniversity.in         HOD')
say('  lecturer1.cse@adityauniversity.in   Lecturer')
say('  26b21cs058@adityauniversity.in      Student')
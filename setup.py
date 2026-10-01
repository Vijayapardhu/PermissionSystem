"""One-command project setup.

Checks the prerequisites, creates a virtual environment, installs
dependencies, prepares .env and loads the database schema.

    python setup.py

Re-running is safe: it skips anything already in place.

Storage is Firebase Storage, not the local filesystem, so there is no upload
folder to create here. Create the bucket in the Firebase console (Storage ->
Get started, public access off) before the first upload.
"""

import os
import platform
import secrets
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(ROOT, '.env')
EXAMPLE = os.path.join(ROOT, '.env.example')
SCHEMA = os.path.join(ROOT, 'migrations', 'schema.sql')

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

if have('psql'):
    ok.append('Postgres client (psql) found on PATH')
else:
    warn.append(
        'psql not found. You can still load the schema from the Supabase SQL '
        'editor: paste migrations/schema.sql and run it once.'
    )

if platform.system() == 'Windows' and not have('psql'):
    say('       Install PostgreSQL: https://www.postgresql.org/download/windows/')

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
say('      DATABASE_URL   Supabase -> Project Settings -> Database -> URI')
say('      FIREBASE_PROJECT_ID / FIREBASE_STORAGE_BUCKET')
say('                       Firebase -> Project settings -> General')
say('      FIREBASE_CREDENTIALS_PATH')
say('                       Firebase -> Project settings -> Service accounts')
say('')
say('    Leave DEV_MODE=true to use the local account picker while testing.')
say('    Set DEV_MODE=false once Outlook sign-in works.')

# ---------------------------------------------------------------- 4
step(4, 'Storage bucket')

say('')
say('    Proof documents live in a Firebase Storage bucket, not on this machine.')
say('    Create the bucket and a service account key once:')
say('      Storage -> Get started -> create a bucket -> public access OFF')
say('      Project settings -> Service accounts -> Generate new private key')
say('')
say('    The bucket must stay private. Proofs carry medical and identity')
say('    documents and are only ever served through an authorised route.')
ok.append('storage: Firebase bucket (create it and a key in the console)')

# ---------------------------------------------------------------- 5
step(5, 'Database schema')

if not have('psql'):
    warn.append('skipped: psql client not on PATH')
else:
    answer = input('    Load migrations/schema.sql into Supabase now? [y/N] ').strip().lower()
    if answer in ('y', 'yes'):
        dsn = os.environ.get('DATABASE_URL', '')
        if not dsn and os.path.isfile(ENV_FILE):
            for line in open(ENV_FILE, encoding='utf-8'):
                if line.startswith('DATABASE_URL='):
                    dsn = line.split('=', 1)[1].strip()
                    break

        if not dsn:
            fail.append('DATABASE_URL is not set; cannot load the schema')
        else:
            with open(SCHEMA, encoding='utf-8') as fh:
                payload = fh.read()

            proc = subprocess.run(
                ['psql', dsn, '-v', 'ON_ERROR_STOP=1', '-f', '-'],
                input=payload, capture_output=True, text=True,
            )
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
say('  1. Edit .env and enter CLIENT_ID, TENANT_ID, CLIENT_SECRET,')
say('     DATABASE_URL and the Firebase storage values.')
say('  2. Start the app:')
say('       Windows   .venv\\Scripts\\activate     then  python wsgi.py')
say('       mac/Linux source .venv/bin/activate   then  python wsgi.py')
say('  3. Open http://127.0.0.1:5000')
say('')
say('Sign-in accounts while DEV_MODE=true:')
say('  hod.cse@adityauniversity.in         HOD')
say('  lecturer1.cse@adityauniversity.in   Lecturer')
say('  26b21cs058@adityauniversity.in      Student')
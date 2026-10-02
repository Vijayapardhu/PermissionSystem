"""One-command project setup.

Checks the prerequisites, creates a virtual environment, installs
dependencies, prepares .env and seeds the Firestore accounts.

    python setup.py

Re-running is safe: it skips anything already in place.

Storage is Cloudflare R2, not the local filesystem, so there is no upload folder
to create here. Create the bucket and its API token in the Cloudflare dashboard
(R2 -> Create bucket, Public off) before the first upload.
"""

import os
import platform
import secrets
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(ROOT, '.env')
EXAMPLE = os.path.join(ROOT, '.env.example')

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
say('      FIREBASE_PROJECT_ID / FIREBASE_CREDENTIALS_PATH')
say('                       Firebase -> Project settings')
say('      R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY')
say('                       Cloudflare -> R2 -> Manage R2 API Tokens')
say('')
say('    Leave DEV_MODE=true to use the local account picker while testing.')
say('    Set DEV_MODE=false once Outlook sign-in works.')

# ---------------------------------------------------------------- 4
step(4, 'Storage bucket')

say('')
say('    Proof documents live in a Cloudflare R2 bucket, not on this machine.')
say('    Create the bucket and a token once, in the Cloudflare dashboard:')
say('      R2 -> Create bucket -> name it "proofs" -> public access OFF')
say('      R2 -> Manage R2 API Tokens -> Create Account API token')
say('                      (Object Read & Write on that bucket)')
say('')
say('    The bucket must stay private. Proofs carry medical and identity')
say('    documents and are only ever served through an authorised route.')
ok.append('storage: R2 bucket "proofs" (create it and a token in the dashboard)')

# ---------------------------------------------------------------- 5
step(5, 'Firestore seed accounts')

# There is no schema to load. Firestore collections come into being when a
# document is written, so "creating the schema" is writing the handful of
# accounts the app cannot start without. The seed is idempotent: it keys on the
# email address and leaves anything already there alone.
seed_py = os.path.join(ROOT, 'migrations', 'seed_firestore.py')

if not os.path.isfile(seed_py):
    fail.append('migrations/seed_firestore.py is missing')
else:
    _has_credentials = any(
        line.startswith(('FIREBASE_CREDENTIALS_PATH=', 'FIREBASE_CREDENTIALS_JSON='))
        and line.split('=', 1)[1].strip()
        for line in (open(ENV_FILE, encoding='utf-8') if os.path.isfile(ENV_FILE)
                     else [])
    )
    if not _has_credentials:
        warn.append(
            'skipped: set FIREBASE_CREDENTIALS_PATH (or _JSON) in .env first, '
            'then run: python migrations/seed_firestore.py'
        )
    else:
        answer = input('    Seed the staff and student accounts into Firestore now? [y/N] ').strip().lower()
        if answer in ('y', 'yes'):
            proc = subprocess.run(
                [VENV_PY, seed_py], capture_output=True, text=True, cwd=ROOT,
            )
            if proc.returncode == 0:
                ok.append('Firestore seeded (staff and student accounts created)')
            else:
                fail.append(f'seed failed: {(proc.stderr or proc.stdout).strip()[-300:]}')
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
say('     the Firestore credentials and the R2 API token values.')
say('  2. Start the app:')
say('       Windows   .venv\\Scripts\\activate     then  python wsgi.py')
say('       mac/Linux source .venv/bin/activate   then  python wsgi.py')
say('  3. Open http://127.0.0.1:5000')
say('')
say('Sign-in accounts while DEV_MODE=true:')
say('  hod.cse@adityauniversity.in         HOD')
say('  lecturer1.cse@adityauniversity.in   Lecturer')
say('  26b21cs058@adityauniversity.in      Student')
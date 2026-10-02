"""Seed the Firestore collections with the accounts the app expects.

Replaces migrations/schema.sql, which created and dropped Postgres tables. There
is no schema to apply here: Firestore collections come into being when a document
is written, so "loading the schema" is creating the handful of documents the app
cannot start without -- the staff accounts and a few students.

    python migrations/seed_firestore.py

Safe to re-run. Every write is keyed on the email address, so an account that
already exists is left exactly as it is; only a missing one is created. Nothing
here touches permission requests, classes or attendance.

The service account's private key is read from the environment, the same as the
app reads it:

    FIREBASE_PROJECT_ID        the Firebase project id
    FIREBASE_CREDENTIALS_PATH  path to the service account JSON (local runs)
    FIREBASE_CREDENTIALS_JSON  the same JSON inline (Render, CI)
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, '.env'))

from app.models.firestore import Store, to_timestamp  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

# microsoft_id is filled in automatically on first Entra sign-in, so the value
# below only reserves the email -> role mapping. name mirrors the roll number for
# students because the Outlook display name is the same value.
STAFF = [
    {'microsoft_id': 'hod-microsoft-id', 'email': 'hod.cse@adityauniversity.in',
     'name': 'Dr. S. Raghavan', 'role': 'HOD', 'department': 'CSE'},
    {'microsoft_id': 'lecturer1-microsoft-id',
     'email': 'lecturer1.cse@adityauniversity.in',
     'name': 'Dr. Anil Kumar', 'role': 'LECTURER', 'department': 'CSE'},
    {'microsoft_id': 'lecturer2-microsoft-id',
     'email': 'lecturer2.cse@adityauniversity.in',
     'name': 'Prof. Meera Sharma', 'role': 'LECTURER', 'department': 'CSE'},
]

STUDENTS = [
    {'microsoft_id': 'student1-microsoft-id',
     'email': '26b21cs058@adityauniversity.in', 'roll_number': '26B21CS058',
     'phone': '9876543210'},
    {'microsoft_id': 'student2-microsoft-id',
     'email': '26b21cs059@adityauniversity.in', 'roll_number': '26B21CS059',
     'phone': '9876543211'},
    {'microsoft_id': 'student3-microsoft-id',
     'email': '25b21cs012@adityauniversity.in', 'roll_number': '25B21CS012',
     'phone': '9876543212'},
    {'microsoft_id': 'student4-microsoft-id',
     'email': '24b21cs145@adityauniversity.in', 'roll_number': '24B21CS145',
     'phone': '9876543213'},
]


class _Config(dict):
    """The handful of keys the store reads, from the environment.

    A plain mapping rather than a Flask config: the seed runs without an
    application, and the store accepts either.
    """

    def __init__(self):
        super().__init__(
            FIREBASE_PROJECT_ID=os.environ.get('FIREBASE_PROJECT_ID') or '',
            FIRESTORE_DATABASE=os.environ.get('FIRESTORE_DATABASE') or '(default)',
            FIRESTORE_EMULATOR_HOST=os.environ.get('FIRESTORE_EMULATOR_HOST') or '',
            FIREBASE_CREDENTIALS_JSON=os.environ.get('FIREBASE_CREDENTIALS_JSON') or '',
            FIREBASE_CREDENTIALS_PATH=os.environ.get('FIREBASE_CREDENTIALS_PATH') or '',
            FIRESTORE_TIMEOUT_SECONDS=20,
            FIRESTORE_RETRIES=3,
            REPORT_TIMEZONE=os.environ.get('REPORT_TIMEZONE') or 'UTC',
        )


def seed():
    store = Store()
    store.init_app(_Config())
    if store.client is None:
        print('Firestore is not configured. Set FIREBASE_PROJECT_ID and '
              'FIREBASE_CREDENTIALS_PATH (or _JSON).', file=sys.stderr)
        return 1

    now = to_timestamp(datetime.now(timezone.utc))
    created = 0

    for account in STAFF:
        created += _put_account(store, account, now)
    for account in STUDENTS:
        payload = dict(account, role='STUDENT', department='CSE',
                       name=account['roll_number'])
        created += _put_account(store, payload, now)

    print(f'Seed complete: {created} created, '
          f'{len(STAFF) + len(STUDENTS) - created} already present '
          f'in {store.target}.')
    return 0


def _put_account(store, account, now) -> int:
    email = account['email'].lower()
    for existing in store.documents('users', email=email):
        print(f'  exists  {email}')
        return 0

    document = {
        'email': email,
        'name': account['name'],
        'microsoft_id': account.get('microsoft_id'),
        'roll_number': account.get('roll_number'),
        'phone': account.get('phone'),
        'role': account.get('role', 'STUDENT'),
        'department': account.get('department', 'CSE'),
        'is_active': True,
        'created_at': now,
        'updated_at': now,
    }
    doc_id = store.next_id('users')
    document['id'] = doc_id
    store.collection('users').document(str(doc_id)).set(document)
    print(f'  created {email} as {document["role"]} (id {doc_id})')
    return 1


if __name__ == '__main__':
    sys.exit(seed())
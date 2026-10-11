"""Import CSE students from the college CSV backup into Firestore.

Reads ``aus_students_FULL_backup_corrupted.csv`` from the Desktop, keeps only
CSE and its sub-branches (CS, AI, DS, IT), and writes each student as a user
document the app can search when adding members to a permission request.

Existing accounts are detected by email or roll number and left unchanged, so
the script is safe to re-run.

    python migrations/import_cse_students.py

Environment
-----------
FIREBASE_PROJECT_ID
FIREBASE_CREDENTIALS_PATH  or  FIREBASE_CREDENTIALS_JSON
"""

import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, '.env'))

from app.models.firestore import Store, utc_now  # noqa: E402

CSV_PATH = os.path.join(
    os.path.expanduser('~'), 'Desktop', 'college',
    'aus_students_FULL_backup_corrupted.csv'
)

CSE_BRANCH_CODES = {'CS', 'AI', 'DS', 'IT'}
UNIVERSITY_EMAIL_DOMAIN = 'adityauniversity.in'
_EMAIL_DOMAIN_ALIASES = {
    'aditayuniversity.in': UNIVERSITY_EMAIL_DOMAIN,
    'adityuniversity.in': UNIVERSITY_EMAIL_DOMAIN,
    'adityaunivesity.in': UNIVERSITY_EMAIL_DOMAIN,
    'adityaunversity.in': UNIVERSITY_EMAIL_DOMAIN,
    'adityauniversity': UNIVERSITY_EMAIL_DOMAIN,
}


def _branch_code_from_roll(roll: str) -> str:
    if not roll or len(roll) < 6:
        return ''
    return roll[5:7].upper()


def _normalise_email(value: str, roll: str) -> str:
    value = (value or '').strip().lower()
    if not value or '@' not in value:
        return f'{(roll or "").lower()}@{UNIVERSITY_EMAIL_DOMAIN}'

    local, _, domain = value.rpartition('@')
    domain = _EMAIL_DOMAIN_ALIASES.get(domain, domain)
    if domain != UNIVERSITY_EMAIL_DOMAIN:
        return f'{(roll or "").lower()}@{UNIVERSITY_EMAIL_DOMAIN}'
    return f'{local}@{domain}'


def _is_cse_row(row) -> bool:
    if len(row) < 8:
        return False
    roll = (row[0] or '').strip()
    return _branch_code_from_roll(roll) in CSE_BRANCH_CODES


def _user_exists(store, email: str, roll: str) -> bool:
    email = (email or '').strip().lower()
    roll = (roll or '').strip().upper()
    if not email and not roll:
        return False
    if email:
        matches = store.documents('users', email=email)
        if matches:
            return True
    if roll:
        matches = store.documents('users', roll_number=roll)
        if matches:
            return True
    return False


def main():
    store = Store()
    store.init_app({
        'FIREBASE_PROJECT_ID': os.environ.get('FIREBASE_PROJECT_ID') or '',
        'FIRESTORE_DATABASE': os.environ.get('FIRESTORE_DATABASE') or '(default)',
        'FIRESTORE_EMULATOR_HOST': os.environ.get('FIRESTORE_EMULATOR_HOST') or '',
        'FIREBASE_CREDENTIALS_JSON': os.environ.get('FIREBASE_CREDENTIALS_JSON') or '',
        'FIREBASE_CREDENTIALS_PATH': os.environ.get('FIREBASE_CREDENTIALS_PATH') or '',
        'FIRESTORE_TIMEOUT_SECONDS': 20,
        'FIRESTORE_RETRIES': 3,
        'REPORT_TIMEZONE': os.environ.get('REPORT_TIMEZONE') or 'UTC',
    })

    if store.client is None:
        print('Firestore is not configured. Set FIREBASE_PROJECT_ID and '
              'FIREBASE_CREDENTIALS_PATH (or _JSON).', file=sys.stderr)
        return 1

    if not os.path.exists(CSV_PATH):
        print(f'CSV not found: {CSV_PATH}', file=sys.stderr)
        return 1

    # Pre-fetch existing accounts so we can skip them without a read per row.
    print('Loading existing accounts...')
    existing_emails = set()
    existing_rolls = set()
    for row in store.documents('users'):
        email = (row.get('email') or '').lower()
        roll = (row.get('roll_number') or '').upper()
        if email:
            existing_emails.add(email)
        if roll:
            existing_rolls.add(roll)
    print(f'  {len(existing_emails)} existing accounts loaded.')

    # Reserve a block of IDs in one transaction so we do not burn one round-trip
    # per student. The counter holds the last allocated id; we bump it once by
    # the number of students we are about to write.
    print('Reserving id block...')
    needed = sum(1 for _ in open(CSV_PATH, 'r', encoding='utf-8', errors='ignore')) - 1
    counter_ref = store.collection('counters').document('users')

    @store.client.transactional
    def _reserve(transaction, ref, needed):
        snapshot = ref.get(transaction=transaction)
        current = int(snapshot.get('value') or 0) if snapshot.exists else 0
        transaction.set(ref, {'value': current + needed})
        return current + 1

    start_id = _reserve(store.client.transaction(), counter_ref, needed)
    print(f'  Reserved ids {start_id}..{start_id + needed - 1}')

    users_collection = store.collection('users')
    created = 0
    skipped = 0
    not_cse = 0
    batch = []
    next_id = start_id

    with open(CSV_PATH, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.reader(f)
        header = next(reader, None)

        for row in reader:
            if not _is_cse_row(row):
                not_cse += 1
                continue

            roll = (row[0] or '').strip()
            name = (row[1] or '').strip()
            raw_email = (row[21] if len(row) > 21 else '').strip()
            email = _normalise_email(raw_email, roll)

            roll_up = roll.upper()
            if email in existing_emails or roll_up in existing_rolls:
                skipped += 1
                continue

            document = {
                'id': next_id,
                'email': email,
                'name': name,
                'roll_number': roll_up,
                'microsoft_id': None,
                'phone': None,
                'role': 'STUDENT',
                'department': 'CSE',
                'is_active': True,
                'created_at': utc_now(),
                'updated_at': utc_now(),
            }
            batch.append(document)
            existing_emails.add(email)
            existing_rolls.add(roll_up)
            next_id += 1
            created += 1

            if len(batch) >= 500:
                for doc in batch:
                    users_collection.document(str(doc['id'])).set(doc)
                print(f'  ... {created} created, {skipped} skipped')
                batch = []

        if batch:
            for doc in batch:
                users_collection.document(str(doc['id'])).set(doc)
            print(f'  ... {created} created, {skipped} skipped')

    print(f'Import complete: {created} created, {skipped} skipped (already present), '
          f'{not_cse} non-CSE rows ignored in {store.target}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

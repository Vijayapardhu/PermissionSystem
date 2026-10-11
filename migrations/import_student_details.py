"""Import student names and phone numbers from the ECAP register export.

    python migrations/import_student_details.py            # dry run, writes nothing
    python migrations/import_student_details.py --apply    # writes

Source
------
``Downloads/sttudents/ecap_excel_*.xls`` -- eight exports from the college's
ECAP system. The ``.xls`` extension is misleading: the files are Excel's "Save as
Web Page" output, so they are HTML tables with a ``<table>`` holding the
register. Read as a workbook they yield nothing at all.

The register table is found by its header rather than by position, because the
column order is not something to hard-code against an export somebody else
produced. The columns used are ``Roll.No``, ``Student Name`` and ``Mobile.No``.

Scope
-----
Only roll numbers already present in the system are touched. The export is the
whole college -- 12,893 distinct rolls -- and this system's CSE register is
1,138 of them, so the other 11,755 are skipped by construction: nothing in this
file can create a user.

What it writes
--------------
``name`` and ``phone``, and only in two cases:

* ``name`` is overwritten when it currently holds the student's own roll
  number. Every one of the 1,138 was imported that way -- the roll stands in
  until a real name arrives -- so the check is "is this name just the roll
  number again", not "is this name empty". A name that is already a real one is
  never replaced, whatever the export says.

* ``phone`` is written when the account has none. An existing phone number is
  treated as the better record: it may have been corrected by hand, and an
  automated import should not undo that.

Safe to re-run. Nothing outside ``name`` and ``phone`` is modified, and the
conditions above mean a second run writes nothing at all.
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, '.env'))

from app.models import UserRole, is_roll_id  # noqa: E402
from app.models.firestore import store  # noqa: E402

SOURCE_DIR = Path(
    os.environ.get('ECAP_EXPORT_DIR',
                   Path.home() / 'Downloads' / 'sttudents'))

USERS = 'users'

ROW = re.compile(r'<tr.*?</tr>', re.S | re.I)
CELL = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.S | re.I)
TAG = re.compile(r'<[^>]+>')
NBSP = ' '
WS = re.compile(r'\s+')

# Phone numbers arrive as text, sometimes with a leading 0, sometimes with
# country code or dashes. Only the digits are kept, and an Indian mobile is
# ten digits starting 6-9 -- anything else is left alone rather than written as
# a number nobody can dial.
INDIAN_MOBILE = re.compile(r'^[6-9]\d{9}$')


def clean(value: str) -> str:
    return WS.sub(' ', TAG.sub('', value).replace(NBSP, ' ')).strip()


def normalise_phone(raw: str):
    """Return a usable 10-digit number, or None if this is not one.

    Returns None rather than a best guess. A wrong phone number on a student
    record is worse than none: it gets dialled, and the person who answers has
    no idea why a college is calling them.
    """
    if not raw:
        return None
    digits = re.sub(r'\D', '', raw)
    if INDIAN_MOBILE.match(digits):
        return digits
    # 91-prefixed and 0-prefixed forms of the same thing.
    for prefix in ('0091', '91', '0'):
        if digits.startswith(prefix) and INDIAN_MOBILE.match(digits[len(prefix):]):
            return digits[len(prefix):]
    return None


def read_register(path: Path):
    """(roll -> {name, phone}) from the register table of one export.

    Reads the whole file, so one at a time: the largest is 42MB and the eight
    together are ~250MB. The result is the only thing kept.
    """
    full = path.read_text(encoding='utf-8', errors='replace')

    for table in re.findall(r'<table.*?</table>', full, re.S | re.I):
        rows = ROW.findall(table)
        if len(rows) < 2:
            continue

        header = [clean(c).lower() for c in CELL.findall(rows[0])]
        if not any('roll' in h for h in header):
            continue

        i_roll = next(i for i, h in enumerate(header) if 'roll' in h)
        i_name = next((i for i, h in enumerate(header) if 'name' in h), None)
        i_phone = next((i for i, h in enumerate(header)
                        if 'mobile' in h or 'phone' in h
                        or 'contact' in h), None)
        if i_name is None:
            continue

        out = {}
        for row_html in rows[1:]:
            cells = CELL.findall(row_html)
            if len(cells) <= max(i_roll, i_name):
                continue
            values = [clean(c) for c in cells]
            roll = values[i_roll].strip().upper()
            if not roll:
                continue
            out[roll] = {
                'name': values[i_name].strip(),
                'phone': normalise_phone(
                    values[i_phone] if i_phone is not None
                    and len(values) > i_phone else ''),
                # Kept so a refused number can be read and judged rather than
                # vanishing. Never written: only `name` and `phone` are used
                # when a change is planned.
                'raw_phone': (values[i_phone] if i_phone is not None
                              and len(values) > i_phone else ''),
            }
        return out

    return {}


def load_all(directory: Path):
    """Every export merged. Later files win on a roll that appears twice.

    Order is by filename, which is the export timestamp -- these eight run from
    20:36 to 20:47, and the last one is the most recent statement of the
    record. Two of them turned out to be byte-identical, which merging handles
    without a special case.
    """
    merged = {}
    sources = {}
    files = sorted(directory.glob('*.xls'))
    if not files:
        raise SystemExit(f'  no .xls exports in {directory}')

    for path in files:
        data = read_register(path)
        print(f'  {path.name:<34} {len(data):>6} rows')
        for roll, values in data.items():
            merged[roll] = values
            sources[roll] = path.name
    return merged, sources, files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',
                        help='write the changes (default is a dry run)')
    args = parser.parse_args()

    if not SOURCE_DIR.is_dir():
        raise SystemExit(
            f'  the export directory is not there: {SOURCE_DIR}\n'
            '  set ECAP_EXPORT_DIR to where the files are.')

    print(f'=== reading {SOURCE_DIR} ===')
    merged, sources, files = load_all(SOURCE_DIR)
    print(f'  {len(files)} file(s), {len(merged):,} distinct roll numbers')

    print()
    print('=== the students already in the system ===')
    students = {}
    for row in store.documents(USERS, role=UserRole.STUDENT.value,
                               is_active=True):
        roll = (row.get('roll_number') or '').strip().upper()
        if roll:
            students[roll] = row
    print(f'  {len(students):,} active student accounts')

    matched = [r for r in students if r in merged]
    unmatched = [r for r in students if r not in merged]
    ignored = [r for r in merged if r not in students]
    print(f'  present in the export : {len(matched):,}')
    print(f'  absent from the export: {len(unmatched):,}')
    print(f'  export rolls skipped (not in the system): {len(ignored):,}'
          f'  -- the export is the whole college, this system is one branch')

    # Decide every write before making any of them.
    plans = []
    skipped_placeholder = []
    bad_phone = 0

    for roll in matched:
        row = students[roll]
        src = merged[roll]
        changes = {}

        current_name = (row.get('name') or '').strip()
        incoming_name = (src['name'] or '').strip()

        # A name that is already the roll number is a placeholder, not data.
        if incoming_name and is_roll_id(current_name) \
                and incoming_name.upper() != roll:
            if not is_roll_id(incoming_name):
                changes['name'] = incoming_name
            else:
                skipped_placeholder.append((roll, incoming_name))
        elif incoming_name and not current_name:
            changes['name'] = incoming_name

        if not (row.get('phone') or '').strip() and src['phone']:
            changes['phone'] = src['phone']

        if changes:
            plans.append((roll, row, changes, sources[roll]))

    print()
    print('=== what would change ===')
    name_changes = sum(1 for _, _, c, _ in plans if 'name' in c)
    phone_changes = sum(1 for _, _, c, _ in plans if 'phone' in c)
    both = sum(1 for _, _, c, _ in plans
               if 'name' in c and 'phone' in c)
    print(f'  accounts updated      : {len(plans):,}')
    print(f'  names filled in       : {name_changes:,}')
    print(f'  phone numbers filled  : {phone_changes:,}')
    print(f'  both                  : {both:,}')
    if skipped_placeholder:
        print(f'  export names that are themselves a roll number, so not used'
              f' : {len(skipped_placeholder):,}')

    print()
    print('=== the first 12, as they would be written ===')
    for roll, row, changes, source in plans[:12]:
        before = f"{row.get('name')!r} / {row.get('phone') or '-'!r}"
        after = (f"{changes.get('name', row.get('name'))!r} / "
                 f"{changes.get('phone', '-')!r}")
        print(f'  {roll:<14} {before:<46} -> {after}')

    print()
    print('=== any matched student the export gives nothing for? ===')
    empty = [r for r in matched
             if not (merged[r]['name'] or merged[r]['phone'])]
    print(f'  {len(empty):,} of {len(matched):,}')
    for roll in empty[:5]:
        print(f'    {roll}  {merged[roll]}')

    if not args.apply:
        print()
        print('DRY RUN. Nothing was written. Re-run with --apply to write.')
        return 0

    print()
    print('=== writing ===')
    written = 0
    failed = 0
    for roll, row, changes, _source in plans:
        ok = store.update(USERS, row['id'], changes)
        if ok:
            written += 1
        else:
            failed += 1
            print(f'  FAILED to update {roll} (id {row["id"]})')

    print(f'  {written:,} account(s) updated, {failed} failed')

    print()
    print('=== verify by reading back ===')
    filled_name = filled_phone = 0
    for roll, _row, _c, _s in plans:
        row = store.get(USERS, students[roll]['id'])
        if row and (row.get('name') or '').strip() \
                and not is_roll_id(row['name']):
            filled_name += 1
        if row and (row.get('phone') or '').strip():
            filled_phone += 1
    print(f'  now have a real name: {filled_name:,} of {len(plans):,}')
    print(f'  now have a phone    : {filled_phone:,} of {len(plans):,}')

    still_placeholder = sum(
        1 for roll in students
        if (store.get(USERS, students[roll]['id']) or {}).get('name')
        and is_roll_id(store.get(USERS, students[roll]['id'])['name']))
    print(f'  students still showing a roll as their name: {still_placeholder:,}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
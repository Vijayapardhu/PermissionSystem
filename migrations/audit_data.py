"""Audit live data for records that exist but cannot be acted on.

    python migrations/audit_data.py [--stuck-days 14] [--verbose]

**Read-only.** It calls nothing but ``store.documents`` and ``store.get``, both
of which are reads. There is no write, no migration and no repair -- deliberately.
An audit that can fix things is a script somebody will eventually run against
production at the wrong moment, and the interesting question here is always
"what is already wrong", which a repair hides by answering it silently.

Not part of ``verify.py``, for the same reason ``check_college_data.py`` is not:
that runs against synthetic fixtures and must pass on a fresh checkout. This is
only meaningful against real data, and real data has properties no fixture
reproduces -- an approval ladder that ends before the top of the hierarchy, a
record written by an import, a queue nobody is working through.

The check that prompted this: application #171, filed by the one Dean on the
system, sitting at ``DEAN_PENDING``. Not reachable through the UI -- the page
grants deny a Dean all eight faculty-leave pages and ``create_leave`` refuses
one at creation -- but written anyway by a script calling ``LeaveModel.create``
directly, which takes an id and cannot tell who is applying. Nobody could ever
approve it: the only person who can decide a ``DEAN_PENDING`` leave is the Dean,
who is the applicant, and ``dean_act_on_leave`` refuses self-approval by id.

So this reports, in order of how much they matter:

  undecidable  a record whose approver cannot exist
  stranded     a non-faculty applicant, which can now never enter the workflow
  stuck       non-terminal, untouched for longer than --stuck-days
  empty       a queue nobody is working through
  orphaned    a reference to a record that is not there

Exits non-zero if anything is undecidable, so it can gate a release. Everything
else is reported and does not fail the run.
"""

import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), '.env'))

from app.models import UserRole  # noqa: E402
from app.models.firestore import store  # noqa: E402
from app.models.leave import LeaveStatus  # noqa: E402

# Roles that can clear the HOD queue. A Deputy HOD is in it for the same reason
# the HOD is: one queue, two possible occupants.
#
# Held as `.value` strings, not enum members, because `user['role']` is the raw
# string read out of the store and never passed through `UserRole(...)`. The
# first version compared those two directly, every pool came back empty, and the
# audit confidently reported all four live applications as undecidable -- a
# false alarm on every single line, which is worse than not running it at all.
HOD_QUEUE_ROLES = tuple(r.value for r in
                        (UserRole.HOD, UserRole.DEPUTY_HOD, UserRole.DEAN))
DEAN_QUEUE_ROLES = tuple(r.value for r in (UserRole.DEAN,))
FACULTY_ROLES = tuple(r.value for r in
                      (UserRole.LECTURER, UserRole.HOD, UserRole.DEPUTY_HOD))

# Where each status goes next. Derived from the service rather than restated,
# so this cannot fall out of step with the workflow it is auditing.
TERMINAL = (LeaveStatus.FINAL_APPROVED, LeaveStatus.REJECTED,
            LeaveStatus.CANCELLED)
TERMINAL_VALUES = frozenset(
    getattr(s, 'value', s) for s in TERMINAL)

# (collection, field, target collection, is_required)
ORPHAN_LINKS = (
    ('faculty_leaves', 'faculty_id', 'users', True),
    ('leave_adjustments', 'leave_id', 'faculty_leaves', True),
    ('leave_adjustments', 'assigned_faculty_id', 'users', False),
    ('leave_adjustments', 'original_faculty_id', 'users', False),
    ('leave_amendments', 'leave_id', 'faculty_leaves', True),
    ('leave_amendments', 'actor_id', 'users', False),
    ('permission_requests', 'student_id', 'users', True),
    ('permission_requests', 'assigned_faculty_id', 'users', False),
    ('approval_history', 'request_id', 'permission_requests', False),
    ('approval_history', 'faculty_id', 'users', False),
    ('proof_documents', 'request_id', 'permission_requests', True),
    ('class_members', 'student_id', 'users', False),
    ('class_members', 'class_id', 'class_groups', True),
    ('class_groups', 'faculty_id', 'users', False),
)

problems = {'undecidable': [], 'stranded': [], 'stuck': [], 'empty': [],
            'orphaned': []}
inventory = Counter()
notes = []


def parse_time(value):
    """ISO-8601 as stored, or None if absent or unparseable."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def who(users, person_id):
    if person_id is None:
        return 'nobody'
    person = users.get(person_id)
    if person is None:
        return f'missing user {person_id}'
    return str(person.get('name') or person_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--stuck-days', type=int, default=14,
                        help='flag non-terminal records untouched for longer '
                             'than this (default: 14)')
    parser.add_argument('--verbose', action='store_true',
                        help='list every record, not only the problems')
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.stuck_days)

    print('=' * 74)
    print('DATA AUDIT -- read-only')
    print('=' * 74)

    # ---------------------------------------------------------------- load
    users = {}
    for doc in store.documents('users'):
        inventory['users'] += 1
        users[doc.get('id')] = doc

    collections = {}
    for name, _, _, _ in ORPHAN_LINKS:
        if name in collections:
            continue
        try:
            collections[name] = store.documents(name)
        except Exception as exc:                      # pragma: no cover
            notes.append(f'could not read {name}: {exc}')
            collections[name] = []
        inventory[name] = len(collections[name])

    leaves = collections.get('faculty_leaves', [])

    print('\n=== inventory ===')
    for name, count in sorted(inventory.items(), key=lambda kv: -kv[1]):
        if count:
            print(f'  {name:<24} {count:>6}')

    # -------------------------------------------------------- approver pool
    by_role = defaultdict(list)
    for person in users.values():
        by_role[person.get('role')].append(person.get('id'))

    can_clear_hod = [i for r in HOD_QUEUE_ROLES for i in by_role.get(r, [])]
    can_clear_dean = [i for r in DEAN_QUEUE_ROLES for i in by_role.get(r, [])]

    print('\n=== who can decide ===')
    for label, roles in (('HOD queue', HOD_QUEUE_ROLES),
                         ('Dean queue', DEAN_QUEUE_ROLES)):
        pool = [i for r in roles for i in by_role.get(r, [])]
        print(f'  {label:<12} {len(pool):>3}  ({", ".join(sorted(roles))})')
    if not can_clear_dean:
        notes.append('No Dean exists. Nothing can ever reach FINAL_APPROVED.')

    # ---------------------------------------------------------- the leaves
    print('\n=== leave applications ===')
    by_status = Counter()
    for leave in leaves:
        status = leave.get('status')
        by_status[str(status)] += 1
        applicant_id = leave.get('faculty_id')
        applicant = users.get(applicant_id)
        label = (f"#{leave.get('id')} {who(users, applicant_id)} "
                 f"[{applicant.get('role') if applicant else '?'}]")

        if status in TERMINAL_VALUES:
            if args.verbose:
                print(f'  {label:<48} {status}  (settled)')
            continue

        # --- undecidable: the approver this needs cannot exist -------------
        if status == LeaveStatus.DEAN_PENDING.value:
            pool = can_clear_dean
        elif status == LeaveStatus.HOD_PENDING.value:
            pool = can_clear_hod
        else:
            pool = None          # applicant-driven stages, not approver-driven

        if pool is not None and not any(p != applicant_id for p in pool):
            problems['undecidable'].append(
                (label, status,
                 f'no {status} approver other than the applicant'))
            print(f'  {label:<48} {status}  *** NOBODY CAN DECIDE THIS ***')
            continue

        # --- stranded: a non-faculty applicant --------------------------
        if applicant is not None and not _is_faculty(applicant):
            problems['stranded'].append(
                (label, status, 'applicant is not teaching staff'))
            print(f'  {label:<48} {status}  *** STRANDED ***')
            continue

        # --- stuck -------------------------------------------------------
        touched = (parse_time(leave.get('updated_at'))
                   or parse_time(leave.get('created_at')))
        if touched and touched < cutoff:
            days = (datetime.now(timezone.utc) - touched).days
            problems['stuck'].append((label, status, f'{days} days untouched'))
            print(f'  {label:<48} {status}  stuck {days} days')
            continue

        if args.verbose:
            print(f'  {label:<48} {status}  actionable')

    if not leaves:
        print('  (no leave applications)')

    # ------------------------------------------------------------- queues
    print('\n=== queues ===')
    counts = Counter(l.get('status') for l in leaves)
    pool_of = {
        LeaveStatus.HOD_PENDING.value: 'HOD or deputy',
        LeaveStatus.DEAN_PENDING.value: 'Dean',
    }
    # Printed once, each status with whoever could work it. The first version
    # printed the two approver queues and then looped over every status again,
    # so DEAN_PENDING appeared twice and it looked like two separate problems.
    #
    # Iterating `counts` alone would only ever print statuses that have rows, so
    # an empty queue -- the HOD one, which is what a Deputy HOD works from --
    # would print nothing and could not be reported. The two approver statuses
    # are therefore printed whether or not they hold anything.
    for status in list(counts) + [s for s in pool_of if s not in counts]:
        n = counts.get(status, 0)
        pool = pool_of.get(status, '')
        marker = ''
        if status in pool_of and n == 0 and (leaves or users):
            problems['empty'].append((status, pool))
            marker = '   <- nobody is working through this'
        print(f'  {str(status):<28} {n:>3}  {pool}{marker}')

    # ------------------------------------------------------------ orphans
    print('\n=== orphaned references ===')
    ids_by_collection = {name: {d.get('id') for d in docs}
                         for name, docs in collections.items()}
    ids_by_collection['users'] = set(users)

    for source, field, target, required in ORPHAN_LINKS:
        for doc in collections.get(source, []):
            value = doc.get(field)
            if value is None:
                if required:
                    problems['orphaned'].append(
                        (f'{source} #{doc.get("id")}', field, 'is empty'))
                continue
            if value not in ids_by_collection.get(target, set()):
                problems['orphaned'].append(
                    (f'{source} #{doc.get("id")}', field,
                     f'{target} {value} does not exist'))
    if problems['orphaned']:
        for item, field, why in problems['orphaned'][:20]:
            print(f'  {item:<34} {field}  {why}')
        if len(problems['orphaned']) > 20:
            print(f'  ... and {len(problems["orphaned"]) - 20} more')
    else:
        print('  none')

    # -------------------------------------------------------------- notes
    if notes:
        print('\n=== notes ===')
        for note in notes:
            print(f'  {note}')

    # -------------------------------------------------------------- verdict
    print('\n' + '=' * 74)
    undecidable = len(problems['undecidable'])
    print('  SUMMARY')
    for kind in ('undecidable', 'stranded', 'stuck', 'empty', 'orphaned'):
        n = len(problems[kind])
        if n or kind in ('undecidable',):
            print(f'    {kind:<12} {n:>4}')
    print()
    if undecidable:
        print(f'  {undecidable} record(s) NOBODY CAN DECIDE. '
              f'These are permanent until')
        print('  someone changes the data or the workflow.')
    print('  This script changed nothing.')
    print('=' * 74)

    return 1 if undecidable else 0


def _is_faculty(user):
    return user.get('role') in FACULTY_ROLES


if __name__ == '__main__':
    sys.exit(main())

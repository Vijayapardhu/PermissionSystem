"""Load the college's faculty, sections and timetable into Firestore.

    python migrations/import_college_data.py --dry-run     # parse and report
    python migrations/import_college_data.py               # write

Three exports are read:

    faculty details.xls              862 staff, employee number keyed
    timetables.xlsx                  7 sections, 7 periods a day
    timetable_choost_Students.xls    1138 students, section and course enrolment

What gets written, and why each piece:

``users``
    Faculty as LECTURER, or HOD where the designation says so. Students as
    STUDENT with a roll number and **no email** -- the mailbox is provisioned as
    ``<rollno>@adityauniversity.in`` and does not exist in these files, so the
    identity is matched on the roll number the mailbox is named after at first
    sign-in (see ``UserModel.create_or_update_from_microsoft``).

``class_groups`` / ``class_members``
    The sections become classes and the students become their rosters, reusing the
    collections the app already has. Attendance, the roster page and permission
    routing then work against real data with nothing new to build.

``section_courses``
    Which course each section runs and the pool of faculty listed against it. The
    pool is what the export actually states -- see the caveat below.

``timetable_periods``
    One row per section, day and period: when the section meets and for what.

**The one thing these files do not contain.** No file says which faculty teaches
which period. The legend and the roster each name a *pool* of one to three
faculty per course, while the grid says only when the section meets -- and
``T1-General`` ADSAA has four periods against three teachers, which no even split
explains. So ``timetable_periods.faculty_id`` is left null and the eligible pool
is stored alongside it.

That is not a shortcut, it is the honest reading, and it is why the HOD gets an
allocation page: a period nobody has claimed is visibly unclaimed rather than
silently attributed to the wrong lecturer. Affected-class detection is only
correct once someone fills those in, so the importer reports how many are
outstanding instead of declaring success.

Re-running is safe. Everything is keyed on a natural key -- employee number, roll
number, section code, section and slot -- so a second run updates in place rather
than duplicating.
"""

import argparse
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, '.env'))

from app.models.firestore import to_timestamp, utc_now  # noqa: E402
from app.models.firestore import store as selected_store  # noqa: E402
from app.utils.college_export import (  # noqa: E402
    ExportError, link_subjects_to_courses, parse_faculty_details,
    parse_section_rosters, parse_timetable_workbook,
)

DEFAULT_FILES = {
    'faculty': r'C:\Users\PARDHU\Downloads\faculty details.xls',
    'students': r'C:\Users\PARDHU\Downloads\timetable_choost_Students.xls',
    'timetable': r'C:\Users\PARDHU\Downloads\timetables.xlsx',
}

#: Departments the CSE portal serves. Faculty outside these are skipped unless
#: they actually teach one of these sections -- four Freshman Engineering and
#: Training staff do, and dropping them would leave sections unstaffable.
IN_SCOPE_DEPARTMENTS = (
    'computer science',
    'ai&ml',
    'artificial intelligence',
    'information technology',
    'data science',
)

#: Firestore rejects a batch over 500 writes. 400 leaves headroom for the
#: counter document the id block reservation touches.
BATCH_SIZE = 400


class _Config(dict):
    """The handful of keys the store reads, from the environment.

    Same shape as ``migrations/seed_firestore.py``: a plain mapping rather than a
    Flask config, because this runs without an application.
    """

    def __init__(self):
        super().__init__(
            FIREBASE_PROJECT_ID=os.environ.get('FIREBASE_PROJECT_ID') or '',
            FIRESTORE_DATABASE=os.environ.get('FIRESTORE_DATABASE') or '(default)',
            FIRESTORE_EMULATOR_HOST=os.environ.get('FIRESTORE_EMULATOR_HOST') or '',
            FIREBASE_CREDENTIALS_JSON=os.environ.get('FIREBASE_CREDENTIALS_JSON') or '',
            FIREBASE_CREDENTIALS_PATH=os.environ.get('FIREBASE_CREDENTIALS_PATH') or '',
            # Passed through so this script honours the same backend choice as the
            # app. Reading them here rather than inside the store keeps the
            # selection in one place, and means a run against SQLite cannot
            # silently write to a different database than the app is serving.
            DATABASE_BACKEND=os.environ.get('DATABASE_BACKEND') or '',
            DATABASE_URL=os.environ.get('DATABASE_URL') or '',
            SQLITE_PATH=os.environ.get('SQLITE_PATH') or '',
            FIRESTORE_TIMEOUT_SECONDS=30,
            FIRESTORE_RETRIES=3,
            REPORT_TIMEZONE=os.environ.get('REPORT_TIMEZONE') or 'UTC',
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _normalise_department(name):
    return ' '.join((name or '').lower().split())


def _is_in_scope(faculty):
    return any(key in _normalise_department(faculty['department'])
               for key in IN_SCOPE_DEPARTMENTS)


def _role_for(faculty):
    """HOD where the designation says so, otherwise LECTURER.

    Matched on the designation rather than a list of names, because the export
    spells it several ways: "Asst. Prof. & HOD", "Assoc.Professor & Deputy HOD".
    A Deputy HOD is given the HOD role here; the app has no separate role for one
    yet, and it is listed in the report so the HOD can be told what happened.
    """
    designation = _normalise_department(faculty['designation'])
    if 'hod' in designation or 'head of' in designation:
        return 'HOD'
    return 'LECTURER'


def _reserve_ids(store, collection, count):
    """Hand out ``count`` consecutive ids for a collection in one round trip.

    Delegates to the store rather than opening a transaction here, because the two
    backends differ: Firestore's ``next_id`` costs a transaction per id, which for
    ~1200 users is ~1200 round trips before a single record is written, while the
    SQL one reserves the whole block in a single transaction. Both give the same
    guarantee -- two concurrent allocations cannot be handed the same id.

    The caller must be the only writer for that collection while this runs, which
    holds for a one-off import. It is stated rather than hidden because it is the
    one assumption that would break under concurrent creation.
    """
    return store.reserve_ids(collection, count)


class Writer:
    """Batched upserts, with a count of what was created against what was updated.

    Batching is not an optimisation here, it is what makes the import finish: a
    thousand individual writes on a remote store is minutes of round trips, and
    one failed write would leave the import half-done with no record of where.
    """

    def __init__(self, store):
        self.store = store
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self._known = {}

    def upsert(self, collection, doc_id, values, existing_ids=None):
        """Write one document, counting it as new only if it was not there."""
        payload = self.store.to_firestore(values)
        payload['id'] = doc_id
        existed = (existing_ids is not None and doc_id in existing_ids) or \
            self.store.collection(collection).document(str(doc_id)).get(
                timeout=self.store.timeout).exists
        self.store.collection(collection).document(str(doc_id)).set(
            payload, timeout=self.store.timeout)
        if existed:
            self.updated += 1
        else:
            self.created += 1
        return doc_id

    def write_many(self, rows):
        """Write ``(collection, doc_id, values)`` triples in batches.

        Each collection's existing ids are read once and cached, so a repeat run
        reports what it replaced rather than claiming thousands of fresh
        documents. The write itself is a plain ``set`` either way, which is what
        makes the import safe to re-run.
        """
        pending, written = [], 0
        for collection, doc_id, values in rows:
            pending.append((collection, doc_id, values))
            if len(pending) >= BATCH_SIZE:
                written += self._flush(pending)
                pending = []
        if pending:
            written += self._flush(pending)
        return written

    def _existing_ids(self, collection):
        """Ids already in a collection, read once and remembered."""
        if collection not in self._known:
            self._known[collection] = {
                row.get('id') for row in self.store.documents(collection)}
        return self._known[collection]

    def _flush(self, pending):
        # store.batch() rather than store.client.batch(), so the script runs
        # unchanged on either backend.
        batch = self.store.batch()
        for collection, doc_id, values in pending:
            payload = self.store.to_firestore(values)
            payload['id'] = doc_id
            if doc_id in self._existing_ids(collection):
                self.updated += 1
            else:
                self.created += 1
            batch.set(self.store.collection(collection).document(str(doc_id)),
                      payload)
        batch.commit()
        return len(pending)


# --------------------------------------------------------------------------
# the import
# --------------------------------------------------------------------------

def build_plan(files):
    """Parse every export and work out exactly what would be written."""
    faculty = parse_faculty_details(files['faculty'])
    rosters = parse_section_rosters(files['students'])
    timetable = parse_timetable_workbook(files['timetable'])
    links = link_subjects_to_courses(timetable, rosters)

    # Anyone teaching one of these sections is in scope regardless of
    # department, or the sections they teach cannot be staffed.
    teaching = set()
    for section in timetable.values():
        for staff in section['legend'].values():
            teaching |= set(staff)

    staff = {}
    for code, record in faculty.items():
        if _is_in_scope(record) or code in teaching:
            staff[code] = record

    # Every employee number referenced anywhere must resolve, or a period would
    # be attributed to a teacher the portal has no account for.
    referenced = teaching
    for section in rosters.values():
        for course in section['courses'].values():
            referenced |= set(course['faculty'])
    unresolved = sorted(referenced - set(staff))

    students = {}
    for section, entry in rosters.items():
        for roll in entry['rolls']:
            students.setdefault(roll, set()).add(section)

    # Slot template: the time each period number most often starts at. P2 is
    # genuinely inconsistent in the source (10:20 in 28 cells, 10:30 in 9), so
    # the majority wins for the template and every period keeps the exact time
    # that section actually uses.
    starts = defaultdict(lambda: defaultdict(int))
    for section in timetable.values():
        for period in section['periods']:
            if period['start']:
                starts[period['period']][period['start']] += 1
    slots = {
        number: max(counts, key=counts.get)
        for number, counts in starts.items()
    }

    return {
        'faculty_all': faculty,
        'staff': staff,
        'teaching': teaching,
        'rosters': rosters,
        'timetable': timetable,
        'links': links,
        'students': students,
        'slots': slots,
        'unresolved': unresolved,
    }


def report(plan, store=None):
    """What the import found, and what still needs a person to decide."""
    lines = []
    staff, rosters, timetable = plan['staff'], plan['rosters'], plan['timetable']

    # Read through _role_for rather than repeating the designation test. The two
    # rules had drifted -- the report matched "hod" while the role assignment also
    # accepted "head of" -- so a Head of IT Applications was given the HOD role
    # and then left out of the list of HODs the department is shown. One rule, so
    # the report cannot undercount what the write actually did.
    heads = [h for h in plan['staff'].values() if _role_for(h) == 'HOD']
    deans = [h for h in plan['staff'].values()
             if 'dean' in _normalise_department(h['designation'])]

    unassigned = sum(
        1 for section in timetable.values() for _ in section['periods'])

    lines.append('Faculty records in export      : %d' % len(plan['faculty_all']))
    lines.append('In scope for this portal       : %d' % len(staff))
    lines.append('  of those, HOD                : %d' % len(heads))
    lines.append('  teaching a listed section    : %d' % len(plan['teaching']))
    lines.append('Students                       : %d' % len(plan['students']))
    lines.append('Sections                       : %d' % len(rosters))
    lines.append('Section periods (no owner yet) : %d' % unassigned)
    lines.append('')

    if heads:
        lines.append('HOD accounts that will be created:')
        for head in heads:
            lines.append('    %-6s %-36s %s' % (
                head['employee_code'], head['name'], head['designation']))
        lines.append('')
    if deans:
        lines.append('Dean-designated staff -- the app has no DEAN role yet, so these '
                     'are listed as LECTURER:')
        for dean in deans:
            lines.append('    %-6s %-36s %s' % (
                dean['employee_code'], dean['name'], dean['designation']))
        lines.append('')

    if plan['unresolved']:
        lines.append('WARNING: employee numbers referenced but not in scope (these '
                     'sections cannot be staffed):')
        for code in plan['unresolved'][:20]:
            lines.append('    %s' % code)
        lines.append('')

    matched = sum(len(v) for v in plan['links']['linked'].values())
    lines.append('Subject codes resolved to courses: %d' % matched)
    if plan['links']['unmatched']:
        lines.append('WARNING: unresolved subject codes:')
        for section, codes in sorted(plan['links']['unmatched'].items()):
            lines.append('    %-26s %s' % (section, ', '.join(codes)))
    else:
        lines.append('  every subject code matched a course by its teaching team')
    lines.append('')

    lines.append('Period times, as found in the workbook:')
    for number in sorted(plan['slots']):
        lines.append('    P%d  %s' % (number, plan['slots'][number]))
    lines.append('')
    lines.append('Note: P2 appears as both 10:20 AM and 10:30 AM across sections. '
                 'The')
    lines.append('      common time is used for the template and each period keeps '
                 'its own.')

    return '\n'.join(lines)


def write(plan, store):
    """Apply the plan. Returns a summary dict."""
    now = to_timestamp(utc_now())
    staff = plan['staff']

    # -- existing users, so a second run updates instead of duplicating -------
    existing = {}
    by_employee, by_roll = {}, {}
    for row in store.documents('users'):
        existing[row['id']] = row
        if row.get('employee_code'):
            by_employee[str(row['employee_code']).strip()] = row['id']
        if row.get('roll_number'):
            by_roll[str(row['roll_number']).strip().upper()] = row['id']

    writer = Writer(store)

    # -- faculty -------------------------------------------------------------
    # Faculty first: section_courses and the period pools reference them by id.
    new_faculty = sorted(
        code for code in staff if code not in by_employee
    )
    faculty_ids = dict(by_employee)
    if new_faculty:
        block = _reserve_ids(store, 'users', len(new_faculty))
        for code, user_id in zip(new_faculty, block):
            faculty_ids[code] = user_id

    rows = []
    for code in sorted(staff):
        record = staff[code]
        payload = {
            'id': faculty_ids[code],
            'employee_code': code,
            'microsoft_id': None,
            # The export carries the address for all 50 who teach here; keeping it
            # lets a lecturer be found by email before they have ever signed in.
            'email': record['email'],
            'name': record['name'],
            'roll_number': None,
            'phone': record['phone'],
            'role': _role_for(record),
            'designation': record['designation'],
            'department': 'CSE',
            'source_department': record['department'],
            'qualification': record['qualification'],
            'joining_date': record['joining_date'],
            'is_active': True,
        }
        current = existing.get(faculty_ids[code])
        if current:
            # Never blank an identity a real sign-in established.
            for field in ('microsoft_id', 'email'):
                if current.get(field):
                    payload[field] = current[field]
            if current.get('is_active') is False:
                payload['is_active'] = False
        payload['created_at'] = (current or {}).get('created_at') or now
        payload['updated_at'] = now
        rows.append(('users', faculty_ids[code], payload))
    writer.write_many(rows)

    # -- students ------------------------------------------------------------
    new_students = sorted(
        roll for roll in plan['students'] if roll not in by_roll
    )
    student_ids = dict(by_roll)
    if new_students:
        block = _reserve_ids(store, 'users', len(new_students))
        for roll, user_id in zip(new_students, block):
            student_ids[roll] = user_id

    rows = []
    for roll in sorted(plan['students']):
        current = existing.get(student_ids[roll])
        payload = {
            'id': student_ids[roll],
            'roll_number': roll,
            # No email and no Microsoft id on purpose. The mailbox is named after
            # the roll number, so the first sign-in resolves this row from the
            # address rather than the other way round.
            'email': (current or {}).get('email'),
            'microsoft_id': (current or {}).get('microsoft_id'),
            'name': (current or {}).get('name') or roll,
            'phone': (current or {}).get('phone'),
            'role': 'STUDENT',
            'department': 'CSE',
            'is_active': True,
        }
        payload['created_at'] = (current or {}).get('created_at') or now
        payload['updated_at'] = now
        rows.append(('users', student_ids[roll], payload))
    writer.write_many(rows)

    # -- sections as classes, students as their rosters ----------------------
    # Written straight through rather than through ClassModel/MemberModel: those
    # are per-document writes, and 1138 roster rows one at a time is the same
    # round-trip problem this script exists to avoid. The fields written are the
    # same ones those models write, so the existing pages read them unchanged.
    # One class group per (section, subject, faculty), not one per section.
    #
    # A section is taught by a team, so a single group per section would have had
    # no lecturer to own it and would have appeared in nobody's "My Classes" --
    # attendance and permission routing both follow the teacher, not the section.
    # Dividing by subject as well is what makes the split meaningful: two
    # lecturers can both teach T1-General, but only one of them teaches ADSAA in
    # it, and that is the class whose roster and attendance belong to them.
    #
    # Named `<label>-<subject>-<employee number>`: "T1-ADSAA-6113". Short
    # enough to scan in a list and to type in a URL, and the three parts together
    # still say exactly which teacher runs which subject in which section.
    labels = _short_section_labels(plan['rosters'])
    group_specs = []
    for section in sorted(plan['rosters']):
        for code in sorted(plan['rosters'][section]['courses']):
            course = plan['rosters'][section]['courses'][code]
            for employee in sorted(course['teachers']):
                faculty_id = faculty_ids.get(employee)
                if faculty_id is None:
                    continue
                subject = _subject_for(plan, section, code) or code
                group_specs.append({
                    'name': f'{labels[section]}-{subject}-{employee}',
                    'section': section,
                    'course_code': code,
                    'course_name': course['course_name'],
                    'subject_code': subject,
                    'employee_code': employee,
                    'faculty_id': faculty_id,
                    # This teacher's own batch of students, never the course's
                    # combined list. The export splits a shared course into
                    # disjoint batches -- 2501IT07 in T6-T-HUB is 67 students
                    # under 6817 and 42 under 6848 -- so handing every teacher
                    # the whole course puts each student in a class they are not
                    # on the roll for, which is what the class page then shows.
                    'rolls': sorted(course['teachers'][employee]['rolls']),
                })

    # Existing groups are matched on (section, subject, employee) rather than on
    # the display name. The name is derived, so it changes whenever the naming
    # scheme does -- and a lookup keyed on it would then find nothing, create a
    # second group for every class, and orphan the original along with its
    # roster. Keyed on the natural key, a rename is just an edit in place.
    def _group_key(row):
        return (row.get('section_code'), row.get('subject_code'),
                row.get('employee_code'))

    existing_groups = {}
    legacy = []
    owner = min(
        (faculty_ids[c] for c, r in staff.items()
         if _role_for(r) == 'HOD'),
        default=None,
    )
    for row in store.documents('class_groups'):
        key = _group_key(row)
        if all(key):
            existing_groups.setdefault(key, row)
        elif (row.get('name') in plan['rosters']
              and row.get('faculty_id') == owner):
            # A section-wide group from before the split by subject: it has no
            # subject and no teacher of its own. Scoped to one this script
            # owned, so a lecturer's own class that happens to share a section's
            # name is never touched.
            legacy.append(row)

    for row in legacy:
        store.delete_where('class_members', class_id=row['id'])
        store.delete('class_groups', row['id'])
    if legacy:
        writer.skipped += len(legacy)
        print(f'  retired {len(legacy)} section-wide group(s) '
              f'split into subject groups')

    renamed = 0
    group_ids = {}
    for spec in group_specs:
        current = existing_groups.get(
            (spec['section'], spec['subject_code'], spec['employee_code']))
        group_ids[spec['name']] = (current or {}).get('id')
        if current is not None and current.get('name') != spec['name']:
            renamed += 1

    missing_groups = [spec['name'] for spec in group_specs
                      if group_ids[spec['name']] is None]
    if missing_groups:
        block = _reserve_ids(store, 'class_groups', len(missing_groups))
        for name, group_id in zip(missing_groups, block):
            group_ids[name] = group_id
    if renamed:
        print(f'  renamed {renamed} existing class group(s) in place '
              f'(id and roster kept)')

    rows = []
    for spec in group_specs:
        group_id = group_ids[spec['name']]
        current = existing.get(group_id) or {}
        rows.append(('class_groups', group_id, {
            'id': group_id,
            'name': spec['name'],
            'section_code': spec['section'],
            'academic_year': '2026',
            # The lecturer who teaches it. This is what puts the class in their
            # "My Classes" and makes them its attendance marker.
            'faculty_id': spec['faculty_id'],
            'kind': 'CLASS',
            'subject_code': spec['subject_code'],
            'course_code': spec['course_code'],
            'course_name': spec['course_name'],
            'employee_code': spec['employee_code'],
            'created_at': current.get('created_at') or now,
            'updated_at': now,
        }))
    writer.write_many(rows)

    # The id of the group a section's course resolves to, when it is
    # unambiguous -- one teacher for that course in that section. A course with
    # several teachers has no single class until the department allocates the
    # periods, so it is left unresolved rather than pinned to an arbitrary one.
    course_group = {}
    for spec in group_specs:
        key = (spec['section'], spec['course_code'])
        course_group.setdefault(key, set()).add(group_ids[spec['name']])

    # Roster rows are keyed on an allocated integer id, not a composed one.
    #
    # `ClassMember.id` is declared as an int and the delete route is
    # `/faculty/classes/<int:class_id>/members/<int:member_id>/delete`, so a
    # document id like "1:25B11CS015" made every roster page raise ValueError on
    # `_to_member` and made member deletion unreachable -- the URL converter would
    # never match. Attendance uses a composed id because nothing routes by it;
    # roster rows are routed by it, so they take a real id like every other
    # collection here.
    existing_members = {
        (row.get('class_id'), row.get('roll_number')): row.get('id')
        for row in store.documents('class_members')
    }
    wanted_members = [
        (group_ids[spec['name']], roll)
        for spec in group_specs
        for roll in spec['rolls']
    ]
    wanted_set = set(wanted_members)
    missing_members = [entry for entry in wanted_members
                       if entry not in existing_members]

    member_ids = {}
    if missing_members:
        block = _reserve_ids(store, 'class_members', len(missing_members))
        for entry, member_id in zip(missing_members, block):
            member_ids[entry] = member_id

    rows = []
    for class_id, roll in wanted_members:
        member_id = member_ids.get((class_id, roll)) \
            or existing_members.get((class_id, roll))
        current = store.get('class_members', member_id) if member_id else None
        rows.append(('class_members', member_id, {
            'id': member_id,
            'class_id': class_id,
            'roll_number': roll,
            'student_id': student_ids.get(roll),
            'enrolled': True,
            'created_at': (current or {}).get('created_at') or now,
            'updated_at': now,
        }))

    # Prune any roster row the export does not put on this class. Two cases, and
    # the second is the one that bites: a student moved to another teacher's
    # batch, or a course's roster was misread earlier. The class still exists
    # and the row still points at it, so pruning by "class no longer exists"
    # alone leaves the student on a roll they are not on. A roster is a
    # statement about who is in the room, so it has to be restated every run.
    stale = 0
    for row in store.documents('class_members'):
        if (row.get('class_id'), row.get('roll_number')) not in wanted_set:
            store.delete('class_members', row['id'])
            stale += 1
    if stale:
        writer.skipped += stale
        print(f'  removed {stale} roster row(s) the export no longer lists '
              f'for that class')
    writer.write_many(rows)

    # -- courses and their teaching pools ------------------------------------
    rows = []
    for name in sorted(plan['rosters']):
        for code in sorted(plan['rosters'][name]['courses']):
            course = plan['rosters'][name]['courses'][code]
            doc_id = f'{name}:{code}'
            rows.append(('section_courses', doc_id, {
                'id': doc_id,
                'section': name,
                # The single class this course resolves to, when one teacher runs
                # it. With a team there is no one class yet, so this stays null
                # rather than pointing at whichever teacher happened to be first.
                'class_id': _sole_group(course_group.get((name, code))),
                'course_code': code,
                'course_name': course['course_name'],
                'subject_code': _subject_for(plan, name, code),
                'eligible_faculty_ids': sorted(
                    faculty_ids[c] for c in course['faculty'] if c in faculty_ids),
                'eligible_employee_codes': sorted(course['faculty']),
                'roll_count': len(course['rolls']),
                'created_at': now,
                'updated_at': now,
            }))
    writer.write_many(rows)

    # -- period slots --------------------------------------------------------
    rows = []
    for number, start in sorted(plan['slots'].items()):
        doc_id = f'P{number}'
        ends = _slot_end(plan, number, start)
        rows.append(('timetable_slots', doc_id, {
            'id': doc_id,
            'period': number,
            'start': start,
            'end': ends,
            'updated_at': now,
        }))
    writer.write_many(rows)

    # -- the periods themselves ---------------------------------------------
    # faculty_id stays null: the exports name a pool per course, never a person
    # per slot. The pool is carried so the HOD's allocation page can offer the
    # eligible teachers, and a null faculty_id is what marks a period unclaimed.
    rows = []
    unclaimed = 0
    for name in sorted(plan['timetable']):
        section = plan['timetable'][name]
        linked = plan['links']['linked'].get(name, {})
        pools = {
            code: sorted(faculty_ids[c] for c in course['faculty']
                         if c in faculty_ids)
            for code, course in plan['rosters'].get(name, {'courses': {}})['courses'].items()
        }
        for period in section['periods']:
            subject = period['subject'].upper()
            code = linked.get(subject)
            doc_id = f'{name}:{period["day"]}:P{period["period"]}'
            rows.append(('timetable_periods', doc_id, {
                'id': doc_id,
                'section': name,
                # Resolved only where one teacher runs the course. The period has
                # no owner yet, so pointing it at a specific teacher's class would
                # be a claim about who is in that room that nobody has made.
                'class_id': _sole_group(course_group.get((name, code))),
                'day': period['day'],
                'period': period['period'],
                'start': period['start'],
                'end': period['end'],
                'subject_code': subject,
                'course_code': code,
                # Empty until the HOD allocates. See the module docstring.
                'faculty_id': None,
                'eligible_faculty_ids': pools.get(code, []),
                'allocated_at': None,
                'created_at': now,
                'updated_at': now,
            }))
            unclaimed += 1
    writer.write_many(rows)

    return {
        'users_written': len(staff) + len(plan['students']),
        'sections': len(plan['rosters']),
        # Students in the section, counted once each. Not the same as the
        # membership rows below: a student sits in several courses, so the two
        # figures are unrelated and reporting one under the other's label is how
        # a roster that is twice the right size goes unnoticed.
        'students': sum(len(s['rolls']) for s in plan['rosters'].values()),
        # One row per class per student, which is the export's own row count.
        'roster_rows': sum(
            len(t['rolls'])
            for s in plan['rosters'].values()
            for c in s['courses'].values()
            for t in c['teachers'].values()),
        'section_courses': sum(
            len(s['courses']) for s in plan['rosters'].values()),
        'periods': unclaimed,
        'created': writer.created,
        'updated': writer.updated,
    }


def _short_section_labels(sections):
    """The part of each section name that identifies it, e.g. 'T1-General' -> 'T1'.

    A class is named ``<label>-<subject>-<employee number>``, and the descriptive
    half of a section name ("General", "T-HUB", "Smart Interviews") only makes
    the name longer without making it clearer: the year is on every row already
    and the full section is kept in ``section_code``, which is what the filters
    and the timetable run on.

    The leading word is only used while it stays unique. If two sections ever
    began with the same word, the clash would silently merge two different
    classes' names, so a section that would collide falls back to its full name.
    """
    leading = {}
    for section in sections:
        head = section.split('-')[0].strip()
        leading.setdefault(head, []).append(section)

    labels = {}
    for section in sections:
        head = section.split('-')[0].strip()
        labels[section] = head if len(leading[head]) == 1 else section
    return labels


def _subject_for(plan, section, course_code):
    """The short subject code for a course, in this section."""
    for subject, code in plan['links']['linked'].get(section, {}).items():
        if code == course_code:
            return subject
    return None


def _sole_group(group_ids):
    """The one group id in a set, or None when a course has a teaching team.

    Some courses in this department are run by one lecturer and some are shared
    across two or three. A single teacher means the (section, subject) resolves
    to exactly one class; a team means it does not resolve at all yet, because
    which of them is in the room on a given period is the thing the allocation
    page exists to decide. Returning the first id would make the data look
    settled when it is not.
    """
    return next(iter(group_ids)) if group_ids and len(group_ids) == 1 else None


def _slot_end(plan, number, start):
    """The end time seen for a period number, or None."""
    ends = defaultdict(lambda: defaultdict(int))
    for section in plan['timetable'].values():
        for period in section['periods']:
            if period['end']:
                ends[period['period']][period['end']] += 1
    counts = ends.get(number)
    return max(counts, key=counts.get) if counts else None


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key, default in DEFAULT_FILES.items():
        parser.add_argument(f'--{key}', default=default,
                            help=f'path to the {key} export')
    parser.add_argument('--dry-run', action='store_true',
                        help='parse and report, write nothing')
    args = parser.parse_args()

    files = {key: getattr(args, key) for key in DEFAULT_FILES}

    try:
        plan = build_plan(files)
    except ExportError as exc:
        print(f'Could not read the exports: {exc}', file=sys.stderr)
        return 2

    print(report(plan))

    if args.dry_run:
        print('\nDry run: nothing written.')
        return 0

    # The store the app itself would use, so a run cannot target a different
    # database than the one being served. `selected_store` was chosen at import
    # time from DATABASE_BACKEND; only its configuration is supplied here.
    store = selected_store
    store.init_app(_Config())
    if getattr(store, 'driver', 'firestore') == 'firestore' \
            and getattr(store, 'client', None) is None:
        print('Firestore is not configured. Set FIREBASE_PROJECT_ID and '
              'FIREBASE_CREDENTIALS_PATH (or _JSON).', file=sys.stderr)
        return 1

    print(f'\nWriting to {store.target}')

    try:
        summary = write(plan, store)
    except Exception as exc:
        print(f'Import failed: {exc}', file=sys.stderr)
        return 1
    finally:
        close = getattr(store, 'close', None)
        if close:
            # Only meaningful on the SQL store, and it matters there: a script that
            # has written a SQLite file cannot delete or move it until the handle
            # is released.
            close()

    print('\nWritten:')
    for key, value in summary.items():
        print('    %-22s %s' % (key, value))
    print('\nNext: allocate the %d unclaimed periods before using leave '
          'adjustments.' % summary['periods'])
    return 0


if __name__ == '__main__':
    sys.exit(main())

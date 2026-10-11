"""Check the imported college data against the export it came from.

This is deliberately not part of ``verify.py``. That script runs against
synthetic fixtures and must pass on a fresh checkout, where none of the college
export has been imported and none of the paths below exist. These checks are the
opposite: they are only meaningful once ``import_college_data.py`` has run and the
source spreadsheets are on disk.

    python migrations/import_college_data.py
    python migrations/check_college_data.py

Exits non-zero if anything disagrees, so it can gate a data load.
"""

import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), '.env'))

from app.models.classes import ClassModel  # noqa: E402
from app.models.firestore import store  # noqa: E402
from app.models.user import UserModel  # noqa: E402
from app.utils.college_export import parse_section_rosters  # noqa: E402

# Where the export lives. Overridable so this can run against a copy of the
# spreadsheets without editing the file.
DOWNLOADS = os.environ.get(
    'COLLEGE_EXPORT_DIR', r'C:\Users\PARDHU\Downloads')
ROSTER_FILE = os.path.join(DOWNLOADS, 'timetable_choost_Students.xls')

ok, fail = [], []


def check(label, condition, detail=''):
    (ok if condition else fail).append(label)
    print(f"  {'OK  ' if condition else 'FAIL'}  {label}"
          + (f'   [{detail}]' if detail else ''))


def group(label):
    print(f'\n{label}')


if not os.path.exists(ROSTER_FILE):
    print(f'Cannot find the export at {ROSTER_FILE}.')
    print('Set COLLEGE_EXPORT_DIR to the folder holding the spreadsheets.')
    sys.exit(2)

print(f'backend: {store.target}')

groups = store.documents('class_groups')
members = store.documents('class_members')
courses = store.documents('section_courses')
users = store.documents('users')
staff = {u['id']: u for u in users if u.get('role') in ('LECTURER', 'HOD')}

print(f'\n{len(groups)} class groups, {len(members)} roster rows, '
      f'{len(courses)} section courses')

# --------------------------------------------------------------------------
group('1. the split is by section, subject and faculty')

check('every group is named section-subject-employee',
      all(g['name'].count('-') >= 2 for g in groups),
      groups[0]['name'] if groups else 'none')

check('group names are unique',
      len({g['name'] for g in groups}) == len(groups))

sections = {c['section'] for c in courses}
check('no section-wide group survives',
      not any(g['name'] in sections for g in groups),
      f'{sum(1 for g in groups if g["name"] in sections)} left over')

by_subject = defaultdict(set)
for g in groups:
    by_subject[(g.get('section_code'), g.get('subject_code'))].add(
        g.get('employee_code'))

shared = {k: v for k, v in by_subject.items() if len(v) > 1}
single = {k: v for k, v in by_subject.items() if len(v) == 1}
print(f'    {len(by_subject)} (section, subject) pairs: '
      f'{len(shared)} shared by more than one teacher, '
      f'{len(single)} run by one')

check('some subjects are taught by several faculty',
      bool(shared), f'{len(shared)} of {len(by_subject)}')
check('some subjects are taught by one faculty',
      bool(single), f'{len(single)} of {len(by_subject)}')

rosters = parse_section_rosters(ROSTER_FILE)

# Every (section, course, teacher) triple in the export must have produced a
# group. A silently skipped triple is a class that exists on the timetable and
# cannot be marked, so this is compared against the export rather than against
# a remembered count.
wanted = set()
for section, data in rosters.items():
    for code, course in data['courses'].items():
        check_pool = set(course['faculty']) == set(course['teachers'])
        if not check_pool:
            print(f'    pool and batches disagree for {section} {code}')
        for employee in course['teachers']:
            wanted.add((section, code, employee))

present = {(g.get('section_code'), g.get('course_code'), g.get('employee_code'))
           for g in groups}
check('every teaching assignment in the export has a class',
      wanted <= present,
      f'{len(wanted - present)} missing of {len(wanted)}')
check('no class exists without a teaching assignment',
      present <= wanted, f'{len(present - wanted)} unexpected')

check('every course carries its section and course code',
      not [c for c in courses
           if not c.get('section') or not c.get('course_code')],
      f"{sum(1 for c in courses if not c.get('section') or not c.get('course_code'))} incomplete")

# --------------------------------------------------------------------------
group('2. a group belongs to the lecturer who teaches it')

check('every group is owned by a real staff account',
      all(g.get('faculty_id') in staff for g in groups),
      f"{sum(1 for g in groups if g.get('faculty_id') not in staff)} unowned")

check('no group is owned by a student or an outsider',
      all(g.get('faculty_id') in staff for g in groups))

owned = Counter(g.get('faculty_id') for g in groups)
check('ownership follows the teaching pool, not one owner per section',
      len(set(owned.values())) > 1,
      f'{len(set(owned.values()))} distinct teaching loads')

busiest = max(owned, key=owned.get)
owner = staff[busiest]
mine = ClassModel.find_for_faculty(owner['id'], kind='CLASS')
check('a lecturer sees their own subject classes',
      bool(mine), f'{owner["name"][:24]} -> {len(mine)}')
check('and sees nobody else\'s',
      all(c.faculty_id == owner['id'] for c in mine),
      f'{len(mine)} listed')
check('every class they are shown is one they actually teach',
      {c.name for c in mine} <= {g['name'] for g in groups
                                if g['faculty_id'] == owner['id']})

# --------------------------------------------------------------------------
group('3. rosters')

by_class = defaultdict(set)
for m in members:
    by_class[m.get('class_id')].add(m.get('roll_number'))

group_ids = {g['id'] for g in groups}
check('every group has a roster',
      group_ids <= set(by_class), f'{len(group_ids - set(by_class))} empty')
check('no roster row points at a group that is gone',
      not [m for m in members if m.get('class_id') not in group_ids],
      f"{sum(1 for m in members if m.get('class_id') not in group_ids)} orphans")
check('every roster row is linked to a student account',
      all(m.get('student_id') for m in members),
      f"{sum(1 for m in members if not m.get('student_id'))} unlinked")
check('no duplicate (class, roll) pairs',
      all(v == 1 for v in Counter((m.get('class_id'), m.get('roll_number'))
                                  for m in members).values()))

# The unit of enrolment is the teaching pair, not the course. The export names
# one teacher per student per course, so a group must hold that teacher's batch
# and no other. Compared group by group against the export.
wrong = []
for g in groups:
    course = rosters.get(g['section_code'], {}).get(
        'courses', {}).get(g['course_code'])
    teacher = (course or {}).get('teachers', {}).get(g.get('employee_code'))
    if teacher is None:
        wrong.append(g['name'])
    elif by_class[g['id']] != set(teacher['rolls']):
        wrong.append(g['name'])
check('each group holds exactly its own teacher\'s students',
      not wrong, f'{len(wrong)} of {len(groups)} differ'
      + (f' e.g. {wrong[:3]}' if wrong else ''))

# The regression this replaced gave every teacher the whole course, so the
# batches were only ever checked against that same wrong union. Assert the
# batches are genuinely disjoint and genuinely smaller than the course, which a
# flattened roster cannot be.
export_pairs = {}
for section, data in rosters.items():
    for code, course in data['courses'].items():
        for emp, teacher in course['teachers'].items():
            export_pairs[(section, code, emp)] = set(teacher['rolls'])

expected_rows = sum(len(v) for v in export_pairs.values())
check('membership rows equal the export\'s own enrolment rows',
      len(members) == expected_rows,
      f'{len(members)} stored, {expected_rows} in the export')

overlapping = 0
shared_courses = 0
by_course = defaultdict(dict)
for (section, code, emp), rolls in export_pairs.items():
    by_course[(section, code)][emp] = rolls
for (section, code), entries in by_course.items():
    if len(entries) < 2:
        continue
    shared_courses += 1
    union = set().union(*entries.values())
    if any(len(r) < len(union) for r in entries.values()):
        overlapping += 1
check('a shared course is split into disjoint batches, not duplicated',
      overlapping == shared_courses,
      f'{shared_courses} shared courses, {overlapping} genuinely partitioned')

# The exact case that was reported: 25B11CS010 sits in T6-T-HUB Agile under
# 6848, and must not appear under 6817.
roll = '25B11CS010'
agile_6817 = ('T6-T-HUB', '2501IT07', '6817')
agile_6848 = ('T6-T-HUB', '2501IT07', '6848')
check('the reported student is under the teacher the export names',
      roll in export_pairs.get(agile_6848, set()),
      '25B11CS010 -> RAPAKA RAHUL 6848')
check('and not under the colleague who shares that course',
      roll not in export_pairs.get(agile_6817, ()),
      '25B11CS010 -> not CHIKKALA LOVA LAKSHMI 6817')

name_of = {g['name']: g['id'] for g in groups}
for pair, expected in ((agile_6848, True), (agile_6817, False)):
    for g in groups:
        if (g.get('section_code'), g.get('course_code'),
                g.get('employee_code')) == pair:
            present = roll in by_class[g['id']]
            label = f"{g['name']} roster {'has' if expected else 'omits'} {roll}"
            check(label, present == expected,
                  f"{len(by_class[g['id']])} students on the roll")

# --------------------------------------------------------------------------
group('4. what a course resolves to')

resolved = [c for c in courses if c.get('class_id')]
check('a single-teacher course resolves to its one class',
      len(resolved) == len(single),
      f'{len(resolved)} resolved, {len(single)} single-teacher')
check('a teaching-team course resolves to no single class',
      all(c['class_id'] is None for c in courses
          if len(c.get('eligible_faculty_ids') or []) > 1),
      f"{sum(1 for c in courses if c['class_id'] is None and len(c.get('eligible_faculty_ids') or []) > 1)} unresolved")
check('every resolved class exists',
      all(c['class_id'] in group_ids for c in resolved))

# --------------------------------------------------------------------------
group('5. the split did not disturb the rest of the import')

students = [u for u in users if u.get('role') == 'STUDENT']
check('every student is still imported', len(students) == 1138,
      f'{len(students)}')
check('every lecturer still has an account',
      len(UserModel.get_lecturers()) >= 214,
      f'{len(UserModel.get_lecturers())}')
check('every course still carries its teaching pool',
      all(c.get('eligible_faculty_ids') for c in courses))
check('every course resolved its subject code',
      all(c.get('subject_code') for c in courses),
      f"{sum(1 for c in courses if not c.get('subject_code'))} missing")

periods = store.documents('timetable_periods')
check('the 250 timetable periods are still present', len(periods) == 250,
      f'{len(periods)}')

# --------------------------------------------------------------------------
print()
print('=' * 70)
print(f'{len(ok)} passed, {len(fail)} failed')
if fail:
    print()
    for item in fail:
        print('  FAILED:', item)
print('=' * 70)

store.close()
sys.exit(1 if fail else 0)

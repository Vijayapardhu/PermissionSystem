"""Parsers for the college's timetable and roster exports.

The department publishes three files, and none of them is in a format a browser
can hand straight to the app:

``faculty details.xls``
    Not an XLS at all -- Excel's "Save as Web Page" wraps the grid in a frameset
    and puts the real rows in a sibling ``<name>_files/sheet001.htm``. Reading the
    outer file yields a frameset definition and no data, so the indirection has to
    be followed. Plain HTTP would be nicer; this is what IT actually sends.

``timetables.xlsx``
    One sheet per section. A cell holds ``"09:30 AM-10:20 AM\\nOOPC++"`` -- the slot
    and the subject -- and the footer is a legend of
    ``"SUBJECT, employee-no-NAME(building,room)"``.

``timetable_choost_Students.xls``
    Columns ``Time Table | Course Code | Course Name | Emp.Code | Roll.No``. This
    is the file that joins sections to courses to faculty to students.

Two properties of the data are worth stating, because the importer depends on them
and neither is obvious from the file names.

**No cell says which faculty teaches it.** The legend and the roster both name a
*pool* of one to three faculty per course, while the grid says only when the
section meets. A course with four periods and three teachers does not divide
evenly, so the allocation genuinely is not in the files. The importer therefore
stores the pool and leaves each period unassigned for the HOD to allocate; see
``migrations/import_college_data.py``.

**The subject short code has no column of its own.** ``ADSAA`` in the grid is the
same course as ``2501CS10`` in the roster, but nothing states that. It is derived
by joining on the faculty set: both files list which teachers a course has, so a
subject and a course with the same teachers in the same section are the same
course. That join is exact on every section in the current export, which is a
stronger guarantee than guessing short codes from course names.

HTML is parsed with the standard library. The alternatives -- pandas, lxml,
xlrd -- are not installed, and pulling three transitive dependencies in to read a
file Excel already saved as markup would be a poor trade for a one-off import.
"""

import re
from collections import defaultdict
from html.parser import HTMLParser

# The weekday labels the grid uses, in the order they appear.
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat')

#: A grid cell: "09:30 AM-10:20 AM" on the first line, the subject below it.
CELL_RE = re.compile(
    r'^\s*(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)\s*(.*)$',
    re.S | re.I,
)

#: A legend entry: "ADSAA, 6694-RAVIKUMAR INAKOTI(Ramanujan Bhavan,RB-104)".
#:
#: The subject is non-greedy so the split lands on the *first* comma, which is
#: the one after the subject code. The trailing parenthetical is a building and
#: room, not part of the name, and it is kept out of the name by matching the
#: employee number rather than splitting on the comma.
LEGEND_RE = re.compile(r'^\s*(.+?)\s*,\s*(\d{2,4})\s*-\s*(.+?)\s*$', re.S)


class ExportError(Exception):
    """Raised when an export file cannot be read at all."""


# --------------------------------------------------------------------------
# HTML tables (the two ".xls" files)
# --------------------------------------------------------------------------

class _TableGrid(HTMLParser):
    """Rebuild a rectangular grid from an Excel-as-HTML export.

    Two details make this more than a regex over ``<td>``, and both would
    silently corrupt the data if ignored:

    - Excel omits trailing empty cells in a row, so a naive flatten shifts every
      later column left by however many blanks a row happened to end with.
    - A merged cell is emitted once and omitted from the rows below it, so a
      rowspan has to carry its value down or those rows come up short.

    Whitespace is collapsed per cell: Excel pretty-prints into nested ``<p>``
    elements, which otherwise yields values wrapped and double-spaced.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._cells = {}     # column -> text, for the row being built
        self._span = {}      # column -> rows a rowspan still covers
        self._carried = {}   # column -> text held down by a rowspan
        self._cursor = 0
        self._buffer = None
        self._cell_span = (1, 1)

    def _next_free(self):
        col = self._cursor
        while col in self._span or col in self._cells:
            col += 1
        return col

    def _place(self, text, colspan, rowspan):
        col = self._next_free()
        self._cells[col] = text
        for extra in range(1, colspan):
            self._cells[col + extra] = ''
        if rowspan > 1:
            for offset in range(colspan):
                self._span[col + offset] = rowspan
        self._cursor = col + colspan

    def _close_row(self):
        held = {}
        for col, remaining in self._span.items():
            if remaining - 1 > 0:
                self._span[col] = remaining - 1
                held[col] = self._cells.get(col) or self._carried.get(col) or ''
            else:
                self._span.pop(col, None)
        # Write the held value into this row too, so every row comes out the
        # same width and a reader can index columns positionally.
        for col, value in held.items():
            self._cells.setdefault(col, value)
        width = max(
            [len(self._cells) - 1] + list(self._cells)
            + list(self._carried) + list(held),
            default=-1,
        ) + 1
        self.rows.append([self._cells.get(c, '') for c in range(width)])
        self._cells = dict(held)
        self._carried = held
        self._cursor = (max(held) + 1) if held else 0

    def handle_starttag(self, tag, attrs):
        if tag in ('td', 'th'):
            attributes = {k.lower(): (v or '') for k, v in attrs}
            self._buffer = []
            try:
                self._cell_span = (
                    int(attributes.get('colspan') or 1),
                    int(attributes.get('rowspan') or 1),
                )
            except ValueError:
                self._cell_span = (1, 1)

    def handle_endtag(self, tag):
        if tag in ('td', 'th'):
            colspan, rowspan = self._cell_span
            self._place(' '.join(''.join(self._buffer or []).split()),
                        colspan, rowspan)
            self._buffer = None
        elif tag == 'tr':
            self._close_row()

    def handle_data(self, data):
        if self._buffer is not None:
            self._buffer.append(data)


def read_html_table(path):
    """Every table in an HTML file, as a list of equal-width rows of text."""
    try:
        with open(path, 'rb') as handle:
            raw = handle.read().decode('utf-8', 'replace')
    except OSError as exc:
        raise ExportError(f'Could not read {path}: {exc}') from exc

    parser = _TableGrid()
    parser.feed(raw)
    if parser._cells:
        parser._close_row()
    return parser.rows


def resolve_excel_html(path):
    """The file that actually holds the grid behind an Excel HTML export.

    ``faculty details.xls`` is a frameset whose rows live in a sibling folder
    that the workbook named for *itself*, not for the download -- here
    ``ecap_excel_20261002940_files/sheet001.htm``, while the download is called
    ``faculty details.xls``. So the folder is read out of the document rather than
    derived from the filename; deriving it is what made this look like a missing
    folder when the data was sitting right there.

    Returns the path unchanged when it holds a grid itself, which is the case
    when someone has saved the sheet directly rather than the workbook.
    """
    import os

    if not os.path.exists(path):
        raise ExportError(f'File not found: {path}')

    with open(path, 'rb') as handle:
        head = handle.read(8192).decode('utf-8', 'replace')

    # Only a frameset carries this; a plain sheet starts with the table.
    if '<frameset' not in head.lower():
        return path

    folder = os.path.dirname(path) or '.'
    # The frameset names the sheet it will load. Prefer that, because a
    # multi-sheet workbook can have a sheet001 that is not the one shown.
    reference = re.search(r'<link\s+id="shLink"\s+href="([^"]+)"', head, re.I)
    if reference:
        candidate = os.path.normpath(os.path.join(folder, reference.group(1)))
        if os.path.exists(candidate):
            return candidate

    listing = re.search(r'href="([^"]+\.htm[l]?)"', head, re.I)
    if listing:
        candidate = os.path.normpath(os.path.join(folder, listing.group(1)))
        if os.path.exists(candidate):
            return candidate

    raise ExportError(
        f'{path} is an Excel frameset and the sheet it points at is missing. '
        f'Copy the whole download -- the data sits in a folder Excel created '
        f'beside the file -- and run this again.'
    )


def _records(rows, header_index):
    """Rows as dicts keyed by the header row's labels."""
    width = max((len(r) for r in rows), default=0)
    header = (rows[header_index] + [''] * width)[:]
    header = [h.strip() for h in header]
    out = []
    for row in rows[header_index + 1:]:
        padded = (list(row) + [''] * width)[:width]
        record = {header[i]: padded[i].strip() for i in range(len(header))
                  if header[i]}
        if any(record.values()):
            out.append(record)
    return out


def parse_faculty_details(path):
    """Faculty master: one record per employee number.

    The header is not row 0 -- Excel writes blank spacer rows above it -- so it is
    located by looking for the row carrying ``EmployeeCode`` rather than assumed.
    """
    grid = read_html_table(resolve_excel_html(path))

    header_index = None
    for index, row in enumerate(grid[:20]):
        if any(cell.strip() == 'EmployeeCode' for cell in row):
            header_index = index
            break
    if header_index is None:
        raise ExportError(
            'No header row with an "EmployeeCode" column was found in the '
            'faculty export. The file layout may have changed.'
        )

    faculty = {}
    for record in _records(grid, header_index):
        code = (record.get('EmployeeCode') or '').strip()
        if not code:
            continue
        faculty[code] = {
            'employee_code': code,
            'name': ' '.join((record.get('EmployeeName') or '').split()),
            'email': (record.get('EmailId') or '').strip().lower() or None,
            'phone': (record.get('CellPhone') or '').strip() or None,
            'designation': ' '.join((record.get('Designation') or '').split()),
            'department': ' '.join(
                (record.get('DepartmentName') or '').split()) or None,
            'qualification': ' '.join(
                (record.get('Qualification') or '').split()) or None,
            'joining_date': (record.get('DOJ') or '').strip() or None,
            'gender': (record.get('Gender') or '').strip() or None,
            'pan': (record.get('PAN') or '').strip() or None,
        }
    if not faculty:
        raise ExportError('The faculty export parsed to zero records.')
    return faculty


def parse_section_rosters(path):
    """Section enrolment: which students sit in which course, under which teacher.

    Returns ``{section: {'rolls': set, 'courses': {code: {...}}}}``.

    Every row of the export names one course, one teacher and one student, so the
    unit of enrolment is the **teaching pair**, not the course. A course taught
    by two lecturers is split between them -- disjoint batches of students, one
    row per student per teacher -- and a student sitting that course appears
    under one of them, never both.

    That distinction is the whole reason this function keeps ``teachers``: given
    only the course's combined roll list there is no way to tell which lecturer a
    student belongs to, and handing every teacher the full list puts each student
    in a class they are not on the roll for.

    Each course therefore carries:

    ``faculty``    the employee numbers teaching it, as a pool. Used to match the
                   course against the timetable's subject short codes, which name
                   a team rather than a pair.
    ``teachers``   ``{employee: {'faculty_name', 'rolls'}}`` -- that teacher's own
                   batch. This is what a class group's roster is built from.
    ``rolls``      every student in the course across all teachers. Reporting
                   only; never use it to build a roster.
    """
    grid = read_html_table(resolve_excel_html(path))

    header_index = None
    for index, row in enumerate(grid[:20]):
        if any(cell.strip() == 'Roll.No' for cell in row):
            header_index = index
            break
    if header_index is None:
        raise ExportError(
            'No header row with a "Roll.No" column was found in the student '
            'export. The file layout may have changed.'
        )

    sections = {}
    unclaimed = 0
    for record in _records(grid, header_index):
        section = (record.get('Time Table') or '').strip()
        roll = (record.get('Roll.No') or '').strip().upper()
        code = (record.get('Course Code') or '').strip()
        employee = (record.get('Emp.Code') or '').strip()
        if not section or not roll or not code:
            continue

        entry = sections.setdefault(section, {'rolls': set(), 'courses': {}})
        entry['rolls'].add(roll)
        course = entry['courses'].setdefault(code, {
            'course_code': code,
            'course_name': ' '.join(
                (record.get('Course Name') or '').split()),
            'faculty': set(),
            'teachers': {},
            'rolls': set(),
        })
        course['rolls'].add(roll)
        if not employee:
            # No teacher on the row. The student is in the section and in the
            # course, but cannot be placed in a class, so it is counted rather
            # than silently added to somebody's roster.
            unclaimed += 1
            continue
        course['faculty'].add(employee)
        teacher = course['teachers'].setdefault(employee, {
            'faculty_name': ' '.join(
                (record.get('Emp.Name') or '').split()),
            'rolls': set(),
        })
        teacher['rolls'].add(roll)

    if not sections:
        raise ExportError('The student export parsed to zero sections.')
    if unclaimed:
        # The current export has none. Loud rather than silent, because the day
        # it is non-zero a roster will be short a student and nothing else would
        # say so.
        print(f'  note: {unclaimed} enrolment row(s) named no teacher and were '
              f'not added to any class roster')
    return sections


# --------------------------------------------------------------------------
# The timetable workbook (xlsx)
# --------------------------------------------------------------------------

def parse_timetable_workbook(path):
    """One entry per section: its period grid and its faculty legend.

    Returns ``{section: {'sheet': name, 'periods': [...], 'legend': {...}}}``
    with ``periods`` a flat list of
    ``{'day', 'period', 'start', 'end', 'subject'}`` and ``legend`` mapping a
    subject code to the employee numbers the sheet lists for it.
    """
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - openpyxl is a pinned dependency
        raise ExportError(
            'openpyxl is required to read the timetable workbook. '
            'Install it with: pip install openpyxl'
        )

    try:
        workbook = openpyxl.load_workbook(path, data_only=True)
    except OSError as exc:
        raise ExportError(f'Could not read {path}: {exc}') from exc

    sections = {}
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        grid = [list(row) for row in sheet.iter_rows(values_only=True)]

        label = ''
        for row in grid[:4]:
            for cell in row:
                if isinstance(cell, str) and 'Time Table' in cell:
                    label = cell.split(':', 1)[1].strip()
                    break
            if label:
                break
        if not label:
            raise ExportError(
                f'Sheet "{sheet_name}" has no "Time Table:" line, so the '
                f'section it belongs to cannot be identified.'
            )

        periods = []
        for row in grid:
            day = str(row[0]).strip() if row and row[0] else ''
            if day not in DAYS:
                continue
            for index, cell in enumerate(row[1:8], start=1):
                parsed = _parse_period_cell(cell)
                if parsed is None:
                    continue
                start, end, subject = parsed
                periods.append({
                    'day': day,
                    'period': index,
                    'start': start,
                    'end': end,
                    'subject': subject,
                })

        legend = defaultdict(set)
        for row in grid:
            # The legend is laid out in two merged column blocks (A:D and E:H),
            # so it is read from both rather than assuming a single column.
            for column in (0, 4):
                if len(row) <= column:
                    continue
                match = LEGEND_RE.match(str(row[column] or '').strip())
                if match:
                    legend[match.group(1).strip().upper()].add(
                        match.group(2).strip())

        sections[label] = {
            'sheet': sheet_name,
            'periods': periods,
            'legend': {code: sorted(staff) for code, staff in legend.items()},
        }

    if not sections:
        raise ExportError('The timetable workbook held no section sheets.')
    return sections


def _parse_period_cell(cell):
    """``"09:30 AM-10:20 AM\\nOOPC++"`` -> ``("09:30 AM", "10:20 AM", "OOPC++")``.

    A cell holding only a subject (no times) is accepted, since a timetable can
    list a session without spelling out its slot. An empty cell is skipped: blank
    slots are the norm, not an error.
    """
    if not cell:
        return None
    lines = [line.strip() for line in str(cell).splitlines() if line.strip()]
    if not lines:
        return None

    match = CELL_RE.match(lines[0])
    if match:
        start, end = match.group(1), match.group(2)
        subject = (match.group(3) or ' '.join(lines[1:])).strip()
        if not subject:
            subject = lines[-1]
        return start, end, subject

    if len(lines) == 1:
        return None, None, lines[0]
    return None, None, ' '.join(lines)


def link_subjects_to_courses(sections, rosters):
    """Resolve each grid subject code to a course code, per section.

    The two exports never state the link, but both list *which teachers* a course
    or subject has. A subject and a course in the same section with the same
    teachers are the same thing, so that is the join.

    Returns ``{section: {subject: course_code}}`` plus, on
    ``'unmatched'``, anything the join could not settle with a reason -- those are
    reported to the HOD rather than guessed, because a wrong link would send
    class adjustments to the wrong lecturer.
    """
    linked, unmatched = {}, {}

    for section, data in sections.items():
        roster = rosters.get(section, {'courses': {}})
        courses_by_staff = {
            frozenset(course['faculty']): code
            for code, course in roster['courses'].items()
        }
        resolved, missing = {}, []
        for subject, staff in data['legend'].items():
            course_code = courses_by_staff.get(frozenset(staff))
            if course_code:
                resolved[subject] = course_code
            else:
                missing.append(subject)

        linked[section] = resolved
        if missing:
            unmatched[section] = sorted(missing)

    return {'linked': linked, 'unmatched': unmatched}

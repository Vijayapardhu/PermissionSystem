"""Roster spreadsheet parsing.

Faculty upload an Excel or CSV sheet listing roll numbers. Only the roll number
matters, so parsing is deliberately tolerant: header rows are skipped, cells may
be numeric (Excel often stores 26B21CS058 oddly), and the first roll-shaped token
in each row wins.
"""

import csv
import io
import re

from werkzeug.datastructures import FileStorage

from app.models import is_roll_id

ALLOWED_ROSTER_EXTENSIONS = {'xlsx', 'xlsm', 'csv', 'txt'}

# Roll numbers look like 26B21CS058. Also tolerates a leading column letter,
# e.g. Excel rendering "26B21CS058" with a stray prefix.
_ROLL_TOKEN = re.compile(r'\b([0-9]{2}[A-Z0-9]{6,12})\b')


class RosterError(Exception):
    """Raised when the uploaded roster cannot be read at all."""


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def parse_faculty_roster(file_storage: FileStorage) -> dict:
    """Return {'entries': [{'name', 'email', 'row'}], 'errors': [...]}.

    The HOD uploads a spreadsheet of lecturers with Name and Email columns and
    each row becomes a LECTURER account. The header is found by content rather
    than position, so "Faculty Name | Email ID" works as well as "Name |
    Email". Rows with a missing name or a malformed email are reported rather
    than aborting the whole import, because a 40-row sheet should not fail on
    row 37.
    """
    filename = (file_storage.filename or '').lower()
    extension = filename.rsplit('.', 1)[-1] if '.' in filename else ''

    if extension not in ALLOWED_ROSTER_EXTENSIONS:
        raise RosterError(
            'Upload an Excel workbook (.xlsx, .xlsm) or a CSV file.'
        )

    file_storage.stream.seek(0)
    raw = file_storage.stream.read()
    file_storage.stream.seek(0)

    if not raw:
        raise RosterError('The uploaded file is empty.')

    if extension in ('xlsx', 'xlsm'):
        rows = _faculty_xlsx_rows(raw)
    else:
        rows = _faculty_delimited_rows(raw)

    header_at = None
    name_idx = email_idx = None
    for lineno, row in enumerate(rows):
        cells = [(str(cell).strip().lower() if cell is not None else '')
                 for cell in row]
        name_hit = next((i for i, cell in enumerate(cells)
                         if 'name' in cell), None)
        email_hit = next((i for i, cell in enumerate(cells)
                          if 'email' in cell or 'mail' in cell), None)
        if name_hit is not None and email_hit is not None:
            header_at, name_idx, email_idx = lineno, name_hit, email_hit
            break

    if header_at is None:
        raise RosterError(
            'No header row with Name and Email columns was found. '
            'The first row should name the columns, e.g. "Name, Email".'
        )

    entries, errors, seen = [], [], set()
    for lineno, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        name = str(row[name_idx]).strip() if len(row) > name_idx and row[name_idx] is not None else ''
        email = (str(row[email_idx]).strip().lower() if len(row) > email_idx and row[email_idx] is not None else '')
        if not name and not email:
            continue
        if not name:
            errors.append(f'Row {lineno}: missing name for {email or "this entry"}.')
            continue
        if not _EMAIL_RE.match(email):
            errors.append(f'Row {lineno}: "{email or "—"}" is not a valid email.')
            continue
        if email in seen:
            errors.append(f'Row {lineno}: {email} appears twice in the sheet.')
            continue
        seen.add(email)
        entries.append({'name': name, 'email': email, 'row': lineno})

    if not entries and not errors:
        raise RosterError('No faculty rows were found below the header row.')
    return {'entries': entries, 'errors': errors}


def _faculty_xlsx_rows(raw: bytes) -> list:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RosterError(
            'Excel support is unavailable. Install openpyxl or upload a CSV file.'
        )

    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise RosterError(f'The workbook could not be read: {exc}')

    try:
        sheet = workbook.worksheets[0]
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _faculty_delimited_rows(raw: bytes) -> list:
    for encoding in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 cannot fail
        raise RosterError('The file could not be decoded as text.')
    return [row for row in csv.reader(io.StringIO(text))]


def parse_roster(file_storage: FileStorage) -> list:
    """Return a list of uppercased roll numbers found in the uploaded sheet."""
    filename = (file_storage.filename or '').lower()
    extension = filename.rsplit('.', 1)[-1] if '.' in filename else ''

    if extension not in ALLOWED_ROSTER_EXTENSIONS:
        raise RosterError(
            'Upload an Excel workbook (.xlsx, .xlsm) or a CSV file.'
        )

    file_storage.stream.seek(0)
    raw = file_storage.stream.read()
    file_storage.stream.seek(0)

    if not raw:
        raise RosterError('The uploaded file is empty.')

    if extension in ('xlsx', 'xlsm'):
        rolls = _parse_xlsx(raw)
    else:
        rolls = _parse_delimited(raw)

    if not rolls:
        raise RosterError(
            'No roll numbers were found. Each row should contain one roll '
            'number such as 26B21CS058.'
        )
    return rolls


def _parse_xlsx(raw: bytes) -> list:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RosterError(
            'Excel support is unavailable. Install openpyxl or upload a CSV file.'
        )

    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise RosterError(f'The workbook could not be read: {exc}')

    rolls = []
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                rolls.extend(_extract_from_row(row))
    finally:
        workbook.close()

    return _dedupe(rolls)


def _parse_delimited(raw: bytes) -> list:
    # Tolerate UTF-8 with or without a BOM, and fall back to latin-1 which never
    # fails, so a mis-encoded CSV still imports rather than erroring.
    for encoding in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 cannot fail
        raise RosterError('The file could not be decoded as text.')

    rolls = []
    for row in csv.reader(io.StringIO(text)):
        rolls.extend(_extract_from_row(row))

    return _dedupe(rolls)


def _extract_from_row(row) -> list:
    """Pick roll numbers out of one spreadsheet row."""
    found = []

    for cell in row:
        if cell is None:
            continue

        if isinstance(cell, (int, float)):
            # Excel sometimes drops the leading zero or the letter; treat the
            # text form as a candidate and let is_roll_id decide.
            candidate = _pad_numeric_roll(cell)
            if candidate and is_roll_id(candidate):
                found.append(candidate.upper())
            continue

        text = str(cell).strip()
        if not text:
            continue

        if is_roll_id(text):
            found.append(text.upper())
            continue

        # A cell may hold "26B21CS058 - Ravi" or a full row pasted into one cell.
        for token in _ROLL_TOKEN.findall(text.upper()):
            if is_roll_id(token):
                found.append(token)

    return found


def _pad_numeric_roll(value) -> str:
    """Rebuild a roll number from an all-digit cell, e.g. 2621cs058 -> 26B21CS058."""
    text = str(value)
    if not text.isdigit():
        return ''

    # The university format is 2 digits + 1 letter + 2 digits + letters + 3 digits,
    # so an all-digit cell is missing only the leading letter of the programme.
    for letter in ('B',):
        for split in (2, 3):
            candidate = f'{text[:split]}{letter}{text[split:]}'
            if is_roll_id(candidate):
                return candidate
    return ''


def _dedupe(rolls) -> list:
    seen = set()
    ordered = []
    for roll in rolls:
        key = roll.upper()
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered
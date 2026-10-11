"""How a Python value is written to the store and read back again.

Both backends share this. That is not tidiness -- it is the thing that makes them
interchangeable. A date is stored as an ISO string and parsed back into a `date`;
a timestamp is stored as an instant and returned as the naive local datetime the
templates have always formatted. If the two implementations disagreed here, a
record written locally would read differently once deployed, and the difference
would only show up as a subtly wrong date on a letter.

Extracted from `firestore.py`, which still re-exports every name so existing
imports keep working.

Two rules the stores depend on:

- **Dates and times are text, timestamps are instants.** A `date` has no
  timezone, so storing it as a string and parsing it back is exact. A `datetime`
  is an instant, so it is normalised to UTC on the way in and converted to
  REPORT_TIMEZONE on the way out -- otherwise a request filed at 23:30 IST
  charts under the following UTC day.
- **Scalars keep their type.** A string "42" and the number 42 are different
  values, and the store treats them as different when indexing and filtering.
"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

#: Sentinel meaning "this value is not one the mapper should convert".
UNCHANGED = object()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_timestamp(value: datetime):
    """A datetime as an aware UTC instant.

    Naive input is read as UTC. That is deliberate: every caller used to pass
    `datetime.now()` into a `timestamptz` column on a session pinned to UTC, so
    the stored value was already a UTC instant with the wall clock mislabelled.
    Writing the instant it was, and converting on the way out, stops that drift
    from compounding.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_local_naive(value: datetime, tz):
    """A stored timestamp as the naive local datetime the app has always shown.

    Templates and letters format these directly, so they must keep displaying the
    same wall clock. Naive on purpose: a mix of naive and aware datetimes raises
    on comparison, and half the codebase compares against `datetime.now()`.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(tz).replace(tzinfo=None)


def to_python_value(value, tz):
    """One stored field back into Python, or report it unchanged.

    The shape is checked before anything is parsed. A reason is free text and must
    come back exactly as it went in, and `datetime.fromisoformat('2026-10-02')`
    happily returns a midnight datetime -- so the length and separators decide
    what is a string before it is parsed as one.
    """
    if isinstance(value, datetime):
        # Covers DatetimeWithNanoseconds, which is what a Firestore read returns,
        # and the plain aware datetime that insert() just wrote.
        return to_local_naive(
            value if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc), tz)

    if isinstance(value, str):
        if len(value) == 10 and value[4] == '-' and value[7] == '-':
            try:
                return date.fromisoformat(value)
            except ValueError:
                return UNCHANGED
        if len(value) >= 8 and value[2] == ':' and value[5] == ':':
            try:
                return time.fromisoformat(value)
            except ValueError:
                return UNCHANGED
        if len(value) >= 19 and value[10] in 'T ':
            try:
                # Parsed and then converted, rather than returned as parsed.
                # A string is what the SQL backend reads back, so skipping the
                # conversion here would hand that backend timestamps in UTC while
                # Firestore handed over local time -- the same record displaying
                # a different hour depending on where it was written.
                return to_local_naive(datetime.fromisoformat(value), tz)
            except ValueError:
                return UNCHANGED

    return UNCHANGED


def to_storage_value(value):
    """One Python value into something both backends can store.

    Order matters: `datetime` is a subclass of `date`, so it has to be tested
    first. Enums are resolved through their `.value`, which is how a `RequestStatus`
    becomes the string the columns and filters actually hold.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, datetime):
        return to_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if hasattr(value, 'value'):
        return value.value
    if isinstance(value, (list, tuple)):
        return [to_storage_value(item) for item in value]
    if isinstance(value, dict):
        # Mapped key by key rather than falling through to `str(value)`. A list
        # of dicts -- the amendment trail on a leave, for one -- used to be
        # stored as a list of their `repr`s, which reads back as a list of
        # strings and loses every field. Firestore stores a map natively and the
        # SQL store puts the whole document in a JSON column, so both can hold
        # one; only the value mapping had to learn it.
        return {str(key): to_storage_value(item)
                for key, item in value.items()}
    return str(value)


def to_json_value(value):
    """As `to_storage_value`, but with datetimes as text.

    JSON has no instant type, so a timestamp has to become a string before it can
    be serialised. Firestore does not need this -- its client library encodes an
    aware datetime natively -- which is why it lives beside the mapping rather than
    inside it: both backends must agree on the stored *text*, because
    `to_python_value` parses that text back either way.
    """
    stored = to_storage_value(value)
    if isinstance(stored, datetime):
        return stored.isoformat()
    if isinstance(stored, (list, tuple)):
        return [to_json_value(item) for item in stored]
    if isinstance(stored, dict):
        # Datetimes nested inside a map need the same treatment as ones inside a
        # list: JSON has no instant type, and `to_python_value` parses the text
        # back either way, so the two backends still agree on what is stored.
        return {key: to_json_value(item) for key, item in stored.items()}
    return stored


def index_value(value):
    """The pair of index columns a value is searchable by.

    Numbers go in `value_num`, everything else in `value_text`. Keeping them apart
    is what stops a filter for the string "42" matching the number 42, which is
    the same distinction the Firestore store makes by refusing to compare across
    types.

    A bool is indexed as a number because that is what it compares equal to in
    every query in this codebase (`is_active=True`), and separating them would
    mean every boolean filter carries a type conversion nobody would remember.
    """
    if isinstance(value, bool):
        return None, int(value)
    if isinstance(value, (int, float)):
        return None, float(value)
    return str(value), None

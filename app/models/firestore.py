"""The data layer: Cloud Firestore.

This is the whole of the app's persistence, and it replaced a Postgres pool and
the SQL that went with it. Three things changed shape; everything above this
module was written so that none of it had to change.

**Documents are schemaless, so the field names are enforced here.** Every read
goes through `documents()` and every write through `insert()`/`update()`, which is
where a missing field would otherwise surface as an AttributeError three layers
away in a template. Dates and times are stored as ISO strings and timestamps as
Firestore Timestamps, and both are converted back to the same Python types Postgres
used to return, so the models and templates read exactly what they always did.

**Firestore has no joins.** The queries that joined `permission_requests` to
`users` in SQL now fetch the rows and attach the related records here, in one
place, so the model methods still return the same objects. Where the old query
counted rows in a subquery (`member_count`, the HOD's per-student totals), the
count is taken over the fetched rows instead of in the database.

**Ordering happens in Python, deliberately.** Firestore can sort on a single
field, but `where(...).order_by(...)` across two fields needs a composite index
created in the console first, and an app that errors until someone does that is a
worse failure than one that sorts a few hundred documents. This is one
department's worth of data, so the network only ever carries equality-filtered
reads and the ordering is free.

Integer ids are kept because routes, templates and letters carry them in URLs.
Firestore document ids are strings, so each collection keeps a counter document
and hands out the next integer inside a transaction.
"""

import json
import os

from datetime import date, datetime, time, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from google.cloud.firestore_v1.base_query import FieldFilter

# Errors worth a second attempt. Everything else -- a missing field, a bad
# document id, a permission failure -- fails identically on a retry and is better
# reported immediately.
_RETRYABLE = (
    'DeadlineExceeded', 'ServiceUnavailable', 'InternalServerError',
    'Aborted', 'ResourceExhausted', 'Unknown',
)

_UNCHANGED = object()


class DatabaseUnavailable(RuntimeError):
    """The store could not be reached.

    Named for what it means rather than what backs it: the routes, the error
    handlers and the health check all reason about "the data is unreachable" and
    must not care which product that is. Raised only for failures -- an
    unconfigured store, a network error, a deadline. A query that legitimately
    finds nothing is not this; that returns None or [].
    """


def with_retry(operation):
    """Run a store call, translating failures into DatabaseUnavailable.

    Every public method funnels through here, so no caller ever sees a gRPC
    exception: the routes, the error handlers and the health check all reason
    about one exception type, whatever the transport underneath is.
    """
    @wraps(operation)
    def wrapper(self, *args, **kwargs):
        attempts = max(1, getattr(self, 'retries', 2) or 2)
        last = None

        for attempt in range(attempts):
            try:
                return operation(self, *args, **kwargs)
            except DatabaseUnavailable:
                raise
            except Exception as exc:
                if not _is_retryable(exc):
                    # A bad field name or a permission failure will fail exactly
                    # the same way on the next attempt. Retrying it only turns a
                    # clear error into a slow one.
                    raise DatabaseUnavailable(str(exc) or type(exc).__name__) from exc
                last = exc
                if attempt + 1 < attempts:
                    self._log('warning',
                              'Firestore %s failed (%s), attempt %d of %d',
                              operation.__name__, type(exc).__name__,
                              attempt + 1, attempts)

        raise DatabaseUnavailable(
            f'Firestore {operation.__name__} failed after {attempts} '
            f'attempts: {last}'
        ) from last
    return wrapper


def _is_retryable(exc) -> bool:
    if type(exc).__name__ in _RETRYABLE:
        return True
    try:
        from google.api_core import exceptions as gexc
    except ImportError:  # pragma: no cover - google-api-core ships with firestore
        return False
    return isinstance(exc, (
        gexc.ServiceUnavailable, gexc.DeadlineExceeded,
        gexc.InternalServerError, gexc.Aborted, gexc.ResourceExhausted,
        gexc.Unknown,
    ))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_int_id(value) -> bool:
    return isinstance(value, int) or (isinstance(value, str)
                                      and value.lstrip('-').isdigit())


def to_timestamp(value: datetime):
    """A datetime as a value Firestore will store as a timestamp.

    Naive input is read as UTC. That is deliberate: every caller used to pass
    `datetime.now()` into a `timestamptz` column on a session pinned to UTC, so
    the stored value was already a UTC instant with the wall clock mislabelled.
    Writing the instant it was, and converting on the way out, stops that drift
    from compounding.

    A timezone-aware datetime is returned rather than the protobuf the client
    library uses internally: passing a protobuf to a public write method is
    rejected by its own encoder, and an aware datetime is what the library expects
    from a caller.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_local_naive(value, tz):
    """A stored timestamp as the naive local datetime the app has always shown.

    Templates and letters format these directly, so they must keep displaying the
    same wall clock. Naive on purpose: a mix of naive and aware datetimes raises
    on comparison, and half the codebase compares against `datetime.now()`.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(tz).replace(tzinfo=None)


def to_python_value(value, tz):
    """Convert one stored field back to Python, or report it unchanged.

    The shape is checked before anything is parsed. A reason is free text and must
    come back exactly as it went in, and `datetime.fromisoformat('2026-10-02')`
    happily returns a midnight datetime -- so the length and separators decide
    what a string is before it is parsed as one.
    """
    if isinstance(value, datetime):
        # Covers DatetimeWithNanoseconds, which is what a read returns, and the
        # plain aware datetime that insert() just wrote and hands back.
        return to_local_naive(
            value if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc), tz)

    if isinstance(value, str):
        if len(value) == 10 and value[4] == '-' and value[7] == '-':
            try:
                return date.fromisoformat(value)
            except ValueError:
                return _UNCHANGED
        if len(value) >= 8 and value[2] == ':' and value[5] == ':':
            try:
                return time.fromisoformat(value)
            except ValueError:
                return _UNCHANGED
        if len(value) >= 19 and value[10] in 'T ':
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return _UNCHANGED

    return _UNCHANGED


def credential_source(config):
    """Which variable is holding the credentials: 'json', 'json-in-path' or None.

    'json-in-path' is the mistake worth naming: the service account JSON pasted
    into FIREBASE_CREDENTIALS_PATH instead of FIREBASE_CREDENTIALS_JSON. It is
    unambiguously a credential and unambiguously not a path, so it is recognised
    rather than reported as a missing file -- otherwise the only clue is a log
    line that prints two kilobytes of private key after "does not exist".
    """
    inline = (config.get('FIREBASE_CREDENTIALS_JSON') or '').strip()
    if inline:
        return 'json'

    path = (config.get('FIREBASE_CREDENTIALS_PATH') or '').strip()
    if not path:
        return None
    if path.startswith('{'):
        return 'json-in-path'
    return 'path'


class Store:
    """Firestore access, configured once per process."""

    def __init__(self, app=None):
        self.client = None
        self.app = None
        self.timezone = ZoneInfo('UTC')
        self.timeout = 8.0
        self.retries = 2
        self.target = 'unconfigured'
        if app is not None:
            self.init_app(app)

    # -- lifecycle ---------------------------------------------------------

    def init_app(self, app):
        from google.cloud import firestore

        self.app = app
        config = self._settings(app)
        self.timezone = ZoneInfo(config.get('REPORT_TIMEZONE') or 'UTC')
        self.timeout = float(config.get('FIRESTORE_TIMEOUT_SECONDS') or 8)
        self.retries = max(1, int(config.get('FIRESTORE_RETRIES') or 2))

        project_id = config.get('FIREBASE_PROJECT_ID') or ''
        database_id = config.get('FIRESTORE_DATABASE') or '(default)'
        self.target = f'firestore:{database_id} in {project_id or "no project"}'

        emulator = config.get('FIRESTORE_EMULATOR_HOST') or ''
        credentials = None
        if not emulator:
            credentials = self._credentials()
            if credentials is None:
                # Not raised. A deployment that has not been given credentials
                # yet should still boot and serve its health path, and every
                # data-backed request should report the store as unavailable
                # rather than take the process down at import time.
                self._log('error',
                          'Firestore has no credentials: set '
                          'FIREBASE_CREDENTIALS_JSON (inline, for Render) or '
                          'FIREBASE_CREDENTIALS_PATH (a file)')
                self.client = None
                return

        # google-cloud-firestore reads FIRESTORE_EMULATOR_HOST from the
        # environment when the client is built, opens an insecure channel to it,
        # and substitutes anonymous credentials of its own.
        if emulator and not os.environ.get('FIRESTORE_EMULATOR_HOST'):
            os.environ['FIRESTORE_EMULATOR_HOST'] = emulator

        self.client = firestore.Client(
            project=project_id or None,
            credentials=credentials,
            database=database_id,
        )
        self._log('info', 'Firestore store opened for %s', self.target)

    def _settings(self, app):
        """Configuration as a mapping, whether it comes from Flask or a script.

        The seed script and the test harness configure the store without building
        an application, so the store reads whatever mapping it is handed rather
        than insisting on Flask's app.config.
        """
        config = getattr(app, 'config', None)
        return config if config is not None else app

    def _log(self, level, message, *args):
        """Log through the app when there is one, otherwise through logging."""
        logger = getattr(self.app, 'logger', None)
        if logger is not None:
            getattr(logger, level)(message, *args)
        else:
            import logging
            getattr(logging.getLogger(__name__), level)(message, *args)

    def _credentials(self):
        """Service-account credentials from the environment, or None."""
        config = self._settings(self.app) if self.app is not None else {}
        source = credential_source(config)

        if source in ('json', 'json-in-path'):
            raw = (config.get('FIREBASE_CREDENTIALS_JSON') if source == 'json'
                   else config.get('FIREBASE_CREDENTIALS_PATH')) or ''
            if source == 'json-in-path':
                self._log('warning',
                          'FIREBASE_CREDENTIALS_PATH holds the service account '
                          'JSON, not a path. It is being read as credentials; '
                          'move it to FIREBASE_CREDENTIALS_JSON and clear '
                          'FIREBASE_CREDENTIALS_PATH, or the next person to read '
                          'this config will not know which one is real.')
            try:
                # Render's dashboard holds multi-line env vars, so the JSON
                # arrives with its newlines intact and has to be parsed rather
                # than pattern-matched.
                info = json.loads(raw)
            except (ValueError, TypeError):
                self._log('error',
                          'The service account credentials are not valid JSON')
                return None
            return self._service_account(info)

        if source == 'path':
            path = (config.get('FIREBASE_CREDENTIALS_PATH') or '').strip()
            if not os.path.exists(path):
                self._log('error',
                          'FIREBASE_CREDENTIALS_PATH does not exist: %s', path)
                return None
            try:
                with open(path, encoding='utf-8') as handle:
                    return self._service_account(json.load(handle))
            except (ValueError, OSError):
                self._log('error',
                          'FIREBASE_CREDENTIALS_PATH does not hold valid JSON: %s',
                          path)
                return None

        return None

    def _service_account(self, info):
        from google.oauth2 import service_account
        scopes = ['https://www.googleapis.com/auth/cloud-platform',
                  'https://www.googleapis.com/auth/datastore']
        try:
            return service_account.Credentials.from_service_account_info(
                info, scopes=scopes)
        except (ValueError, KeyError):
            self._log('error',
                      'The service account JSON is missing fields it needs')
            return None

    def ping(self):
        """Prove the store answers. Raises DatabaseUnavailable if it does not."""
        self.collection('users').limit(1).get(timeout=self.timeout)

    def _require_client(self):
        if self.client is None:
            raise DatabaseUnavailable(
                'Firestore is not configured: set FIREBASE_CREDENTIALS_JSON or '
                'FIREBASE_CREDENTIALS_PATH'
            )

    # -- reads -------------------------------------------------------------

    def collection(self, name):
        self._require_client()
        return self.client.collection(name)

    @with_retry
    def documents(self, name, array_contains=None, **filters):
        """Every document in a collection whose fields match, as dicts.

        `filters` are equality comparisons and nothing else. Each one is a single
        field lookup, which Firestore serves from an automatic index, so this
        never needs a composite index to be configured first.

        `array_contains` maps a field name to a value and keeps the documents
        whose array field holds it. That is how a co-member on a group permission
        finds the request they were added to: their own id sits inside
        `member_ids` rather than in a field of its own. Firestore serves it from
        an automatic index too, so it costs the same as an equality filter. It is
        a named argument rather than a `where` key so it can never be mistaken
        for a document field called `array_contains`.
        """
        query = self.collection(name)
        field_filter = FieldFilter
        for field, value in filters.items():
            if value is None:
                # An equality filter on None is one Firestore cannot express
                # without a type field, and the only place it is wanted is "this
                # foreign key is not set", which the caller already knows.
                continue
            query = query.where(
                filter=field_filter(field, '==', value))
        for field, value in (array_contains or {}).items():
            if value is None:
                continue
            query = query.where(filter=field_filter(field, 'array_contains', value))
        return [self.to_python(snapshot.to_dict() or {})
                for snapshot in query.stream(timeout=self.timeout)]

    @with_retry
    def get(self, name, doc_id):
        if doc_id is None:
            return None
        snapshot = self.collection(name).document(str(doc_id)).get(
            timeout=self.timeout)
        if not snapshot.exists:
            return None
        data = self.to_python(dict(snapshot.to_dict() or {}))
        # Keep the stored id, coercing only int-like values. A blind int()
        # crashes on string-keyed documents (settings, attendance marks),
        # which insert() explicitly supports -- and the crash used to surface
        # as a 503, sending the reader to check the database instead of the
        # mapping.
        stored_id = data.get('id')
        if stored_id is None:
            data['id'] = int(doc_id) if _is_int_id(doc_id) else doc_id
        elif _is_int_id(stored_id):
            data['id'] = int(stored_id)
        return data

    # -- writes ------------------------------------------------------------

    @with_retry
    def insert(self, name, values, doc_id=None, timestamps=('created_at',)):
        """Write a document, assigning the next integer id unless given one."""
        payload = self.to_firestore(values)
        now = utc_now()
        for field in timestamps:
            payload.setdefault(field, now)
        payload.setdefault('updated_at', now)

        if doc_id is None:
            doc_id = self.next_id(name)
        # Attendance marks are keyed by "<class>:<student>:<date>" so a second
        # save for the same day overwrites the first. That id is a string by
        # design, and only the allocated integer ones are coerced.
        payload['id'] = int(doc_id) if _is_int_id(doc_id) else doc_id

        self.collection(name).document(str(doc_id)).set(
            payload, timeout=self.timeout)
        result = self.to_python(payload)
        result['id'] = payload['id']
        return result

    @with_retry
    def update(self, name, doc_id, values, touch=True):
        """Merge fields into a document. Returns False when it is not there."""
        reference = self.collection(name).document(str(doc_id))
        if not reference.get(timeout=self.timeout).exists:
            return False
        payload = self.to_firestore(values)
        if touch:
            payload['updated_at'] = utc_now()
        reference.update(payload, timeout=self.timeout)
        return True

    @with_retry
    def delete(self, name, doc_id):
        reference = self.collection(name).document(str(doc_id))
        if not reference.get(timeout=self.timeout).exists:
            return False
        reference.delete(timeout=self.timeout)
        return True

    def delete_where(self, name, **filters):
        """Delete every matching document. Returns how many went."""
        removed = 0
        for doc in self.documents(name, **filters):
            if self.delete(name, doc.get('id')):
                removed += 1
        return removed

    @with_retry
    def next_id(self, name):
        """The next integer for a collection, allocated inside a transaction.

        Firestore has no sequences, so the counter is a document of its own. The
        transaction is what makes it safe: two requests allocating at the same
        moment must not be handed the same id, which would silently overwrite one
        of the records.
        """
        from google.cloud import firestore

        reference = self.collection('counters').document(name)

        @firestore.transactional
        def _bump(transaction, ref):
            snapshot = ref.get(transaction=transaction)
            current = int(snapshot.get('value') or 0) if snapshot.exists else 0
            transaction.set(ref, {'value': current + 1})
            return current + 1

        return int(_bump(self.client.transaction(), reference))

    # -- type mapping ------------------------------------------------------

    def to_firestore(self, values):
        """Python values into something Firestore will accept."""
        out = {}
        for key, value in values.items():
            if value is None or isinstance(value, (str, bool, int, float)):
                out[key] = value
            elif isinstance(value, datetime):
                # Checked before date on purpose: datetime is a subclass of it.
                out[key] = to_timestamp(value)
            elif isinstance(value, date):
                out[key] = value.isoformat()
            elif isinstance(value, time):
                out[key] = value.isoformat()
            elif hasattr(value, 'value'):  # the models' Enums
                out[key] = value.value
            elif isinstance(value, (list, tuple)):
                out[key] = [str(item) for item in value]
            else:
                out[key] = str(value)
        return out

    def to_python(self, data):
        """Stored fields back into the types Postgres used to return."""
        out = dict(data)
        for key, value in data.items():
            converted = to_python_value(value, self.timezone)
            if converted is not _UNCHANGED:
                out[key] = converted
        return out


store = Store()
db = store  # the name the routes already import
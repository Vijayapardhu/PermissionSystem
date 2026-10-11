"""A SQL implementation of the same store interface Firestore provides.

Every model, route and service in this app talks to `store` and knows nothing
about what is behind it. This module supplies that same interface on top of SQL,
so the same code runs against SQLite while developing and Postgres in
production, with no conditional logic anywhere above this line.

The interface it satisfies is the one `firestore.Store` exposes:
``documents``, ``get``, ``insert``, ``update``, ``delete``, ``delete_where``,
``next_id``, ``ping``, plus ``collection``/``batch`` for the import script.

**Why documents in a JSON column rather than a table per entity.** Firestore is
schemaless, so the collections above this line have no fixed shape: a user's
`designation` is present on staff and absent on students, and a period gained
`course_code` months after the collection started. A table per entity would mean
migrating every model before a single query could run, and re-migrating whenever
the college's exports change shape. So each document is one JSON row, and the
fields that are actually filtered get their own index rows.

That index is the part worth understanding. `doc_index` holds one row per
(collection, field, value, document), and a list contributes one row per element.
An equality filter or an array-membership test is therefore an index seek rather
than a scan of the JSON, which is what keeps `find_away_between` and the
availability search from degrading into full table reads.

**Deliberately portable SQL.** There is no UPSERT anywhere in this file. SQLite
and Postgres spell conditional inserts differently -- `INSERT OR IGNORE` versus
`ON CONFLICT DO NOTHING`, and the two disagree about qualifying a column in an
update -- so instead of translating dialect at every call site, every write is
"look, then insert or replace". That is a little more work per write and it is
the same statement on both, which is worth far more than the round trip.

**What this does not give you, stated plainly.** The records are still read one
collection at a time and joined in Python, because that is how the models were
written. On SQLite that is free, so the fan-out that exhausted the Firestore quota
stops mattering. It is the reason to move the timetable and adjustments onto real
tables later if the join ever needs to be one query. What this backend *does* add
over Firestore is real transactions -- `store.transaction()` -- so approving a
leave can be made atomic in a way a document store could not.
"""

import json
import os
import threading
from datetime import datetime, timezone

from app.models.firestore import DatabaseUnavailable
from app.models.values import (
    UNCHANGED, index_value, to_json_value, to_python_value, to_storage_value,
)

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS docs (
        collection  TEXT NOT NULL,
        doc_id      TEXT NOT NULL,
        data        TEXT NOT NULL,
        created_at  TEXT,
        updated_at  TEXT,
        PRIMARY KEY (collection, doc_id)
    )
    """,
    # One row per (collection, field, value, document). The primary key covers the
    # text lookups, which is what an equality filter and an array membership test
    # both become.
    """
    CREATE TABLE IF NOT EXISTS doc_index (
        collection  TEXT NOT NULL,
        field       TEXT NOT NULL,
        value_text  TEXT,
        value_num   DOUBLE PRECISION,
        doc_id      TEXT NOT NULL,
        PRIMARY KEY (collection, field, value_text, doc_id)
    )
    """,
    # Numeric lookups need their own index: the primary key is on value_text, so
    # filtering on a number would otherwise be a scan.
    """
    CREATE INDEX IF NOT EXISTS doc_index_num
        ON doc_index (collection, field, value_num)
    """,
    # Writing a document rewrites its index rows, which starts from "every index
    # row belonging to this document". Without this, a delete is a collection
    # scan.
    """
    CREATE INDEX IF NOT EXISTS doc_index_doc
        ON doc_index (collection, doc_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS counters (
        name   TEXT PRIMARY KEY,
        value  INTEGER NOT NULL
    )
    """,
)

DEFAULT_SQLITE_PATH = 'local-dev.db'

#: Marks the store as usable. `client` is None when it is not -- the test harness
#: sets it that way to simulate an unreachable database.
_USABLE = object()


class SqlStore:
    """The store interface, on SQL."""

    def __init__(self, app=None):
        self.app = None
        self.config = {}
        self.timezone = None
        self.timeout = 8.0
        self.retries = 2
        self.target = 'unconfigured'
        self.driver = None          # 'sqlite' or 'postgres'
        self._local = threading.local()
        # The liveness flag. SQL has no persistent client until the first query,
        # so this is a plain sentinel rather than a handle: set it to None and
        # every operation raises DatabaseUnavailable, exactly as an unreachable
        # Firestore does. That is what lets the outage tests drive either backend
        # the same way.
        self._client = _USABLE
        if app is not None:
            self.init_app(app)

    # -- lifecycle ---------------------------------------------------------

    def init_app(self, app):
        settings = getattr(app, 'config', None)
        settings = settings if settings is not None else app
        self.app = app
        self.config = settings

        from zoneinfo import ZoneInfo
        self.timezone = ZoneInfo(settings.get('REPORT_TIMEZONE') or 'UTC')
        self.timeout = float(settings.get('FIRESTORE_TIMEOUT_SECONDS') or 8)
        self.retries = max(1, int(settings.get('FIRESTORE_RETRIES') or 2))

        url = (settings.get('DATABASE_URL') or '').strip()
        if url:
            self.driver = 'postgres'
            self.target = 'postgres (from DATABASE_URL)'
        else:
            self.driver = 'sqlite'
            path = (settings.get('SQLITE_PATH') or '').strip() \
                or DEFAULT_SQLITE_PATH
            self.target = f'sqlite:{path}'

        self._log('info', 'SQL store opened for %s', self.target)

    def _settings(self, app=None):
        return self.config

    def _log(self, level, message, *args):
        logger = getattr(self.app, 'logger', None)
        if logger is not None:
            getattr(logger, level)(message, *args)
        else:
            import logging
            getattr(logging.getLogger(__name__), level)(message, *args)

    # -- connection --------------------------------------------------------

    def _connect(self):
        """One connection per thread.

        A connection is not safe to share between threads and gunicorn runs this
        app with several. Thread-local keeps each thread on its own connection
        without a pool, which is the right trade for a workload making a handful
        of short queries per request.
        """
        if self._client is None:
            # Refused here rather than at each call site, so clearing the flag
            # makes the whole store unavailable the way losing the database does.
            raise DatabaseUnavailable(
                'The database is not configured or not reachable: set '
                'DATABASE_URL for Postgres, or SQLITE_PATH for SQLite'
            )

        connection = getattr(self._local, 'connection', None)
        if connection is not None:
            return connection

        connection = (self._connect_postgres() if self.driver == 'postgres'
                      else self._connect_sqlite())
        self._local.connection = connection
        self._create_schema(connection)
        return connection

    def _connect_sqlite(self):
        import sqlite3

        path = (self.config.get('SQLITE_PATH') or '').strip() \
            or DEFAULT_SQLITE_PATH
        if path != ':memory:':
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)

        connection = sqlite3.connect(
            path, timeout=self.timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # WAL lets a reader and a writer work at the same time, which matters as
        # soon as two requests overlap.
        connection.execute('PRAGMA journal_mode=WAL')
        # A blocked write waits rather than failing, so a burst of concurrent
        # requests queues instead of erroring.
        connection.execute(f'PRAGMA busy_timeout={int(self.timeout * 1000)}')
        return connection

    def _connect_postgres(self):
        try:
            import psycopg
        except ImportError:  # pragma: no cover - psycopg ships in requirements
            raise DatabaseUnavailable(
                'DATABASE_URL is set but psycopg is not installed. '
                'Run: pip install "psycopg[binary]"'
            )
        return psycopg.connect(self.config['DATABASE_URL'], autocommit=True,
                               connect_timeout=int(self.timeout))

    def _create_schema(self, connection):
        """Create the tables once per connection.

        Idempotent DDL on every boot and every new thread. That costs a few
        statements and saves the alternative -- a migration step nobody remembers
        to run before the first query.
        """
        with cursor_of(connection) as cursor:
            for statement in SCHEMA:
                cursor.execute(statement)

    def _mark(self):
        """The parameter placeholder for the active dialect."""
        return '%s' if self.driver == 'postgres' else '?'

    def transaction(self):
        """A context manager giving a real multi-statement transaction.

        This is the capability a document store could not offer. Approving a leave
        writes the leave, the adjustments, the balance and an audit row, and those
        either all land or none do -- a half-written approval would show a leave
        granted against adjustments that were never closed.

        Nesting is allowed and joins rather than nesting: a store method called
        inside the block -- `insert`, `next_id`, `_put` -- uses the cursor that is
        already open. Neither database permits a second BEGIN, so without this the
        obvious thing to write (`with store.transaction(): store.insert(...)`)
        would raise rather than work.
        """
        return _Transaction(self)

    def _open_cursor(self):
        """A context manager yielding the cursor a write should use.

        Inside `transaction()` that is the already-open cursor, wrapped so that
        leaving the block does not commit or close it -- the outer transaction
        owns that. Outside one, a fresh transaction is started.
        """
        active = getattr(self._local, 'active_cursor', None)
        if active is not None:
            return _BorrowedCursor(active)
        return self.transaction()

    @property
    def client(self):
        """Deprecated: prefer `store.batch()`.

        Kept as an attribute rather than the property that used to be here,
        because the test harness sets it to None to simulate an unreachable store
        and a read-only property refused the assignment. It doubles as the
        liveness flag `_connect` checks, which is why it is an attribute and not
        derived.
        """
        return self._client

    @client.setter
    def client(self, value):
        self._client = value

    def batch(self):
        """The Firestore-style batch surface, used by the import script."""
        return _Batch(self)

    def collection(self, name):
        """A Firestore-shaped handle on one collection."""
        return _Collection(self, name)

    def ping(self):
        """Prove the database answers. Raises DatabaseUnavailable if it does not."""
        with cursor_of(self._connect()) as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()

    def close(self):
        """Release this thread's connection.

        Not needed to serve requests -- connections are thread-local and reused --
        but a script that writes a database file wants to be able to delete or move
        it afterwards, and SQLite will not let go of the file until the handle is
        closed.
        """
        connection = getattr(self._local, 'connection', None)
        if connection is None:
            return
        self._local.connection = None
        try:
            connection.close()
        except Exception:
            # Already closed, or a driver that does not need it. Either way there
            # is nothing useful to report and nothing left to do.
            pass

    # -- type mapping ------------------------------------------------------

    def to_firestore(self, values):
        """Python values into stored form.

        Named for the Firestore store's method so callers do not have to know
        which backend is configured.
        """
        return {key: to_json_value(value) for key, value in values.items()}

    def to_python(self, data):
        """Stored fields back into the types the models expect."""
        out = {}
        for key, value in data.items():
            if isinstance(value, list):
                out[key] = [self._one_back(item) for item in value]
            else:
                out[key] = self._one_back(value)
        return out

    def _one_back(self, value):
        """One stored value as Python, in the reporting timezone."""
        converted = to_python_value(value, self.timezone)
        return value if converted is UNCHANGED else converted

    # -- reads -------------------------------------------------------------

    def documents(self, name, array_contains=None, **filters):
        """Every document in a collection whose fields match.

        The same shape as the Firestore store's: equality comparisons only, plus
        `array_contains` for a field whose value is a list. Both become joins
        against `doc_index`, so neither depends on a schema.
        """
        conditions = dict(filters)
        for field, value in (array_contains or {}).items():
            # A list membership test is an equality test on one indexed element,
            # which is why arrays contribute a row per element in the first place.
            conditions[f'\x00contains\x00{field}'] = value

        if not conditions:
            return self._scan(name)

        mark = self._mark()
        params = []
        joins = []
        for index, (field, value) in enumerate(conditions.items()):
            alias = f'i{index}'
            column, converted = self._index_predicate(field, value)
            # The sentinel key marks a membership test; only the real field name
            # is stored in the index.
            indexed_field = (field.split('\x00')[-1]
                             if field.startswith('\x00') else field)
            joins.append(
                f'JOIN doc_index {alias}'
                f' ON {alias}.collection = d.collection'
                f' AND {alias}.doc_id = d.doc_id'
                f' AND {alias}.field = {mark}'
                f' AND {alias}.{column} = {mark}'
            )
            params.extend([indexed_field, converted])

        # The collection goes last because its placeholder is written last: the
        # joins come before the WHERE clause, and the parameter list has to follow
        # the order the placeholders actually appear in.
        params.append(name)

        # DISTINCT: a list holding the same element twice would otherwise yield
        # the same document twice.
        sql = (f'SELECT DISTINCT d.doc_id, d.data FROM docs d '
               + ' '.join(joins)
               + f' WHERE d.collection = {mark}')

        return [self.to_python(self._row_to_dict(row))
                for row in self._exec(sql, params)]

    def _index_predicate(self, field, value):
        """Which index column a filter value belongs in, and it as stored."""
        if isinstance(value, bool):
            # bool before int, because isinstance(True, int) is True in Python
            # and indexing it as a number would make the filter miss.
            return 'value_num', float(int(value))
        text, number = index_value(to_storage_value(value))
        if number is not None:
            return 'value_num', number
        return 'value_text', text

    def _scan(self, name):
        mark = self._mark()
        sql = f'SELECT doc_id, data FROM docs WHERE collection = {mark}'
        return [self.to_python(self._row_to_dict(row)) for row in self._exec(sql, [name])]

    def get(self, name, doc_id):
        if doc_id is None:
            return None
        mark = self._mark()
        sql = (f'SELECT doc_id, data FROM docs'
               f' WHERE collection = {mark} AND doc_id = {mark}')
        rows = self._exec(sql, [name, str(doc_id)])
        if not rows:
            return None
        data = self.to_python(self._row_to_dict(rows[0]))
        stored = data.get('id')
        if stored is None:
            data['id'] = coerce_id(doc_id)
        return data

    # -- writes ------------------------------------------------------------

    def insert(self, name, values, doc_id=None, timestamps=('created_at',)):
        """Write a document, assigning the next integer id unless given one."""
        payload = self.to_firestore(values)
        now = _now_text()
        for field in timestamps:
            payload.setdefault(field, now)
        payload.setdefault('updated_at', now)

        if doc_id is None:
            doc_id = self.next_id(name)
        doc_id = str(doc_id)
        payload['id'] = coerce_id(doc_id)

        self._put(name, doc_id, payload)
        result = self.to_python(payload)
        result['id'] = payload['id']
        return result

    def update(self, name, doc_id, values, touch=True):
        """Merge fields into a document. Returns False when it is not there."""
        doc_id = str(doc_id)
        mark = self._mark()
        current = self._exec(
            f'SELECT data FROM docs WHERE collection = {mark} AND doc_id = {mark}',
            [name, doc_id])
        if not current:
            return False

        payload = json.loads(current[0]['data'])
        payload.update(self.to_firestore(values))
        if touch:
            payload['updated_at'] = _now_text()
        self._put(name, doc_id, payload)
        return True

    def _put(self, name, doc_id, payload, cursor=None):
        """Replace a document and rebuild its index rows, in one transaction.

        Document and index move together or not at all. Writing the row first and
        indexing afterwards leaves a window where a filtered query cannot see a
        document that exists -- and on a partially applied approval that window is
        exactly the wrong thing to have.

        Joins an ambient transaction when there is one, so a caller writing several
        documents inside `store.transaction()` gets a single atomic unit.
        """
        if cursor is not None:
            self._write(name, doc_id, payload, cursor)
            return

        with self._open_cursor() as own:
            self._write(name, doc_id, payload, own)

    def _write(self, name, doc_id, payload, cursor):
        mark = self._mark()
        if self._exists(name, doc_id, cursor):
            self._exec(
                f'UPDATE docs SET data = {mark}, updated_at = {mark}'
                f' WHERE collection = {mark} AND doc_id = {mark}',
                [json.dumps(payload), payload.get('updated_at'),
                 name, doc_id], cursor)
        else:
            self._exec(
                f'INSERT INTO docs (collection, doc_id, data, created_at,'
                f' updated_at) VALUES ({mark}, {mark}, {mark}, {mark}, {mark})',
                [name, doc_id, json.dumps(payload),
                 payload.get('created_at'), payload.get('updated_at')], cursor)

        # Rebuild this document's index. Cheaper and much simpler than diffing it,
        # and the collections here are a few hundred rows. The rows were just
        # deleted, so a plain INSERT cannot collide -- which is what lets this
        # avoid ON CONFLICT, whose spelling differs between the two databases.
        self._exec(
            f'DELETE FROM doc_index WHERE collection = {mark} AND doc_id = {mark}',
            [name, doc_id], cursor)
        for field, value in payload.items():
            # `id` is indexed like any other field. Callers do filter on it --
            # `delete_where(name, id=...)` and the import script's batching both
            # rely on it -- and a field that cannot be found is worse than one
            # that costs an extra row.
            for item in (value if isinstance(value, list) else [value]):
                column, converted = self._index_predicate(field, item)
                self._exec(
                    f'INSERT INTO doc_index (collection, field, value_text,'
                    f' value_num, doc_id) VALUES ({mark}, {mark}, {mark},'
                    f' {mark}, {mark})',
                    [name, field,
                     converted if column == 'value_text' else None,
                     converted if column == 'value_num' else None, doc_id], cursor)

    def delete(self, name, doc_id):
        doc_id = str(doc_id)
        if not self._exists(name, doc_id):
            return False
        mark = self._mark()
        with self._open_cursor() as own:
            self._exec(
                f'DELETE FROM docs WHERE collection = {mark}'
                f' AND doc_id = {mark}', [name, doc_id], own)
            self._exec(
                f'DELETE FROM doc_index WHERE collection = {mark}'
                f' AND doc_id = {mark}', [name, doc_id], own)
        return True

    def delete_where(self, name, **filters):
        """Delete every matching document. Returns how many went."""
        removed = 0
        for row in self.documents(name, **filters):
            if self.delete(name, row.get('id')):
                removed += 1
        return removed

    def next_id(self, name):
        """The next integer for a collection.

        Read and write inside one transaction, and that is the point: two requests
        allocating at the same moment must not be handed the same id, which would
        silently overwrite one of the records.
        """
        mark = self._mark()
        with self._open_cursor() as cursor:
            rows = self._exec(
                f'SELECT value FROM counters WHERE name = {mark}', [name], cursor)
            current = int(rows[0]['value']) if rows else 0
            following = current + 1
            if rows:
                self._exec(f'UPDATE counters SET value = {mark} WHERE name = {mark}',
                           [following, name], cursor)
            else:
                self._exec(f'INSERT INTO counters (name, value) VALUES ({mark}, {mark})',
                           [name, following], cursor)
        return following

    def reserve_ids(self, name, count):
        """Hand out `count` consecutive ids in one round trip.

        `next_id` costs a transaction per id, which for a thousand-row import is a
        thousand round trips before the first write. The caller must be the only
        writer for that collection while this runs, which holds for an offline
        import.
        """
        mark = self._mark()
        with self._open_cursor() as cursor:
            rows = self._exec(
                f'SELECT value FROM counters WHERE name = {mark}', [name], cursor)
            current = int(rows[0]['value']) if rows else 0
            end = current + count
            if rows:
                self._exec(f'UPDATE counters SET value = {mark} WHERE name = {mark}',
                           [end, name], cursor)
            else:
                self._exec(f'INSERT INTO counters (name, value) VALUES ({mark}, {mark})',
                           [name, end], cursor)
        return list(range(current + 1, end + 1))

    def _exists(self, name, doc_id, cursor=None):
        mark = self._mark()
        return bool(self._exec(
            f'SELECT 1 FROM docs WHERE collection = {mark} AND doc_id = {mark}',
            [name, str(doc_id)], cursor))

    # -- plumbing ----------------------------------------------------------

    def _exec(self, sql, params, cursor=None):
        """Run a statement, returning any rows it produced."""
        if cursor is not None:
            cursor.execute(sql, list(params))
            return cursor.fetchall()
        with cursor_of(self._connect()) as own:
            own.execute(sql, list(params))
            return own.fetchall()

    @staticmethod
    def _row_to_dict(row):
        data = json.loads(row['data'])
        data['id'] = coerce_id(row['doc_id'])
        return data


def coerce_id(doc_id):
    """An integer id where the value looks like one, else the text.

    Mirrors the Firestore store: those records are keyed by
    "5:10:2026-10-09" and must stay text, while a request id must come back as
    the integer every route and template expects.
    """
    text = str(doc_id)
    return int(text) if text.lstrip('-').isdigit() else text


def cursor_of(connection):
    """A cursor that closes itself, on either driver.

    `sqlite3.Cursor` does not implement the context manager protocol while
    `psycopg.Cursor` does, so `with connection.cursor()` works on one backend and
    raises a TypeError on the other. This is the one place that difference is
    absorbed, so no SQL above it has to care which driver is connected.
    """
    import contextlib

    @contextlib.contextmanager
    def _cursor():
        cursor = connection.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    return _cursor()


def _now_text():
    return datetime.now(timezone.utc).isoformat()


class _Transaction:
    """BEGIN/COMMIT around a block, rolled back on any exception.

    Registered on the store's thread-local while open, so any store method called
    inside the block reuses this cursor instead of opening a second transaction.
    """

    def __init__(self, store):
        self.store = store
        self.cursor = None

    def __enter__(self):
        self.cursor = self.store._connect().cursor()
        self.cursor.execute('BEGIN')
        self.store._local.active_cursor = self.cursor
        return self.cursor

    def __exit__(self, exc_type, exc, tb):
        try:
            self.cursor.execute('COMMIT' if exc_type is None else 'ROLLBACK')
        finally:
            self.store._local.active_cursor = None
            self.cursor.close()
        return False

class _BorrowedCursor:
    """Yields an already-open cursor without taking ownership of it.

    Used when a store method is called inside `store.transaction()`: the work
    joins the caller's transaction, and committing or closing it here would end
    the unit the caller is still building.
    """

    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, exc_type, exc, tb):
        return False


class _Snapshot:
    """Enough of a Firestore snapshot for the import script's read-back."""

    def __init__(self, row):
        self._row = row

    @property
    def exists(self):
        return self._row is not None

    def to_dict(self):
        if self._row is None:
            return None
        return json.loads(self._row['data'])


class _Ref:
    """A single document handle: ``collection().document().get()/set()``."""

    def __init__(self, store, collection, doc_id):
        self.store = store
        self.collection_name = collection
        self.doc_id = str(doc_id)

    def get(self, timeout=None):
        rows = self.store._exec(
            f'SELECT doc_id, data FROM docs WHERE collection = {self.store._mark()}'
            f' AND doc_id = {self.store._mark()}',
            [self.collection_name, self.doc_id])
        return _Snapshot(rows[0] if rows else None)

    def set(self, payload, timeout=None):
        self.store._put(self.collection_name, self.doc_id, dict(payload))

    def update(self, payload, timeout=None):
        self.store.update(self.collection_name, self.doc_id, payload)

    def delete(self, timeout=None):
        return self.store.delete(self.collection_name, self.doc_id)


class _Doc:
    def __init__(self, row):
        self._row = row

    def to_dict(self):
        return self._row

    @property
    def exists(self):
        return True


class _Query:
    """The `limit(n).get()` half of the Firestore client surface."""

    def __init__(self, store, collection, limit=None):
        self.store = store
        self.collection_name = collection
        self._limit = limit

    def limit(self, count):
        return _Query(self.store, self.collection_name, limit=count)

    def where(self, *args, **kwargs):
        raise NotImplementedError(
            'Where clauses are not part of the store interface. Use '
            'store.documents(name, field=value) instead.'
        )

    def get(self, timeout=None):
        rows = self.store.documents(self.collection_name)
        if self._limit:
            rows = rows[:self._limit]
        return [_Doc(row) for row in rows]


class _Collection:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def document(self, doc_id):
        return _Ref(self.store, self.name, doc_id)

    def limit(self, count):
        return _Query(self.store, self.name, limit=count)

    def where(self, *args, **kwargs):
        raise NotImplementedError(
            'Where clauses are not part of the store interface.'
        )


class _Batch:
    """Firestore-style batching, so the import script is unchanged.

    Statements are collected and applied inside one transaction on commit -- the
    same atomicity Firestore gives, arrived at differently.
    """

    def __init__(self, store):
        self.store = store
        self._pending = []

    def set(self, ref, payload):
        self._pending.append((ref.collection_name, ref.doc_id,
                              dict(payload), 'set'))

    def update(self, ref, payload):
        self._pending.append((ref.collection_name, ref.doc_id,
                              dict(payload), 'merge'))

    def delete(self, ref):
        self._pending.append((ref.collection_name, ref.doc_id, None, 'delete'))

    def commit(self):
        mark = self.store._mark()
        with self.store.transaction() as cursor:
            for collection, doc_id, payload, mode in self._pending:
                if mode == 'delete':
                    self.store._exec(
                        f'DELETE FROM docs WHERE collection = {mark} AND doc_id = {mark}',
                        [collection, doc_id], cursor)
                    self.store._exec(
                        f'DELETE FROM doc_index WHERE collection = {mark} AND doc_id = {mark}',
                        [collection, doc_id], cursor)
                else:
                    # The cursor is passed through rather than letting _put open
                    # its own transaction: nesting BEGIN inside a transaction is
                    # an error on both databases.
                    self.store._put(collection, doc_id, payload, cursor)
        self._pending = []

"""Postgres access layer (Supabase).

The pool is per-process, so DB_POOL_MAX is multiplied by the number of gunicorn
workers when sizing connections against the Supabase connection limit. The
default of 6 x 2 workers leaves room well inside the free plan's limit.

Three distinct failures used to reach the browser as the same opaque "the user
directory is temporarily unavailable" message, so each is handled explicitly
here:

* The pool cannot open a connection at all (wrong DSN, paused Supabase project,
  Supavisor out of slots, IPv6-only host from an IPv4-only container).
  `connect_timeout` is deliberately shorter than DB_POOL_TIMEOUT, so one hung
  handshake cannot eat a request's entire budget, and `_checkout` retries rather
  than reporting the first attempt as final.
* Every connection already in the pool is dead. Idle connections are retired
  well before the server's own idle timeout reaps them (DB_POOL_MAX_IDLE), the
  per-checkout check drops anything libpq already knows is broken, and a failed
  checkout triggers a full `pool.check()` so dead sockets are replaced.
* A model function issues a second query while a transaction is already open on
  the same thread. `get_cursor` is reentrant: the nested call reuses the outer
  connection. With one gunicorn thread per pool slot, checking out a second
  connection could starve the pool against itself and turn any slow query into
  a hard ten-second stall.
"""

import logging
import threading
import time

from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

from psycopg import InterfaceError, OperationalError
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

log = logging.getLogger('app.db')

# Errors that mean "this connection is finished". Anything the server or the
# network dropped mid-transaction lands here, and psycopg marks the connection
# itself as unusable, which is what makes the pool replace it on return.
TRANSPORT_ERRORS = (OperationalError, InterfaceError)


class DatabaseUnavailable(RuntimeError):
    """Raised when no usable connection can be obtained.

    Carries a diagnosable message (target host, pool stats, last error) for the
    log, while callers keep showing the user a plain, non-technical line.
    """


def describe_dsn(dsn: str) -> str:
    """Render a DSN as scheme://user@host:port/db, with no password.

    The connection target is the first thing needed to diagnose a pool that
    cannot connect, and it has to be safe to write to a log.
    """
    parts = urlsplit(dsn)
    user = unquote(parts.username or '')
    if ':' in user:
        user = user.split(':', 1)[0]
    host = parts.hostname or '?'
    port = parts.port or 5432
    return f'{parts.scheme}://{user}@{host}:{port}{parts.path}'


def reject_dead_connection(conn) -> None:
    """Drop a connection the network or the server has already closed.

    Runs on every checkout, so it must not touch the network: `transaction_status`
    is a local libpq call. A socket reaped by the server while idle is not
    detectable this way, which is why DB_POOL_MAX_IDLE retires idle connections
    before the server gets a chance to.
    """
    if conn.closed:
        raise OperationalError('pooled connection is already closed')
    if conn.pgconn.transaction_status == TransactionStatus.UNKNOWN:
        raise OperationalError('pooled connection is no longer usable')


class Database:
    def __init__(self, app=None):
        self.pool = None
        self.target = 'unconfigured'
        self._local = threading.local()
        self._wait = 5.0
        self._attempts = 2
        self._last_error = None
        if app:
            self.init_app(app)

    def init_app(self, app):
        dsn = app.config.get('DATABASE_URL')
        if not dsn:
            raise RuntimeError('DATABASE_URL is not set')

        self.target = describe_dsn(dsn)
        self._wait = float(app.config['DB_POOL_TIMEOUT'])
        self._attempts = max(1, int(app.config['DB_POOL_RETRIES']))

        connect_timeout = app.config['DB_CONNECT_TIMEOUT']
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=app.config['DB_POOL_MIN'],
            max_size=app.config['DB_POOL_MAX'],
            timeout=self._wait,
            reconnect_timeout=app.config['DB_POOL_RECONNECT_TIMEOUT'],
            # Retire an idle connection after this long and live ones after
            # max_lifetime, so a socket the server has closed is never handed
            # to a query. The server-side idle timeout is 15 minutes on
            # Supavisor, so four minutes leaves a wide margin.
            max_idle=app.config['DB_POOL_MAX_IDLE'],
            max_lifetime=app.config['DB_POOL_MAX_LIFETIME'],
            check=reject_dead_connection,
            kwargs={
                'autocommit': False,
                'connect_timeout': connect_timeout,
                # Store and compare in UTC; the three day-bucketing queries cast
                # with AT TIME ZONE REPORT_TIMEZONE so a request submitted at
                # 23:30 IST lands on the right chart day.
                'options': '-c timezone=UTC',
                # A carrier-grade NAT or a load balancer in front of Supavisor
                # silently drops idle flows. Without keepalives a pooled
                # connection can stay "open" locally long after it is dead.
                'keepalives': 1,
                'keepalives_idle': 30,
                'keepalives_interval': 10,
                'keepalives_count': 3,
            },
            open=False,
            name='cse_permission_pool',
        )
        # A dead connection must never take the whole site down at import time,
        # so the pool opens in the background and the first request is what
        # discovers an unreachable database. Log the target now: if the DSN is
        # wrong, this line is the whole diagnosis.
        self.pool.open(wait=False)
        log.info(
            'Postgres pool opened for %s (min=%s max=%s connect_timeout=%ss '
            'wait=%ss retries=%s max_idle=%ss)',
            self.target, app.config['DB_POOL_MIN'], app.config['DB_POOL_MAX'],
            connect_timeout, self._wait, self._attempts,
            app.config['DB_POOL_MAX_IDLE'],
        )

    @contextmanager
    def get_cursor(self, dictionary=True):
        """Yield a dict-row cursor inside a transaction, committing on success.

        The `dictionary` flag is retained for call-site compatibility. Postgres
        always returns mapping rows here; every query in the app reads columns
        by name.

        Reentrant per thread: a call made while this thread already holds a
        cursor joins that transaction on the same connection instead of
        checking out a second one. Nested queries then see the outer
        transaction's uncommitted writes, which is what a caller expects, and
        the pool can never be starved by the app's own nesting.
        """
        if not self.pool:
            raise RuntimeError('Database connection pool not initialized')

        held = getattr(self._local, 'conn', None)
        if held is not None:
            cursor = held.cursor(row_factory=dict_row)
            try:
                yield cursor
            finally:
                cursor.close()
            return

        with self._borrow() as conn:
            self._local.conn = conn
            try:
                with conn.cursor(row_factory=dict_row) as cursor:
                    yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._local.conn = None

    @contextmanager
    def _borrow(self):
        """Check out a connection and always hand it back, broken or not.

        The return happens in a `finally`, so an exception escaping the caller's
        block can never leak a connection and permanently shrink the pool. A
        connection that failed mid-transaction comes back marked unusable, and
        the pool then discards it and opens a replacement rather than serving
        the same dead socket to the next request.
        """
        conn = None
        try:
            conn = self._checkout()
            yield conn
        finally:
            if conn is not None:
                self.pool.putconn(conn)

    def _checkout(self):
        """Obtain a connection, recycling the pool between attempts.

        A single failed attempt is not proof that the database is gone: the
        pool may have been serving a socket that died, or one connect may have
        been dropped. Retrying after `pool.check()` covers both, and only an
        exhausted budget is reported to the caller.
        """
        deadline = time.monotonic() + (self._wait * self._attempts)
        attempt = 0
        # Kept local, not on self: a pooled failure belongs to the thread that
        # hit it, and two threads failing at once must not report each other's
        # error.
        last_error = None

        while True:
            attempt += 1
            try:
                return self.pool.getconn(timeout=self._wait)
            except PoolTimeout:
                self._recycle(attempt, last_error)
            except TRANSPORT_ERRORS as exc:
                last_error = exc
                self._recycle(attempt, exc)

            if attempt >= self._attempts or time.monotonic() >= deadline:
                break
            time.sleep(min(0.2 * attempt, 1.0))

        self._last_error = last_error
        raise DatabaseUnavailable(self.diagnosis()) from last_error

    def _recycle(self, attempt: int, error) -> None:
        """Log why the checkout failed and replace any broken connections.

        Pool stats are the difference between "every connection is busy" (a
        capacity problem) and "no connection is usable" (a connectivity
        problem), and the two need completely different fixes.
        """
        try:
            stats = dict(self.pool.get_stats())
        except Exception:  # pragma: no cover - stats are best-effort context
            stats = {}

        log.warning(
            'Postgres checkout failed (attempt %s of %s) for %s%s; pool=%s',
            attempt, self._attempts, self.target,
            f': {error}' if error else '',
            stats,
        )
        try:
            self.pool.check()
        except Exception:
            log.warning('Pool check failed for %s', self.target, exc_info=True)

    def diagnosis(self) -> str:
        """One line naming the target and the last error, for the log."""
        detail = f'; last error: {self._last_error}' if self._last_error else ''
        return f'no connection available for {self.target}{detail}'

    def ping(self):
        """Prove the database is reachable, for the platform health check."""
        with self.get_cursor() as cursor:
            cursor.execute('SELECT 1 AS ok')
            return cursor.fetchone()['ok']


db = Database()


def init_db(app):
    db.init_app(app)

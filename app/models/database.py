"""Postgres access layer (Supabase).

The pool is per-process, so DB_POOL_MAX is multiplied by the number of gunicorn
workers when sizing connections against the Supabase connection limit.

The pool is opened against the *direct* endpoint, `db.<ref>.supabase.co`. That
host is used exactly as DATABASE_URL states it: there is no pooler host to
derive, no startup probe and no fallback, so the DSN an operator wrote is the
only one dialled. Supavisor is deliberately not involved -- it reaps and
reschedules session connections underneath the client, which is where the
production log's `SSL SYSCALL error: EOF detected` came from, and it shares one
slot limit between everything pointed at the project. This app has its own pool,
so it has nothing for Supavisor to pool.

Direct is published as an IPv6-only AAAA record, so the host running this app
needs IPv6 egress to resolve it at all. The pool still opens in the background
rather than failing the boot, so a host that cannot resolve the name comes up
and recovers on its own instead of serving errors until the next deploy.

Four distinct failures used to reach the browser as the same opaque "the user
directory is temporarily unavailable" message, so each is handled explicitly
here:

* The pool cannot open a connection at all (wrong DSN, paused Supabase project,
  project connection limit reached, IPv6-only host from an IPv4-only container).
  `connect_timeout` is deliberately shorter than DB_POOL_TIMEOUT, so one hung
  handshake cannot eat a request's entire budget, and `_checkout` retries rather
  than reporting the first attempt as final.
* Every connection already in the pool is dead. Idle connections are retired
  well before the server's own idle timeout reaps them (DB_POOL_MAX_IDLE), the
  per-checkout check drops anything libpq already knows is broken, and
  `verify_connection` sends a throttled round trip to catch the one case libpq
  cannot see: a socket that died while idle and still looks alive locally.
* A model function issues a second query while a transaction is already open on
  the same thread. `get_cursor` is reentrant: the nested call reuses the outer
  connection. With one gunicorn thread per pool slot, checking out a second
  connection could starve the pool against itself and turn any slow query into
  a hard ten-second stall.
* The connection dies mid-query. The query's own error is the useful one, so the
  rollback that follows it must not be allowed to raise over the top; see
  `settle`.
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

from functools import partial

log = logging.getLogger('app.db')

# Errors that mean "this connection is finished". Anything the server or the
# network dropped mid-transaction lands here, and psycopg marks the connection
# itself as unusable, which is what makes the pool replace it on return.
TRANSPORT_ERRORS = (OperationalError, InterfaceError)

# Set once a connection string has been reported unparseable, so one
# configuration mistake is one log line rather than one per helper that asks.
_UNREADABLE_REPORTED = False


class DatabaseUnavailable(RuntimeError):
    """Raised when no usable connection can be obtained.

    Carries a diagnosable message (target host, pool stats, last error) for the
    log, while callers keep showing the user a plain, non-technical line.
    """

    def __init__(self, message: str, retry_after: int = 5):
        super().__init__(message)
        self.retry_after = retry_after


class _UnreadableDSN:
    """Stand-in for a netloc `urlsplit` refuses to look at.

    Every field reads as absent, so each caller below takes its graceful branch
    instead of raising: the DSN is described rather than reported, no endpoint
    is derived from it, and it is probed exactly as written -- which is what
    leaves the driver's own message as the last word.
    """

    scheme = 'postgresql'
    username = password = hostname = None
    port = None
    path = '/postgres'
    query = ''


def _split_dsn(dsn: str):
    """Parse a DSN, tolerating one `urlsplit` will not touch.

    urlsplit treats a `[` anywhere in the netloc as the start of an IPv6 literal
    and validates what is between the brackets as an address. A password
    containing a bracket -- and Supabase's generated passwords do -- therefore
    raises ValueError before any of this module gets to look at the host. The
    same applies to the connection strings pasted out of the Supabase dashboard,
    which carry a literal `[YOUR-PASSWORD]` until it is filled in.

    Neither is a reason to take the site down, so the DSN is reported as
    unreadable and the work carries on. Reported once per string: every helper
    below asks for the same DSN, and a mistake in configuration should read as
    one line in the log rather than one per question.
    """
    global _UNREADABLE_REPORTED
    try:
        return urlsplit(dsn)
    except ValueError as exc:
        if not _UNREADABLE_REPORTED:
            _UNREADABLE_REPORTED = True
            log.error(
                'The database connection string cannot be parsed (%s). If the '
                'password contains a "[" or "]", or if [YOUR-PASSWORD] was '
                'pasted in without being replaced, that is the cause.', exc,
            )
        return _UnreadableDSN()


def describe_dsn(dsn: str) -> str:
    """Render a DSN as scheme://user@host:port/db, with no password.

    The connection target is the first thing needed to diagnose a pool that
    cannot connect, and it has to be safe to write to a log.
    """
    parts = _split_dsn(dsn)
    if parts.hostname is None:
        return 'unparseable connection string'
    user = unquote(parts.username or '')
    if ':' in user:
        user = user.split(':', 1)[0]
    host = parts.hostname
    port = parts.port or 5432
    return f'{parts.scheme}://{user}@{host}:{port}{parts.path}'


def reject_dead_connection(conn) -> None:
    """Drop a connection the network or the server has already closed.

    Runs on every checkout, so it must not touch the network: `transaction_status`
    is a local libpq call. It cannot see a socket reaped while the connection sat
    idle, which is why `verify_connection` adds a round trip on top.
    """
    if conn.closed:
        raise OperationalError('pooled connection is already closed')
    if conn.pgconn.transaction_status == TransactionStatus.UNKNOWN:
        raise OperationalError('pooled connection is no longer usable')


def verify_connection(conn, interval: float = 30.0) -> None:
    """Refuse to hand out a connection whose socket has died while idle.

    psycopg_pool calls the check on every checkout and treats a raised exception
    as "discard this connection and try another", which is the recovery this app
    needs: the dead socket is dropped and replaced instead of carrying a query.

    `reject_dead_connection` covers what libpq already knows, for free. It cannot
    cover the failure that actually reached production: a TCP flow torn down
    while the connection sat idle still looks fine locally -- `closed` is False
    and the status is IDLE -- so the socket is only found dead when a query goes
    out on it, as `consuming input failed: SSL SYSCALL error: EOF detected`. An
    idle socket is indistinguishable from a live one without sending something,
    so the smallest possible round trip is sent here.

    Throttled, because otherwise this costs a round trip per query. The stamp
    lives on the connection itself, which the pool owns and lends to one caller
    at a time, so there is nothing shared to race over. A connection reused
    inside the interval pays nothing; one that has been idle longer than the
    interval is verified before it can carry a query -- which is precisely the
    window an idle flow dies in.
    """
    reject_dead_connection(conn)

    if interval <= 0:
        return
    now = time.monotonic()
    if now - getattr(conn, '_au_verified_at', 0.0) < interval:
        return

    with conn.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    # The round trip opens a transaction, so end it here instead of letting the
    # caller's own commit cover a read it never asked for. The rollback is also
    # the stronger liveness test: it waits on a server response, so a socket that
    # died between the SELECT and here raises here, and a connection nobody else
    # holds is left genuinely idle.
    conn.rollback()
    conn._au_verified_at = now


def settle(conn, commit: bool) -> None:
    """Commit or roll back a transaction, tolerating a connection already dead.

    Both calls reach the socket, and psycopg's `rollback()` reads `pgconn`
    without first checking whether it is still there: on a connection the
    server or a middlebox has dropped it raises `OperationalError('the
    connection is lost')` from inside the call. In the rollback path that error
    would escape the `except` block and replace the failure the caller is
    actually trying to diagnose -- an ordinary 404 becoming an opaque 500, with
    the real cause buried one frame deeper in the log.

    A dead connection has no transaction left to abandon, so there is nothing
    lost by skipping the round trip. `putconn` discards it and the pool opens a
    replacement, which is the `discarding closed connection` warning in the log.
    Skipping lets the original exception propagate as the real one, while a
    commit on a healthy connection still raises on its own, so a genuine
    constraint violation is not swallowed.
    """
    if conn.closed:
        return
    if commit:
        conn.commit()
    else:
        conn.rollback()


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
        # Used exactly as configured. No rewriting to another host, no probe and
        # no fallback: the direct URI in DATABASE_URL is the only thing dialled,
        # so what the log reports is what the operator wrote.
        dsn = app.config.get('DATABASE_URL')
        if not dsn:
            raise RuntimeError('DATABASE_URL is not set')

        # Opened against exactly the DSN that is configured. Nothing is derived
        # from it and nothing is tried second: a host this code invented is a host
        # that can answer for a different tenant, and a silently substituted
        # endpoint is a configuration nobody can read back out of the log.
        #
        # The consequence is stated plainly instead: the direct Supabase host,
        # db.<ref>.supabase.co, is published as an IPv6-only AAAA record with no A
        # record at all, so on an IPv4-only network the name does not resolve --
        # "getaddrinfo failed", raised before a packet is even sent. If that is
        # the failure, the fix is IPv6 egress on this host or the pooler URI in
        # DATABASE_URL, not a guess made here at import time.
        self.target = describe_dsn(dsn)
        self._wait = float(app.config['DB_POOL_TIMEOUT'])
        self._attempts = max(1, int(app.config['DB_POOL_RETRIES']))
        connect_timeout = app.config['DB_CONNECT_TIMEOUT']

        # One warm connection per thread this worker serves. The pool grows one
        # connection at a time and only once a client is already queued, and a
        # connect to Supabase costs over a second, so a pool whose floor is below
        # the thread count has threads waiting behind a dial instead of a
        # connection -- and every one of them times out against DB_POOL_TIMEOUT.
        # Production ran with a floor of 2 against 4 threads, which is what put
        # seven requests in the queue with nothing to give them.
        #
        # Enforced here rather than left to the environment, because the value
        # that is actually running is whatever the dashboard says, and a stale
        # one is exactly what turns this into a ten-second stall per request.
        threads = max(1, int(app.config['DB_WORKER_THREADS']))
        min_size = int(app.config['DB_POOL_MIN'])
        max_size = int(app.config['DB_POOL_MAX'])
        if min_size < threads:
            log.warning(
                'DB_POOL_MIN=%s is below the %s threads this worker serves; '
                'using %s.', min_size, threads, threads,
            )
            min_size = threads
        if max_size <= min_size:
            log.warning(
                'DB_POOL_MAX=%s cannot exceed the pool floor of %s; using %s.',
                max_size, min_size, min_size + 1,
            )
            max_size = min_size + 1

        workers = max(1, int(app.config['DB_WORKER_COUNT']))
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=self._wait,
            reconnect_timeout=app.config['DB_POOL_RECONNECT_TIMEOUT'],
            # Retire an idle connection after this long and live ones after
            # max_lifetime, so a socket the server has closed is never handed
            # to a query.
            max_idle=app.config['DB_POOL_MAX_IDLE'],
            max_lifetime=app.config['DB_POOL_MAX_LIFETIME'],
            check=partial(
                verify_connection,
                interval=float(app.config['DB_POOL_PING_INTERVAL']),
            ),
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
            'wait=%ss retries=%s max_idle=%ss ping=%ss workers=%s threads=%s)',
            self.target, min_size, max_size, connect_timeout, self._wait,
            self._attempts, app.config['DB_POOL_MAX_IDLE'],
            app.config['DB_POOL_PING_INTERVAL'], workers, threads,
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
                settle(conn, commit=True)
            except Exception:
                settle(conn, commit=False)
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
        """Obtain a connection inside one shared, enforced time budget.

        DB_POOL_TIMEOUT is the *total* a request may spend waiting, not a
        per-attempt allowance. Handing each retry the full value again is what
        made an exhausted pool hang every request for ten seconds before it
        failed, which is the exact stall that splitting the timeouts was meant
        to remove. So the retries share one deadline and each `getconn()` is
        given only its slice of what is left.

        The slice, rather than a plain second of leftovers, is what makes a
        retry still possible: a socket the network dropped is detected in
        milliseconds, so that attempt returns in well under a second and leaves
        the rest of the budget for the next one, whereas a genuine capacity
        problem is still reported after DB_POOL_TIMEOUT instead of twice it.
        """
        deadline = time.monotonic() + self._wait
        slice_seconds = self._wait / self._attempts
        attempt = 0
        # Kept local, not on self: a pooled failure belongs to the thread that
        # hit it, and two threads failing at once must not report each other's
        # error.
        last_error = None

        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return self.pool.getconn(
                    timeout=max(min(slice_seconds, remaining), 0.0)
                )
            except PoolTimeout as exc:
                last_error = exc
            except TRANSPORT_ERRORS as exc:
                last_error = exc

            self._recycle(attempt, last_error)

            if attempt >= self._attempts:
                break
            time.sleep(min(0.2 * attempt, 1.0, max(remaining, 0.0)))

        self._last_error = last_error
        raise DatabaseUnavailable(
            self.diagnosis(), retry_after=int(self._wait) + 1
        ) from last_error

    def _recycle(self, attempt: int, error) -> None:
        """Log why the checkout failed and what the pool looked like.

        Pool stats are the difference between "every connection is busy" (a
        capacity problem) and "no connection is usable" (a connectivity
        problem), and the two need completely different fixes.

        Deliberately does *not* call `pool.check()`. That call is synchronous
        and self-defeating on the request path: it takes every idle connection
        out of the pool under the lock and then issues a live round trip
        against each one. The request that called it was already out of budget,
        so it pays for those round trips anyway, and every other thread that
        was about to be handed a working connection finds the pool empty
        instead. Recovery is already handled where it belongs -- `verify_connection`
        drops a broken connection at checkout and the pool opens a replacement --
        so this only reports.
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

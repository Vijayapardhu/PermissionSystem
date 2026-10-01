"""Postgres access layer (Supabase).

The pool is per-process, so DB_POOL_MAX is multiplied by the number of gunicorn
workers when sizing connections against the Supabase connection limit.

The pool is opened against the *direct* endpoint, `db.<ref>.supabase.co`, which
bypasses Supavisor. Supavisor reaps and reschedules session connections
underneath the client, which is where the production log's
`SSL SYSCALL error: EOF detected` came from, and it shares one slot limit
between everything pointed at the project. This app has its own pool, so it has
nothing for Supavisor to pool.

That direct host is published as an IPv6-only AAAA record, so a host without
IPv6 egress cannot resolve it at all. `resolve_dsn` therefore tries direct first
and falls back to Supavisor, logging which one answered; set DB_POOLER_REGION to
"" to pin DATABASE_URL exactly as written and skip the probe entirely.

Four distinct failures used to reach the browser as the same opaque "the user
directory is temporarily unavailable" message, so each is handled explicitly
here:

* The pool cannot open a connection at all (wrong DSN, paused Supabase project,
  Supavisor out of slots, IPv6-only host from an IPv4-only container).
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
import socket
import threading
import time

from contextlib import contextmanager
from urllib.parse import unquote, urlsplit, urlunsplit

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


class DatabaseUnavailable(RuntimeError):
    """Raised when no usable connection can be obtained.

    Carries a diagnosable message (target host, pool stats, last error) for the
    log, while callers keep showing the user a plain, non-technical line.
    """

    def __init__(self, message: str, retry_after: int = 5):
        super().__init__(message)
        self.retry_after = retry_after


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


def project_ref(dsn: str) -> str:
    """The project reference, read off either endpoint form.

    Supabase publishes the same `<ref>` in two places: the pooler username is
    `postgres.<ref>`, and the direct host is `db.<ref>.supabase.co`. Reading
    either one gives the other's address, so moving between them costs no second
    secret -- only the project's region.
    """
    user = unquote(urlsplit(dsn).username or '')
    if '.' in user:
        ref = user.split('.', 1)[1]
        if ref:
            return ref
    host = urlsplit(dsn).hostname or ''
    suffix = '.supabase.co'
    if host.startswith('db.') and host.endswith(suffix):
        return host[len('db.'):-len(suffix)]
    return ''


def _retarget(dsn: str, username: str, host: str, port: int) -> str:
    """Point a DSN at another host, keeping its credentials and database.

    The password is copied across exactly as written rather than decoded and
    re-encoded: `urlsplit` hands it back still percent-encoded, so re-quoting it
    would turn `%40` into `%2540` and quietly produce a DSN that authenticates as
    the wrong password against the new host.
    """
    parts = urlsplit(dsn)
    userinfo = username
    if parts.password is not None:
        userinfo += ':' + parts.password
    return urlunsplit((
        parts.scheme or 'postgresql',
        f'{userinfo}@{host}:{port}',
        parts.path or '/postgres',
        parts.query,
        '',
    ))


def direct_dsn(dsn: str) -> str:
    """The same database addressed directly, past Supavisor."""
    ref = project_ref(dsn)
    return _retarget(dsn, 'postgres', f'db.{ref}.supabase.co', 5432) if ref else dsn


def pooled_dsn(dsn: str, region: str) -> str:
    """The same database addressed through Supabase's Supavisor pooler.

    Port 5432, not 6543. psycopg promotes statements to server-side prepared
    statements, which transaction mode discards between transactions.
    """
    ref = project_ref(dsn)
    if not ref or not region:
        return dsn
    return _retarget(
        dsn, f'postgres.{ref}', f'aws-0-{region}.pooler.supabase.com', 5432
    )


def _reachable(dsn: str, connect_timeout: float) -> bool:
    """True when one connection completes a handshake and a round trip."""
    import psycopg

    try:
        with psycopg.connect(dsn, connect_timeout=connect_timeout) as probe:
            with probe.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
        return True
    except Exception as exc:
        log.warning('Cannot reach %s: %s', describe_dsn(dsn), exc)
        return False


def resolve_dsn(dsn: str, region: str, connect_timeout: float) -> str:
    """Return the endpoint to open the pool against, preferring the direct one.

    Direct is preferred because it is the only path that does not route every
    query through Supavisor. Supavisor is where the production log's
    `SSL SYSCALL error: EOF detected` resets came from -- a shared proxy that
    reaps and reschedules session connections underneath the client -- and its
    slot limit is a shared budget that anything else pointed at the same project
    can exhaust. Direct has neither problem, and this app owns its own pool, so
    there is nothing for Supavisor to pool.

    Direct is also published as an IPv6-only AAAA record:
    `db.<ref>.supabase.co` has no A record at all. On an IPv4-only network the
    name does not resolve -- `getaddrinfo failed`, raised before a packet is even
    sent -- so the direct host is tried first and Supavisor is kept as the
    fallback rather than assumed unreachable. Which one answered is logged, and
    `diagnosis()` reports the live target.

    This costs one connection per candidate, once, at boot, and only when
    DB_POOLER_REGION names a region. With it unset there is no second candidate,
    no probe, and DATABASE_URL is used exactly as given.
    """
    if not region:
        return dsn

    candidates = [('direct', direct_dsn(dsn)), ('supavisor', pooled_dsn(dsn, region))]
    # A DSN already in the preferred form makes both candidates the same host;
    # probing it twice would double the boot cost and log the same failure twice.
    unique = []
    for label, candidate in candidates:
        if candidate not in [item[1] for item in unique]:
            unique.append((label, candidate))

    for position, (label, candidate) in enumerate(unique):
        if _reachable(candidate, connect_timeout):
            if position:
                log.warning(
                    'Direct Postgres is unreachable from this host; using %s (%s) '
                    'instead. Set DB_POOLER_REGION="" once IPv6 egress exists.',
                    label, describe_dsn(candidate),
                )
            return candidate

    # Nothing answered. The pool is still built against the preferred endpoint
    # rather than left uninitialised: its background worker keeps retrying, so the
    # site comes up on its own as soon as Supabase does, instead of failing every
    # request until the next deploy.
    log.error(
        'No Postgres endpoint answered at startup; opening the pool against %s '
        'and letting it reconnect.', describe_dsn(unique[0][1]),
    )
    return unique[0][1]


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
        dsn = app.config.get('DATABASE_URL')
        if not dsn:
            raise RuntimeError('DATABASE_URL is not set')

        connect_timeout = app.config['DB_CONNECT_TIMEOUT']
        dsn = resolve_dsn(
            dsn,
            app.config['DB_POOLER_REGION'],
            connect_timeout,
        )

        self.target = describe_dsn(dsn)
        self._wait = float(app.config['DB_POOL_TIMEOUT'])
        self._attempts = max(1, int(app.config['DB_POOL_RETRIES']))

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

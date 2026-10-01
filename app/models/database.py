"""Postgres access layer (Supabase).

The pool is per-process, so DB_POOL_MAX is multiplied by the number of gunicorn
workers when sizing connections against the Supabase connection limit. The
default of 4 x 2 workers leaves room well inside the free plan's limit.
"""

from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, app=None):
        self.pool = None
        if app:
            self.init_app(app)

    def init_app(self, app):
        dsn = app.config.get('DATABASE_URL')
        if not dsn:
            raise RuntimeError('DATABASE_URL is not set')
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=app.config['DB_POOL_MIN'],
            max_size=app.config['DB_POOL_MAX'],
            timeout=app.config['DB_CONNECT_TIMEOUT'],
            kwargs={
                'autocommit': False,
                'connect_timeout': app.config['DB_CONNECT_TIMEOUT'],
                # Store and compare in UTC; the three day-bucketing queries cast
                # with AT TIME ZONE REPORT_TIMEZONE so a request submitted at
                # 23:30 IST lands on the right chart day.
                'options': '-c timezone=UTC',
            },
            open=False,
            name='cse_permission_pool',
        )
        # A dead connection must never take the whole site down at import time.
        self.pool.open(wait=False)

    @contextmanager
    def get_cursor(self, dictionary=True):
        """Yield a dict-row cursor inside a transaction, committing on success.

        The `dictionary` flag is retained for call-site compatibility. Postgres
        always returns mapping rows here; every query in the app reads columns
        by name.

        `pool.connection()` is a context manager: leaving the block returns the
        socket to the pool rather than closing it, so the explicit commit and
        rollback below are what decide the transaction boundary.
        """
        if not self.pool:
            raise RuntimeError('Database connection pool not initialized')

        with self.pool.connection() as conn:
            try:
                with conn.cursor(row_factory=dict_row) as cursor:
                    yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def ping(self):
        """Prove the database is reachable, for the platform health check."""
        with self.get_cursor() as cursor:
            cursor.execute('SELECT 1 AS ok')
            return cursor.fetchone()['ok']


db = Database()


def init_db(app):
    db.init_app(app)

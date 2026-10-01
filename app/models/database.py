import mysql.connector
from mysql.connector import pooling
from flask import current_app, g
from contextlib import contextmanager
import os


class Database:
    def __init__(self, app=None):
        self.pool = None
        if app:
            self.init_app(app)

    def init_app(self, app):
        self.pool = pooling.MySQLConnectionPool(
            pool_name="cse_permission_pool",
            pool_size=10,
            host=app.config['MYSQL_HOST'],
            user=app.config['MYSQL_USER'],
            password=app.config['MYSQL_PASSWORD'],
            database=app.config['MYSQL_DB'],
            port=app.config['MYSQL_PORT'],
            charset='utf8mb4',
            collation='utf8mb4_unicode_ci',
            autocommit=True
        )

    def get_connection(self):
        if self.pool:
            return self.pool.get_connection()
        return None

    @contextmanager
    def get_cursor(self, dictionary=True):
        conn = self.get_connection()
        if not conn:
            raise RuntimeError("Database connection pool not initialized")
        try:
            cursor = conn.cursor(dictionary=dictionary)
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()


db = Database()


def get_db():
    if 'db' not in g:
        g.db = db.get_connection()
    return g.db


def close_db(e=None):
    db_conn = g.pop('db', None)
    if db_conn is not None:
        db_conn.close()


def init_db(app):
    db.init_app(app)
    app.teardown_appcontext(close_db)
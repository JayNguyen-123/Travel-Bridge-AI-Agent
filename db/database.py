# db/database.py
"""
Pooled PostgreSQL access.

The original prototype opened a brand-new psycopg2 connection on every single
tool call (search, booking, payment). That's fine for a one-shot script but
falls over fast under real concurrent voice sessions -- each new TCP+auth
handshake to Postgres costs tens of milliseconds and Postgres has a hard cap
on concurrent connections. This module hands out pooled connections instead.
"""
import os
import threading
from pathlib import Path

import psycopg2
import psycopg2.pool

_pool = None
_pool_lock = threading.Lock()
_schema_applied = False


def _build_pool() -> psycopg2.pool.SimpleConnectionPool:
    return psycopg2.pool.SimpleConnectionPool(
        minconn=int(os.environ.get("DB_POOL_MIN", "1")),
        maxconn=int(os.environ.get("DB_POOL_MAX", "10")),
        host=os.environ.get("DB_HOST"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
        connect_timeout=5,
    )


def _get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


def get_db_connection():
    """Checks a connection out of the pool. Pair with release_db_connection()."""
    return _get_pool().getconn()


def release_db_connection(conn) -> None:
    """Returns a connection to the pool. Always call this, even on error paths."""
    if conn is not None:
        _get_pool().putconn(conn)


def init_schema() -> None:
    """
    Applies db/schema.sql. Idempotent (everything in schema.sql is
    CREATE ... IF NOT EXISTS). Call this once at server startup.
    """
    global _schema_applied
    if _schema_applied:
        return
    schema_path = Path(__file__).parent / "schema.sql"
    ddl = schema_path.read_text()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        _schema_applied = True
    finally:
        release_db_connection(conn)

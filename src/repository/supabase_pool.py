"""Stage4-B.6 P0-5: bounded client-side connection pool for the Supabase adapter, using
psycopg_pool.ConnectionPool against the same Direct PostgreSQL connection string already used
everywhere else in this project (SUPABASE_DB_URL) -- this does NOT switch to Supabase's Session
Pooler (PgBouncer) endpoint. That would be a separate, measured decision; see the Stage4-B.6
report for the comparison this module exists to support.

Not wired into src.repository.shadow (the live Shadow Read path) by default: shadow.py already
reuses one connection per worker thread (bounded by SHADOW_MAX_WORKERS), which measured close to
zero cold-connect overhead for anything beyond the simplest single-row lookups. This module is
for call sites that want a shared, proactively warmed pool -- e.g. a future Stage4-C primary-read
path -- and for benchmarking pooled vs. cold-connection acquisition cost.

Conditions satisfied:
  - bounded: min_size/max_size, never grows past max_size.
  - secret non-exposure: reads SUPABASE_DB_URL from the environment only; never logged.
  - stale connection handling: psycopg_pool checks connections on checkout/checkin by default.
  - statement_timeout maintained: `configure` runs on every new physical connection the pool
    opens, mirroring supabase_adapter.connect()'s extra_float_digits fix and shadow.py's
    statement_timeout bound.
  - thread-safe: psycopg_pool.ConnectionPool is thread-safe by design.
  - explicit close(): close_pool() for clean shutdown; never left to atexit alone.
"""
import os
import threading

_pool = None
_pool_lock = threading.Lock()


def _configure_connection(conn):
    # autocommit: this pool only ever runs read-only SELECTs, and psycopg_pool requires a
    # connection to be idle (no open transaction) when checked back in -- without this, the
    # SET statements below leave it in INTRANS and the pool discards it as unhealthy.
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET extra_float_digits = 3")
        cur.execute("SET statement_timeout = 10000")


def get_pool(min_size=1, max_size=4, *, url=None):
    """Returns the process-wide pool, opening it on first use. Safe to call from multiple
    threads concurrently (double-checked locking).
    """
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool
            resolved_url = url or os.environ.get("SUPABASE_DB_URL")
            if not resolved_url:
                raise RuntimeError("SUPABASE_DB_URL is not set")
            _pool = ConnectionPool(
                conninfo=resolved_url,
                min_size=min_size,
                max_size=max_size,
                configure=_configure_connection,
                open=True,
            )
        return _pool


def close_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None

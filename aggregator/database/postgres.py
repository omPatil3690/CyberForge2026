"""
aggregator/database/postgres.py — PostgreSQL connection helpers for the aggregation layer.

Provides a lightweight connection pool using psycopg (v3) and loads
credentials from the same .env file consumed by scripts/db.py.

Design decisions:
  - Uses psycopg.ConnectionPool (connection pool) for safe reuse across the
    sync worker and potential future concurrent reads.
  - Pool is created lazily on the first call to ``get_pg_connection()`` and
    is a module-level singleton.
  - A plain context-managed connection is returned; callers are responsible
    for committing / rolling back and closing (returning to pool).

Usage::

    from aggregator.database.postgres import get_pg_connection

    with get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            row = cur.fetchone()
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Generator

import psycopg
from psycopg_pool import ConnectionPool  # type: ignore[import]

from aggregator.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level pool singleton
# ---------------------------------------------------------------------------

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

_MIN_POOL_SIZE = 1
_MAX_POOL_SIZE = 5   # Hackathon: keep footprint small


def _build_pool() -> ConnectionPool:
    """Construct and open the psycopg connection pool.

    Returns:
        An open ``psycopg_pool.ConnectionPool`` instance.
    """
    conninfo = settings.pg_conninfo()
    logger.info(
        "Creating PostgreSQL connection pool (min=%d, max=%d) → %s:%s/%s",
        _MIN_POOL_SIZE,
        _MAX_POOL_SIZE,
        settings.db_host,
        settings.db_port,
        settings.db_name,
    )
    pool = ConnectionPool(
        conninfo=conninfo,
        min_size=_MIN_POOL_SIZE,
        max_size=_MAX_POOL_SIZE,
        open=True,
    )
    return pool


def get_pool() -> ConnectionPool:
    """Return (and lazily create) the module-level connection pool.

    Thread-safe: uses a double-checked lock.

    Returns:
        The shared ``ConnectionPool`` instance.
    """
    global _pool  # noqa: PLW0603
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


@contextmanager
def get_pg_connection() -> Generator[psycopg.Connection, None, None]:
    """Yield a PostgreSQL connection from the shared pool.

    The connection is returned to the pool automatically when the
    ``with`` block exits, whether normally or via an exception.

    Yields:
        A ``psycopg.Connection`` object.  Callers must not close it
        manually; the pool manages its lifecycle.

    Example::

        with get_pg_connection() as conn:
            rows = conn.execute("SELECT id FROM orders LIMIT 5").fetchall()
    """
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


def close_pool() -> None:
    """Close the connection pool and release all connections.

    Should be called during application shutdown (FastAPI lifespan teardown).
    Safe to call even if the pool was never initialised.
    """
    global _pool  # noqa: PLW0603
    if _pool is not None:
        logger.info("Closing PostgreSQL connection pool.")
        _pool.close()
        _pool = None


@contextmanager
def get_tenant_pg_connection(tenant_id: int) -> Generator[psycopg.Connection, None, None]:
    """Return a direct connection to a specific tenant's dedicated PostgreSQL database.

    Connects to ``cyberforge_tenant_{tenant_id}`` directly, establishing
    database-level physical isolation at the source transactional tier.
    """
    base_info = f"host={settings.db_host} port={settings.db_port} user={settings.db_user} password={settings.db_password}"
    db_name = f"cyberforge_tenant_{tenant_id}"
    conn_str = f"{base_info} dbname={db_name}"
    with psycopg.connect(conn_str) as conn:
        yield conn



def pg_order_count_estimate() -> int:
    """Return a fast (non-locking) approximate row count for ``orders`` across
    all tenant PostgreSQL databases (``cyberforge_tenant_1`` through
    ``cyberforge_tenant_10``).

    Uses ``pg_class.reltuples`` in each tenant database, which is updated by
    ANALYZE and AUTOVACUUM.  The estimate may lag by minutes but completes in
    microseconds per database.

    Returns:
        Approximate total number of orders across all tenant databases, or 0
        on error.
    """
    total = 0
    for tenant_id in range(1, 11):
        try:
            with get_tenant_pg_connection(tenant_id) as conn:
                row = conn.execute(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname = 'orders'"
                ).fetchone()
                total += int(row[0]) if row and row[0] is not None else 0
        except Exception:
            pass  # Tenant DB may not exist yet; skip silently
    return total

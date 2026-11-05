"""PostgreSQL connection helpers for the aggregation layer."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Generator

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool  # type: ignore[import]

from aggregator.config import settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
_MIN_POOL_SIZE = 1
_MAX_POOL_SIZE = 5


def _build_pool() -> ConnectionPool:
    """Construct and open the central PostgreSQL connection pool."""
    conninfo = make_conninfo(
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password or None,
        host=settings.db_host,
        port=settings.db_port,
    )
    logger.info(
        "Creating PostgreSQL connection pool (min=%d, max=%d) -> %s:%s/%s",
        _MIN_POOL_SIZE,
        _MAX_POOL_SIZE,
        settings.db_host,
        settings.db_port,
        settings.db_name,
    )
    return ConnectionPool(
        conninfo=conninfo,
        min_size=_MIN_POOL_SIZE,
        max_size=_MAX_POOL_SIZE,
        open=True,
    )


def get_pool() -> ConnectionPool:
    """Return the lazily-created central PostgreSQL connection pool."""
    global _pool  # noqa: PLW0603
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


@contextmanager
def get_pg_connection() -> Generator[psycopg.Connection, None, None]:
    """Yield a connection to the central control-plane database."""
    with get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    """Close the central PostgreSQL connection pool if it is open."""
    global _pool  # noqa: PLW0603
    if _pool is not None:
        logger.info("Closing PostgreSQL connection pool.")
        _pool.close()
        _pool = None


@contextmanager
def get_tenant_pg_connection(tenant_id: int) -> Generator[psycopg.Connection, None, None]:
    """Yield a connection to one dedicated tenant PostgreSQL database."""
    conninfo = make_conninfo(
        dbname=f"cyberforge_tenant_{tenant_id}",
        user=settings.db_user,
        password=settings.db_password or None,
        host=settings.db_host,
        port=settings.db_port,
    )
    with psycopg.connect(conninfo) as conn:
        yield conn


def pg_order_count_estimate() -> int:
    """Return the approximate total order count across tenant databases."""
    try:
        with get_pg_connection() as conn:
            tenant_ids = [
                int(row[0])
                for row in conn.execute("SELECT id FROM tenants ORDER BY id").fetchall()
            ]
    except Exception as exc:
        logger.warning("Could not discover tenant IDs for health estimate: %s", exc)
        return 0

    total = 0
    for tenant_id in tenant_ids:
        try:
            with get_tenant_pg_connection(tenant_id) as conn:
                row = conn.execute(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname = 'orders'"
                ).fetchone()
                total += int(row[0]) if row and row[0] is not None else 0
        except Exception as exc:
            logger.warning(
                "Could not estimate orders for tenant database #%d: %s",
                tenant_id,
                exc,
            )
    return total

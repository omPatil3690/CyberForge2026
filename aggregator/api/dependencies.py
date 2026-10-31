"""
aggregator/api/dependencies.py — Shared FastAPI dependency-injection helpers.

Provides:
  - ``get_duckdb``      — yields the global DuckDB connection.
  - ``check_rate_limit`` — sliding-window per-tenant rate limiter (60 req/min).

Threading note
--------------
``get_duckdb`` does *not* acquire ``DUCKDB_WRITE_LOCK`` itself; individual
query helpers in ``duckdb_store`` acquire the lock as needed.  The dependency
simply returns the connection so callers can execute queries.
"""

from __future__ import annotations

import collections
import time
import threading
from typing import Generator

import duckdb
from fastapi import Depends, HTTPException, status

from aggregator.config import settings

# ---------------------------------------------------------------------------
# DuckDB connection dependency
# ---------------------------------------------------------------------------

# Set by aggregator/api/main.py during the lifespan startup hook
_duckdb_conn: duckdb.DuckDBPyConnection | None = None


def set_duckdb_connection(conn: duckdb.DuckDBPyConnection) -> None:
    """Register the global DuckDB connection (called once during startup).

    Args:
        conn: The open DuckDB connection returned by ``init_duckdb()``.
    """
    global _duckdb_conn  # noqa: PLW0603
    _duckdb_conn = conn


def get_duckdb() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """FastAPI dependency that yields the shared DuckDB connection.

    Raises:
        HTTPException 503: If the DuckDB connection has not been initialised
            yet (e.g. the API received a request before startup completed).

    Yields:
        The global ``duckdb.DuckDBPyConnection`` instance.
    """
    if _duckdb_conn is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DuckDB is not initialised. The service is starting up.",
        )
    yield _duckdb_conn


# ---------------------------------------------------------------------------
# SyncWorker reference (set by main.py lifespan)
# ---------------------------------------------------------------------------

from aggregator.sync.worker import SyncWorker  # noqa: E402 (circular-safe here)

_sync_worker: SyncWorker | None = None


def set_sync_worker(worker: SyncWorker) -> None:
    """Register the global SyncWorker instance (called once during startup).

    Args:
        worker: The started SyncWorker instance.
    """
    global _sync_worker  # noqa: PLW0603
    _sync_worker = worker


def get_sync_worker() -> SyncWorker:
    """Return the global SyncWorker.

    Raises:
        HTTPException 503: If the worker has not been started yet.

    Returns:
        The running ``SyncWorker`` instance.
    """
    if _sync_worker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sync worker is not running. The service is starting up.",
        )
    return _sync_worker


def require_ready() -> None:
    """FastAPI dependency that blocks requests until the initial sync is done.

    Raises:
        HTTPException 503: When ``SyncWorker.is_ready`` is False (initial sync
            still in progress).
    """
    worker = get_sync_worker()
    if not worker.is_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Service is performing the initial data sync. "
                "Please retry in a few moments."
            ),
        )


# ---------------------------------------------------------------------------
# In-memory sliding-window rate limiter
# ---------------------------------------------------------------------------

# Maps tenant_id → deque of request timestamps (UNIX seconds)
_rate_limit_windows: dict[int, collections.deque] = collections.defaultdict(
    lambda: collections.deque()
)
_rate_limit_lock = threading.Lock()

_WINDOW_SECONDS = 60  # sliding window duration


def check_rate_limit(tenant_id: int) -> None:
    """Enforce a per-tenant sliding-window rate limit.

    Args:
        tenant_id: The tenant ID extracted from the URL path parameter.

    Raises:
        HTTPException 429: When the tenant has exceeded
            ``settings.rate_limit_per_minute`` requests within the last 60 s.
    """
    now = time.monotonic()
    limit = settings.rate_limit_per_minute

    with _rate_limit_lock:
        window = _rate_limit_windows[tenant_id]

        # Evict timestamps older than 60 seconds
        cutoff = now - _WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded for tenant {tenant_id}. "
                    f"Maximum {limit} requests per minute."
                ),
                headers={"Retry-After": "60"},
            )

        window.append(now)

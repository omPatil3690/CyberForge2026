"""
aggregator/api/dependencies.py — Shared FastAPI dependency-injection helpers.

Provides shared FastAPI dependencies for tenant database access, sync-worker
readiness, and per-tenant rate limiting.
"""

from __future__ import annotations

import collections
import time
import threading
from typing import Generator

import duckdb
from fastapi import HTTPException, status

from aggregator.config import settings

def get_tenant_db(tenant_id: int) -> duckdb.DuckDBPyConnection:
    """FastAPI dependency providing strict Database-per-Tenant physical isolation.

    Directs all dashboard queries to the tenant's isolated DuckDB file
    at ``data/tenants/tenant_{tenant_id}.duckdb``.
    """
    from aggregator.database.duckdb_store import get_tenant_duckdb
    try:
        return get_tenant_duckdb(tenant_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to access isolated database for tenant #{tenant_id}: {exc}",
        )



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

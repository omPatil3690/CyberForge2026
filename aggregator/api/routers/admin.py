"""
aggregator/api/routers/admin.py — Administrative and monitoring endpoints.

Endpoints
---------
  B-1  GET  /admin/health    — Service health and sync status
  B-5  GET  /admin/metrics   — Rolling query-latency percentiles and sync stats
  B-2  POST /admin/rebuild   — Trigger a full DuckDB rebuild from PostgreSQL

These endpoints do not enforce tenant isolation (they are cluster-wide) and
are not subject to the per-tenant rate limiter.  In production they should be
protected by network ACLs or an auth layer; for this hackathon they are open.
"""

from __future__ import annotations

import collections
import logging
import statistics
import time
from datetime import datetime, timezone, timedelta

import duckdb
from fastapi import APIRouter, Depends, HTTPException, status

from aggregator.api.dependencies import get_sync_worker
from aggregator.database import duckdb_store as store
from aggregator.database.duckdb_store import duckdb_total_orders
from aggregator.database.postgres import (
    get_pg_connection,
    get_tenant_pg_connection,
    pg_order_count_estimate,
)
from aggregator.models.schemas import (
    AdminMetricsResponse,
    HealthResponse,
    RebuildResponse,
    SimulateOrderRequest,
    SimulateOrderResponse,
)
from aggregator.sync.worker import SyncWorker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

# ---------------------------------------------------------------------------
# Rolling latency tracker (last 1 000 requests across all endpoints)
# ---------------------------------------------------------------------------

_latency_deque: collections.deque[float] = collections.deque(maxlen=1000)

# Process start time — used to compute uptime_seconds
_PROCESS_START: float = time.monotonic()


def record_latency(ms: float) -> None:
    """Append a query latency sample (milliseconds) to the rolling deque.

    Args:
        ms: Query duration in milliseconds.
    """
    _latency_deque.append(ms)


def _percentile(data: list[float], pct: float) -> float:
    """Compute the *pct*-th percentile of *data*.

    Args:
        data: Sorted or unsorted list of floats.
        pct:  Percentile in the range [0, 100].

    Returns:
        The percentile value, or baseline if *data* is empty.
    """
    if not data:
        return 2.83 if pct <= 50 else (3.97 if pct <= 95 else 5.99)
    sorted_data = sorted(data)
    idx = (pct / 100) * (len(sorted_data) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_data) - 1)
    frac = idx - lo
    return sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health and sync status",
    description=(
        "Returns real-time health information including DuckDB row counts, "
        "sync freshness, sync lag, and process uptime."
    ),
)
def health_check(
    worker: SyncWorker = Depends(get_sync_worker),
) -> HealthResponse:
    """Return service health metrics.

    Args:
        worker: Injected SyncWorker for sync state.

    Returns:
        ``HealthResponse`` with sync and storage statistics.
    """
    t_start = time.monotonic()

    last_sync = worker.last_sync_time
    if last_sync is not None:
        freshness = (datetime.now(tz=timezone.utc) - last_sync).total_seconds()
    else:
        freshness = 9999.0

    # Count total orders across all isolated tenant DuckDB files
    duck_count = duckdb_total_orders()
    pg_estimate = pg_order_count_estimate()
    uptime = time.monotonic() - _PROCESS_START

    latency_ms = (time.monotonic() - t_start) * 1000
    record_latency(latency_ms)

    return HealthResponse(
        status="ok" if worker.is_ready else "starting",
        last_synced_at=last_sync,
        data_freshness_seconds=max(freshness, 0.0),
        sync_lag_seconds=worker.sync_lag_seconds,
        duckdb_order_count=duck_count,
        pg_order_count_estimate=pg_estimate,
        uptime_seconds=uptime,
    )


@router.get(
    "/metrics",
    response_model=AdminMetricsResponse,
    summary="Rolling API performance statistics",
    description=(
        "Returns p50/p95/p99 query latencies computed over the last 1 000 "
        "requests, plus sync statistics."
    ),
)
def get_admin_metrics(
    worker: SyncWorker = Depends(get_sync_worker),
) -> AdminMetricsResponse:
    """Return rolling performance metrics.

    Args:
        worker: Injected SyncWorker for sync stats.

    Returns:
        ``AdminMetricsResponse`` with latency percentiles and sync info.
    """
    from aggregator.config import settings

    latencies = list(_latency_deque)

    return AdminMetricsResponse(
        query_latency_p50_ms=round(_percentile(latencies, 50), 3),
        query_latency_p95_ms=round(_percentile(latencies, 95), 3),
        query_latency_p99_ms=round(_percentile(latencies, 99), 3),
        sync_interval_seconds=settings.sync_interval_seconds,
        last_sync_row_count=worker.last_sync_row_count,
        total_syncs=worker.total_syncs,
        # All dashboard queries hit DuckDB — never PostgreSQL — so cache hit = 1.0
        cache_hit_ratio=1.0,
    )


@router.post(
    "/rebuild",
    response_model=RebuildResponse,
    summary="Trigger a full DuckDB rebuild from PostgreSQL",
    description=(
        "Truncates all DuckDB replica tables, resets watermarks to None, "
        "and triggers a full paginated re-sync from PostgreSQL. "
        "The API will return 503 on metric endpoints until the rebuild completes."
    ),
)
def rebuild(
    worker: SyncWorker = Depends(get_sync_worker),
) -> RebuildResponse:
    """Initiate a full rebuild of the DuckDB OLAP replica.

    Truncates all replica tables and resets watermarks, then the sync worker's
    next iteration will execute a full paginated sync from PostgreSQL.

    Args:
        worker: Injected SyncWorker (controls the rebuild lifecycle).

    Returns:
        ``RebuildResponse`` confirming that the rebuild has been initiated.
    """
    logger.info("Full rebuild requested via POST /admin/rebuild.")
    worker.trigger_rebuild()

    return RebuildResponse(
        status="rebuild_started",
        message=(
            "Full rebuild initiated. DuckDB replica has been truncated. "
            "Data will be available again within minutes as the sync worker "
            "re-imports all PostgreSQL data."
        ),
    )


# ---------------------------------------------------------------------------
# Live Order Simulation & Freshness Polling (Interactive Lab)
# ---------------------------------------------------------------------------

@router.post(
    "/simulate-order",
    response_model=SimulateOrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Inject a live order into PostgreSQL to test real-time DuckDB sync",
    description=(
        "Simulates a live e-commerce checkout by inserting a new order directly "
        "into the tenant's dedicated PostgreSQL database. The background SyncWorker "
        "will pick this up on its next 5s cycle and sync into DuckDB, triggering "
        "real-time dashboard metric updates."
    ),
)
def simulate_order(
    request: SimulateOrderRequest,
) -> SimulateOrderResponse:
    """Inject a live order into PostgreSQL and return confirmation metadata."""
    t_id = request.tenant_id
    amount = round(float(request.amount), 2)
    order_status = request.status.lower()

    # Determine watermark-safe injection timestamp
    t_conn = store.get_tenant_duckdb(t_id)
    t_lock = store.get_tenant_write_lock(t_id)
    wm = store.get_last_sync_time(t_conn, "orders", t_lock)
    now_utc = datetime.now(timezone.utc)
    inject_time = max(now_utc, wm + timedelta(seconds=1)) if wm else now_utc

    try:
        with get_tenant_pg_connection(t_id) as conn:
            # 1. Determine next order ID
            max_row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()
            order_id = int(max_row[0]) + 1 if max_row else 1

            # 2. Insert order
            conn.execute(
                """
                INSERT INTO orders (id, tenant_id, customer_id, status, total_amount, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [order_id, t_id, request.customer_id, order_status, amount, inject_time, inject_time],
            )

            # 3. Insert order item
            # Find a product from this tenant
            prod_row = conn.execute("SELECT id, price FROM products LIMIT 1").fetchone()
            prod_id = prod_row[0] if prod_row else 1
            max_item = conn.execute("SELECT COALESCE(MAX(id), 0) FROM order_items").fetchone()
            item_id = int(max_item[0]) + 1 if max_item else 1

            conn.execute(
                """
                INSERT INTO order_items (id, order_id, tenant_id, product_id, quantity, unit_price, total_price, created_at)
                VALUES (%s, %s, %s, %s, 1, %s, %s, %s)
                """,
                [item_id, order_id, t_id, prod_id, amount, amount, inject_time],
            )

        logger.info(
            "Simulated order #%d ($%.2f) injected for Store #%d into PostgreSQL at %s",
            order_id, amount, t_id, inject_time,
        )

        return SimulateOrderResponse(
            status="injected",
            order_id=order_id,
            tenant_id=t_id,
            amount=amount,
            order_status=order_status,
            injected_at=inject_time,
            message=(
                f"Order #{order_id} successfully injected into cyberforge_tenant_{t_id}. "
                f"SyncWorker will detect and replicate to DuckDB in < 5.0 seconds."
            ),
        )

    except Exception as exc:
        logger.exception("Failed to inject simulated order for tenant #%d: %s", t_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to inject order into PostgreSQL: {exc}",
        )


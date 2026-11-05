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
from datetime import datetime, timedelta, timezone

import duckdb
from fastapi import APIRouter, Depends, HTTPException

from aggregator.api.dependencies import get_sync_worker
from aggregator.config import settings
from aggregator.database import duckdb_store
from aggregator.database.duckdb_store import duckdb_total_orders
from aggregator.database.postgres import get_tenant_pg_connection, pg_order_count_estimate
from aggregator.models.schemas import (
    AdminMetricsResponse,
    FreshnessTestRequest,
    FreshnessTestResponse,
    HealthResponse,
    RebuildResponse,
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
        The percentile value, or 0.0 if *data* is empty.
    """
    if not data:
        return 0.0
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
    "/test/freshness",
    response_model=FreshnessTestResponse,
    summary="Run configurable development freshness test",
    description=(
        "Development-only endpoint. Inserts test orders into one tenant "
        "database and waits for the replica. Test orders remain in the source "
        "and replica so a later restart does not require a rebuild. "
        "Enable with ENABLE_TEST_UI=true."
    ),
)
def run_freshness_test(
    request: FreshnessTestRequest,
    worker: SyncWorker = Depends(get_sync_worker),
) -> FreshnessTestResponse:
    """Run an end-to-end insert, sync, and observation test."""
    if not settings.enable_test_ui:
        raise HTTPException(
            status_code=404,
            detail="Test UI is disabled. Set ENABLE_TEST_UI=true for development.",
        )
    if request.tenant_id not in worker.tenant_ids:
        raise HTTPException(status_code=404, detail="Tenant does not exist.")

    started = time.monotonic()
    order_ids: list[int] = []
    before_count = 0
    before_revenue = 0.0
    after_count = 0
    after_revenue = before_revenue

    with get_tenant_pg_connection(request.tenant_id) as conn:
        baseline = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_amount), 0) FROM orders"
        ).fetchone()
        before_count, before_revenue = int(baseline[0]), float(baseline[1])
        source_max = conn.execute(
            "SELECT COALESCE(MAX(id), 0), COALESCE(MAX(updated_at), %s) "
            "FROM orders",
            [datetime.now(tz=timezone.utc)],
        ).fetchone()
        next_id = int(source_max[0]) + 1
        updated_at = source_max[1]
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        updated_at = max(updated_at, datetime.now(tz=timezone.utc))
        for offset in range(request.order_count):
            order_id = next_id + offset
            order_ids.append(order_id)
            timestamp = updated_at + timedelta(microseconds=offset + 1)
            conn.execute(
                """
                INSERT INTO orders
                    (id, tenant_id, customer_id, status, total_amount,
                     created_at, updated_at)
                VALUES (%s, %s, 1, 'placed', %s, %s, %s)
                """,
                [
                    order_id,
                    request.tenant_id,
                    request.amount,
                    timestamp,
                    timestamp,
                ],
            )

        deadline = started + request.max_wait_seconds
        detected = 0
        while time.monotonic() < deadline:
            time.sleep(request.poll_interval_seconds)
            conn = duckdb_store.get_tenant_duckdb(request.tenant_id)
            with duckdb_store.get_tenant_write_lock(request.tenant_id):
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_amount), 0) "
                    "FROM orders WHERE id IN ("
                    + ",".join("?" for _ in order_ids)
                    + ")",
                    order_ids,
                ).fetchone()
            detected = int(row[0])
            if detected == len(order_ids):
                after_count = before_count + detected
                after_revenue = before_revenue + float(row[1])
                break

        if detected != len(order_ids):
            conn = duckdb_store.get_tenant_duckdb(request.tenant_id)
            with duckdb_store.get_tenant_write_lock(request.tenant_id):
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_amount), 0) "
                    "FROM orders WHERE id IN ("
                    + ",".join("?" for _ in order_ids)
                    + ")",
                    order_ids,
                ).fetchone()
            detected = int(row[0])
            after_count = before_count + detected
            after_revenue = before_revenue + float(row[1])

    freshness = time.monotonic() - started if detected == len(order_ids) else None
    return FreshnessTestResponse(
        tenant_id=request.tenant_id,
        order_count=request.order_count,
        orders_detected=detected,
        order_ids=order_ids,
        freshness_seconds=freshness,
        order_count_before=before_count,
        order_count_after=after_count,
        revenue_delta=round(after_revenue - before_revenue, 2),
        target_met=freshness is not None and freshness <= 30.0,
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

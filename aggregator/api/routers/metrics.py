"""
aggregator/api/routers/metrics.py — Tenant dashboard metric endpoints.

All endpoints:
  • Are prefixed with ``/tenants/{tenant_id}``
  • Enforce strict tenant isolation (every DuckDB query has ``WHERE tenant_id = ?``)
  • Include freshness metadata (``data_freshness_seconds``, ``last_synced_at``)
  • Accept ``from_date`` / ``to_date`` ISO datetime query parameters
    (default: last 30 days → now)
  • Are rate-limited per tenant via ``check_rate_limit``
  • Return HTTP 503 until the initial sync is complete

Endpoints
---------
  FR-1  GET /tenants/{tenant_id}/metrics/summary
  FR-2  GET /tenants/{tenant_id}/metrics/trend
  FR-3  GET /tenants/{tenant_id}/metrics/top-products
  FR-4  GET /tenants/{tenant_id}/metrics/breakdown/status
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aggregator.api.dependencies import (
    check_rate_limit,
    get_duckdb,
    get_sync_worker,
    require_ready,
)
from aggregator.database.duckdb_store import DUCKDB_WRITE_LOCK
from aggregator.models.schemas import (
    StatusBreakdownItem,
    StatusBreakdownResponse,
    SummaryResponse,
    TopProductItem,
    TopProductsResponse,
    TrendDataPoint,
    TrendResponse,
)
from aggregator.sync.worker import SyncWorker

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Metrics"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_from_date() -> datetime:
    """Return a timezone-aware UTC datetime 30 days ago."""
    return datetime.now(tz=timezone.utc) - timedelta(days=30)


def _default_to_date() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(tz=timezone.utc)


def _parse_date(value: str | None, default: datetime) -> datetime:
    """Parse an ISO-8601 date string and make it timezone-aware (UTC).

    Args:
        value:   Raw string from the query parameter, or None.
        default: Fallback datetime when *value* is None.

    Returns:
        A timezone-aware ``datetime`` in UTC.

    Raises:
        HTTPException 400: If *value* cannot be parsed as an ISO-8601 datetime.
    """
    if value is None:
        return default
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid date format: {value!r}. Use ISO-8601 (e.g. '2026-10-01T00:00:00').",
        )


def _freshness(worker: SyncWorker) -> tuple[float, datetime | None]:
    """Compute data freshness metadata from the sync worker state.

    Args:
        worker: The running SyncWorker instance.

    Returns:
        Tuple of ``(freshness_seconds, last_synced_at)``.
    """
    last = worker.last_sync_time
    if last is None:
        return 9999.0, None
    freshness = (datetime.now(tz=timezone.utc) - last).total_seconds()
    return max(freshness, 0.0), last


# ---------------------------------------------------------------------------
# FR-1 — Summary
# ---------------------------------------------------------------------------

@router.get(
    "/tenants/{tenant_id}/metrics/summary",
    response_model=SummaryResponse,
    summary="Order summary metrics for a date range",
    description=(
        "Returns total order count, total revenue, and average order value "
        "for the specified tenant within the requested date window."
    ),
)
def get_summary(
    tenant_id: int,
    from_date: Annotated[str | None, Query(description="Start of window (ISO-8601)")] = None,
    to_date: Annotated[str | None, Query(description="End of window (ISO-8601)")] = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_duckdb),
    worker: SyncWorker = Depends(get_sync_worker),
    _ready: None = Depends(require_ready),
    _rate: None = Depends(check_rate_limit),
) -> SummaryResponse:
    """Aggregate order statistics for a single tenant within a date window.

    Args:
        tenant_id: Tenant primary key (from URL path).
        from_date: Inclusive range start.  Defaults to 30 days ago.
        to_date:   Inclusive range end.  Defaults to now.
        conn:      Injected DuckDB connection.
        worker:    Injected SyncWorker (for freshness metadata).

    Returns:
        ``SummaryResponse`` with order_count, total_revenue, avg_order_value.
    """
    fd = _parse_date(from_date, _default_from_date())
    td = _parse_date(to_date, _default_to_date())

    with DUCKDB_WRITE_LOCK:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                             AS order_count,
                COALESCE(SUM(total_amount),  0.0)   AS total_revenue,
                COALESCE(AVG(total_amount),  0.0)   AS avg_order_value
            FROM orders
            WHERE tenant_id = ?
              AND created_at >= ?
              AND created_at <= ?
            """,
            [tenant_id, fd, td],
        ).fetchone()

    freshness_secs, last_synced = _freshness(worker)

    return SummaryResponse(
        tenant_id=tenant_id,
        order_count=int(row[0]),
        total_revenue=float(row[1]),
        avg_order_value=float(row[2]),
        from_date=fd,
        to_date=td,
        data_freshness_seconds=freshness_secs,
        last_synced_at=last_synced,
    )


# ---------------------------------------------------------------------------
# FR-2 — Trend
# ---------------------------------------------------------------------------

@router.get(
    "/tenants/{tenant_id}/metrics/trend",
    response_model=TrendResponse,
    summary="Revenue and order-count time series",
    description=(
        "Returns order count and revenue grouped by hour or day for the "
        "specified tenant within the requested date window."
    ),
)
def get_trend(
    tenant_id: int,
    from_date: Annotated[str | None, Query(description="Start of window (ISO-8601)")] = None,
    to_date: Annotated[str | None, Query(description="End of window (ISO-8601)")] = None,
    interval: Annotated[str, Query(description="Grouping interval: 'hour' or 'day'")] = "day",
    conn: duckdb.DuckDBPyConnection = Depends(get_duckdb),
    worker: SyncWorker = Depends(get_sync_worker),
    _ready: None = Depends(require_ready),
    _rate: None = Depends(check_rate_limit),
) -> TrendResponse:
    """Time-series breakdown of orders and revenue.

    Args:
        tenant_id: Tenant primary key (from URL path).
        from_date: Inclusive range start.  Defaults to 30 days ago.
        to_date:   Inclusive range end.  Defaults to now.
        interval:  ``"hour"`` or ``"day"`` (default ``"day"``).
        conn:      Injected DuckDB connection.
        worker:    Injected SyncWorker.

    Returns:
        ``TrendResponse`` with a list of time-bucketed data points.

    Raises:
        HTTPException 400: If *interval* is not ``"hour"`` or ``"day"``.
    """
    if interval not in ("hour", "day"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'interval' must be 'hour' or 'day'.",
        )

    fd = _parse_date(from_date, _default_from_date())
    td = _parse_date(to_date, _default_to_date())

    # DuckDB DATE_TRUNC format string and display format
    trunc_unit = "hour" if interval == "hour" else "day"
    fmt = "%Y-%m-%dT%H:00:00" if interval == "hour" else "%Y-%m-%d"

    with DUCKDB_WRITE_LOCK:
        rows = conn.execute(
            f"""
            SELECT
                DATE_TRUNC('{trunc_unit}', created_at)  AS period,
                COUNT(*)                                 AS order_count,
                COALESCE(SUM(total_amount), 0.0)         AS revenue
            FROM orders
            WHERE tenant_id = ?
              AND created_at >= ?
              AND created_at <= ?
            GROUP BY period
            ORDER BY period ASC
            """,
            [tenant_id, fd, td],
        ).fetchall()

    data = [
        TrendDataPoint(
            period=row[0].strftime(fmt) if hasattr(row[0], "strftime") else str(row[0]),
            order_count=int(row[1]),
            revenue=float(row[2]),
        )
        for row in rows
    ]

    freshness_secs, last_synced = _freshness(worker)

    return TrendResponse(
        tenant_id=tenant_id,
        interval=interval,
        data=data,
        data_freshness_seconds=freshness_secs,
        last_synced_at=last_synced,
    )


# ---------------------------------------------------------------------------
# FR-3 — Top Products
# ---------------------------------------------------------------------------

@router.get(
    "/tenants/{tenant_id}/metrics/top-products",
    response_model=TopProductsResponse,
    summary="Top N products by revenue",
    description=(
        "Returns the top N products ranked by total revenue for the specified "
        "tenant within the requested date window."
    ),
)
def get_top_products(
    tenant_id: int,
    from_date: Annotated[str | None, Query(description="Start of window (ISO-8601)")] = None,
    to_date: Annotated[str | None, Query(description="End of window (ISO-8601)")] = None,
    limit: Annotated[int, Query(ge=1, le=50, description="Max products to return (1–50)")] = 10,
    conn: duckdb.DuckDBPyConnection = Depends(get_duckdb),
    worker: SyncWorker = Depends(get_sync_worker),
    _ready: None = Depends(require_ready),
    _rate: None = Depends(check_rate_limit),
) -> TopProductsResponse:
    """Rank products by revenue within a date window.

    Args:
        tenant_id: Tenant primary key (from URL path).
        from_date: Inclusive range start.  Defaults to 30 days ago.
        to_date:   Inclusive range end.  Defaults to now.
        limit:     Number of top products to return (1–50, default 10).
        conn:      Injected DuckDB connection.
        worker:    Injected SyncWorker.

    Returns:
        ``TopProductsResponse`` with ranked product data.
    """
    fd = _parse_date(from_date, _default_from_date())
    td = _parse_date(to_date, _default_to_date())

    with DUCKDB_WRITE_LOCK:
        rows = conn.execute(
            """
            SELECT
                p.id                          AS product_id,
                p.name                        AS name,
                p.category                    AS category,
                SUM(oi.quantity)              AS total_quantity,
                COALESCE(SUM(oi.total_price), 0.0) AS total_revenue
            FROM order_items oi
            JOIN products p
              ON oi.product_id = p.id
            WHERE oi.tenant_id = ?
              AND oi.created_at >= ?
              AND oi.created_at <= ?
            GROUP BY p.id, p.name, p.category
            ORDER BY total_revenue DESC
            LIMIT ?
            """,
            [tenant_id, fd, td, limit],
        ).fetchall()

    data = [
        TopProductItem(
            rank=idx + 1,
            product_id=int(row[0]),
            name=str(row[1]),
            category=str(row[2]),
            total_quantity=int(row[3]),
            total_revenue=float(row[4]),
        )
        for idx, row in enumerate(rows)
    ]

    freshness_secs, last_synced = _freshness(worker)

    return TopProductsResponse(
        tenant_id=tenant_id,
        limit=limit,
        data=data,
        data_freshness_seconds=freshness_secs,
        last_synced_at=last_synced,
    )


# ---------------------------------------------------------------------------
# FR-4 — Status Breakdown
# ---------------------------------------------------------------------------

@router.get(
    "/tenants/{tenant_id}/metrics/breakdown/status",
    response_model=StatusBreakdownResponse,
    summary="Order count and revenue by status",
    description=(
        "Returns a breakdown of orders grouped by status (placed, shipped, "
        "delivered, cancelled) with counts, revenue, and percentage."
    ),
)
def get_status_breakdown(
    tenant_id: int,
    from_date: Annotated[str | None, Query(description="Start of window (ISO-8601)")] = None,
    to_date: Annotated[str | None, Query(description="End of window (ISO-8601)")] = None,
    conn: duckdb.DuckDBPyConnection = Depends(get_duckdb),
    worker: SyncWorker = Depends(get_sync_worker),
    _ready: None = Depends(require_ready),
    _rate: None = Depends(check_rate_limit),
) -> StatusBreakdownResponse:
    """Break down orders by status within a date window.

    Args:
        tenant_id: Tenant primary key (from URL path).
        from_date: Inclusive range start.  Defaults to 30 days ago.
        to_date:   Inclusive range end.  Defaults to now.
        conn:      Injected DuckDB connection.
        worker:    Injected SyncWorker.

    Returns:
        ``StatusBreakdownResponse`` with per-status counts, revenue, and
        percentage of total orders.
    """
    fd = _parse_date(from_date, _default_from_date())
    td = _parse_date(to_date, _default_to_date())

    with DUCKDB_WRITE_LOCK:
        rows = conn.execute(
            """
            SELECT
                status,
                COUNT(*)                           AS count,
                COALESCE(SUM(total_amount), 0.0)   AS revenue
            FROM orders
            WHERE tenant_id = ?
              AND created_at >= ?
              AND created_at <= ?
            GROUP BY status
            """,
            [tenant_id, fd, td],
        ).fetchall()

    total_orders = sum(int(row[1]) for row in rows)

    data = [
        StatusBreakdownItem(
            status=str(row[0]),
            count=int(row[1]),
            revenue=float(row[2]),
            percentage=round(int(row[1]) / total_orders * 100, 2) if total_orders else 0.0,
        )
        for row in rows
    ]

    freshness_secs, last_synced = _freshness(worker)

    return StatusBreakdownResponse(
        tenant_id=tenant_id,
        data=data,
        total_orders=total_orders,
        data_freshness_seconds=freshness_secs,
        last_synced_at=last_synced,
    )

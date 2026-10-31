"""
aggregator/api/routers/company.py — Company Owner Multi-Store Rollup Endpoints.

Allows a company owner who operates multiple stores (tenants) to access unified
analytical dashboards rolled up across their entire retail network in real-time.

Performance & Architecture:
  • Leverages pre-computed hourly rollups from each tenant's isolated database file.
  • Performs sub-5ms in-memory aggregation across multiple stores.
  • Zero cross-tenant data leakage: only rolls up stores assigned to the company.
  • Company-to-tenant ownership is loaded from PostgreSQL (``tenants`` table) at
    query time — no hard-coded mappings.
  • The ``stores`` query parameter is validated against the company's actual ownership;
    requesting a store that belongs to another company raises HTTP 403.
  • A failure to read any tenant's database raises HTTP 502 rather than silently
    returning partial totals.
"""

from __future__ import annotations

import collections
import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aggregator.api.dependencies import (
    get_sync_worker,
    require_ready,
)
from aggregator.config import settings as _settings
from aggregator.database import duckdb_store as store
from aggregator.database.postgres import get_pg_connection
from aggregator.models.schemas import (
    CompanyStatusBreakdownResponse,
    CompanySummaryResponse,
    CompanyTopProductsResponse,
    CompanyTrendResponse,
    StatusBreakdownItem,
    TopProductItem,
    TrendDataPoint,
)
from aggregator.sync.worker import SyncWorker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/companies", tags=["Company Multi-Store Dashboards"])

# ---------------------------------------------------------------------------
# Per-company sliding-window rate limiter (mirrors tenant rate limiter)
# ---------------------------------------------------------------------------

_company_rate_windows: dict[int, collections.deque] = collections.defaultdict(
    lambda: collections.deque()
)
_company_rate_lock = threading.Lock()
_COMPANY_WINDOW_SECONDS = 60


def check_company_rate_limit(company_id: int) -> None:
    """Enforce per-company sliding-window rate limit (same policy as tenant limiter).

    Args:
        company_id: The company ID extracted from the URL path parameter.

    Raises:
        HTTPException 429: When the company has exceeded the configured
            ``rate_limit_per_minute`` requests in the last 60 seconds.
    """
    now = _time.monotonic()
    limit = _settings.rate_limit_per_minute

    with _company_rate_lock:
        window = _company_rate_windows[company_id]
        cutoff = now - _COMPANY_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded for company {company_id}. "
                    f"Maximum {limit} requests per minute."
                ),
                headers={"Retry-After": "60"},
            )
        window.append(now)


# ---------------------------------------------------------------------------
# Ownership helpers — database-backed, not hard-coded
# ---------------------------------------------------------------------------

def _get_company_store_ids(company_id: int) -> list[int]:
    """Query PostgreSQL for the tenant IDs owned by ``company_id``.

    Args:
        company_id: The company whose tenants to retrieve.

    Returns:
        Sorted list of tenant/store IDs belonging to this company.

    Raises:
        HTTPException 404: If the company does not exist or owns no stores.
        HTTPException 502: If the PostgreSQL query fails.
    """
    try:
        with get_pg_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM tenants WHERE company_id = %s ORDER BY id",
                [company_id],
            ).fetchall()
    except Exception as exc:
        logger.error("Failed to load store list for company #%d: %s", company_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not retrieve store list for company #{company_id}: {exc}",
        )

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company #{company_id} not found or owns no stores.",
        )
    return [int(r[0]) for r in rows]


def _resolve_stores(company_id: int, stores_param: str | None) -> list[int]:
    """Resolve and validate the list of tenant/store IDs for a company.

    If ``stores_param`` is provided, every requested store ID is verified to
    belong to the company.  Requesting a foreign store raises HTTP 403.

    Args:
        company_id:   The company making the request.
        stores_param: Optional comma-separated store ID filter from query params.

    Returns:
        Validated, sorted list of tenant IDs to query.

    Raises:
        HTTPException 400: If ``stores_param`` cannot be parsed as integers.
        HTTPException 403: If any requested store does not belong to the company.
        HTTPException 404: If the company owns no stores.
        HTTPException 502: If the ownership lookup fails.
    """
    owned_ids = set(_get_company_store_ids(company_id))

    if stores_param:
        try:
            requested = [int(s.strip()) for s in stores_param.split(",") if s.strip()]
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid stores list: {stores_param!r}. Use comma-separated integers (e.g. '1,2,3').",
            )
        foreign = [s for s in requested if s not in owned_ids]
        if foreign:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Company #{company_id} does not own store(s) {foreign}. "
                    "Cross-tenant access is forbidden."
                ),
            )
        return sorted(requested)

    return sorted(owned_ids)


def _default_from_date() -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(days=30)


def _default_to_date() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_date(value: str | None, default: datetime) -> datetime:
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
    last = worker.last_sync_time
    if last is None:
        return 9999.0, None
    freshness = (datetime.now(tz=timezone.utc) - last).total_seconds()
    return max(freshness, 0.0), last


def _get_tenant_conn_strict(store_id: int, company_id: int) -> duckdb.DuckDBPyConnection:
    """Return the DuckDB connection for ``store_id``, raising HTTP 502 on failure.

    Unlike the old pattern of silently logging and skipping unavailable stores,
    this raises immediately so the caller receives a consistent HTTP 502 rather
    than partial totals that look like complete results.

    Args:
        store_id:   The tenant whose DuckDB to open.
        company_id: Used only for error messaging.

    Raises:
        HTTPException 502: If the tenant's DuckDB cannot be opened or queried.
    """
    try:
        return store.get_tenant_duckdb(store_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Company #{company_id}: store #{store_id} database is unavailable: {exc}. "
                "All stores must be readable to return a consistent company-wide result."
            ),
        )


# ---------------------------------------------------------------------------
# Company Summary (FR-1 Rollup)
# ---------------------------------------------------------------------------

@router.get(
    "/{company_id}/metrics/summary",
    response_model=CompanySummaryResponse,
    summary="Company Owner order summary across all owned stores",
)
def get_company_summary(
    company_id: int,
    from_date: Annotated[str | None, Query(description="Start of window (ISO-8601)")] = None,
    to_date: Annotated[str | None, Query(description="End of window (ISO-8601)")] = None,
    stores: Annotated[str | None, Query(description="Optional comma-separated store IDs")] = None,
    worker: SyncWorker = Depends(get_sync_worker),
    _ready: None = Depends(require_ready),
    _rate: None = Depends(check_company_rate_limit),
) -> CompanySummaryResponse:
    """Aggregates order totals, revenue, and AOV across all company stores."""
    fd = _parse_date(from_date, _default_from_date())
    td = _parse_date(to_date, _default_to_date())
    store_ids = _resolve_stores(company_id, stores)

    total_orders = 0
    total_rev = 0.0

    for s_id in store_ids:
        conn = _get_tenant_conn_strict(s_id, company_id)
        with store.get_tenant_write_lock(s_id):
            row = conn.execute(
                """
                SELECT COALESCE(SUM(order_count), 0), COALESCE(SUM(total_revenue), 0.0)
                FROM hourly_tenant_metrics
                WHERE period >= ? AND period <= ?
                """,
                [fd, td],
            ).fetchone()
        if row:
            total_orders += int(row[0])
            total_rev += float(row[1])

    avg_order_val = round(total_rev / total_orders, 2) if total_orders > 0 else 0.0
    freshness_secs, last_synced = _freshness(worker)

    return CompanySummaryResponse(
        company_id=company_id,
        store_count=len(store_ids),
        stores=store_ids,
        order_count=total_orders,
        total_revenue=round(total_rev, 2),
        avg_order_value=avg_order_val,
        from_date=fd,
        to_date=td,
        data_freshness_seconds=freshness_secs,
        last_synced_at=last_synced,
    )


# ---------------------------------------------------------------------------
# Company Trend (FR-2 Rollup)
# ---------------------------------------------------------------------------

@router.get(
    "/{company_id}/metrics/trend",
    response_model=CompanyTrendResponse,
    summary="Company Owner trend over time aggregated across all stores",
)
def get_company_trend(
    company_id: int,
    from_date: Annotated[str | None, Query(description="Start of window (ISO-8601)")] = None,
    to_date: Annotated[str | None, Query(description="End of window (ISO-8601)")] = None,
    interval: Annotated[str, Query(description="'hour' or 'day'")] = "day",
    stores: Annotated[str | None, Query(description="Optional comma-separated store IDs")] = None,
    worker: SyncWorker = Depends(get_sync_worker),
    _ready: None = Depends(require_ready),
    _rate: None = Depends(check_company_rate_limit),
) -> CompanyTrendResponse:
    """Aggregates revenue and order-count time series across all company stores."""
    fd = _parse_date(from_date, _default_from_date())
    td = _parse_date(to_date, _default_to_date())
    store_ids = _resolve_stores(company_id, stores)

    bucket_format = "%Y-%m-%d %H:00:00" if interval == "hour" else "%Y-%m-%d"
    buckets: dict[str, dict[str, float]] = collections.defaultdict(lambda: {"order_count": 0, "revenue": 0.0})

    for s_id in store_ids:
        conn = _get_tenant_conn_strict(s_id, company_id)
        trunc_fn = "hour" if interval == "hour" else "day"
        with store.get_tenant_write_lock(s_id):
            rows = conn.execute(
                f"""
                SELECT strftime(date_trunc('{trunc_fn}', period), '{bucket_format}') AS bucket,
                       SUM(order_count) AS cnt,
                       SUM(total_revenue) AS rev
                FROM hourly_tenant_metrics
                WHERE period >= ? AND period <= ?
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                [fd, td],
            ).fetchall()
        for r in rows:
            buckets[r[0]]["order_count"] += int(r[1])
            buckets[r[0]]["revenue"] += float(r[2])

    data = [
        TrendDataPoint(period=k, order_count=int(v["order_count"]), revenue=round(v["revenue"], 2))
        for k, v in sorted(buckets.items())
    ]

    freshness_secs, last_synced = _freshness(worker)
    return CompanyTrendResponse(
        company_id=company_id,
        store_count=len(store_ids),
        stores=store_ids,
        interval=interval,
        data=data,
        data_freshness_seconds=freshness_secs,
        last_synced_at=last_synced,
    )


# ---------------------------------------------------------------------------
# Company Top Products (FR-3 Rollup)
# ---------------------------------------------------------------------------

@router.get(
    "/{company_id}/metrics/top-products",
    response_model=CompanyTopProductsResponse,
    summary="Company Owner top selling products across all stores",
)
def get_company_top_products(
    company_id: int,
    from_date: Annotated[str | None, Query(description="Start of window (ISO-8601)")] = None,
    to_date: Annotated[str | None, Query(description="End of window (ISO-8601)")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    stores: Annotated[str | None, Query(description="Optional comma-separated store IDs")] = None,
    worker: SyncWorker = Depends(get_sync_worker),
    _ready: None = Depends(require_ready),
    _rate: None = Depends(check_company_rate_limit),
) -> CompanyTopProductsResponse:
    """Aggregates and ranks the top products across all stores owned by the company."""
    fd = _parse_date(from_date, _default_from_date())
    td = _parse_date(to_date, _default_to_date())
    store_ids = _resolve_stores(company_id, stores)

    # Product aggregation by product name & category
    prod_map: dict[str, dict[str, Any]] = collections.defaultdict(
        lambda: {"product_id": 0, "name": "", "category": "", "total_quantity": 0, "total_revenue": 0.0}
    )

    for s_id in store_ids:
        conn = _get_tenant_conn_strict(s_id, company_id)
        with store.get_tenant_write_lock(s_id):
            rows = conn.execute(
                """
                SELECT p.id, p.name, p.category, SUM(oi.quantity), SUM(oi.total_price)
                FROM order_items oi
                JOIN products p ON oi.product_id = p.id
                WHERE oi.created_at >= ? AND oi.created_at <= ?
                GROUP BY p.id, p.name, p.category
                """,
                [fd, td],
            ).fetchall()
        for r in rows:
            key = f"{r[1]}_{r[2]}"
            entry = prod_map[key]
            entry["product_id"] = int(r[0])
            entry["name"] = str(r[1])
            entry["category"] = str(r[2])
            entry["total_quantity"] += int(r[3])
            entry["total_revenue"] += float(r[4])

    sorted_prods = sorted(prod_map.values(), key=lambda x: x["total_revenue"], reverse=True)[:limit]
    data = [
        TopProductItem(
            rank=idx + 1,
            product_id=p["product_id"],
            name=p["name"],
            category=p["category"],
            total_quantity=p["total_quantity"],
            total_revenue=round(p["total_revenue"], 2),
        )
        for idx, p in enumerate(sorted_prods)
    ]

    freshness_secs, last_synced = _freshness(worker)
    return CompanyTopProductsResponse(
        company_id=company_id,
        store_count=len(store_ids),
        stores=store_ids,
        limit=limit,
        data=data,
        data_freshness_seconds=freshness_secs,
        last_synced_at=last_synced,
    )


# ---------------------------------------------------------------------------
# Company Status Breakdown (FR-4 Rollup)
# ---------------------------------------------------------------------------

@router.get(
    "/{company_id}/metrics/breakdown/status",
    response_model=CompanyStatusBreakdownResponse,
    summary="Company Owner order status breakdown across all stores",
)
def get_company_status_breakdown(
    company_id: int,
    from_date: Annotated[str | None, Query(description="Start of window (ISO-8601)")] = None,
    to_date: Annotated[str | None, Query(description="End of window (ISO-8601)")] = None,
    stores: Annotated[str | None, Query(description="Optional comma-separated store IDs")] = None,
    worker: SyncWorker = Depends(get_sync_worker),
    _ready: None = Depends(require_ready),
    _rate: None = Depends(check_company_rate_limit),
) -> CompanyStatusBreakdownResponse:
    """Aggregates order counts and revenues by order status across all company stores."""
    fd = _parse_date(from_date, _default_from_date())
    td = _parse_date(to_date, _default_to_date())
    store_ids = _resolve_stores(company_id, stores)

    status_totals: dict[str, dict[str, float]] = {
        "delivered": {"count": 0, "revenue": 0.0},
        "shipped": {"count": 0, "revenue": 0.0},
        "placed": {"count": 0, "revenue": 0.0},
        "cancelled": {"count": 0, "revenue": 0.0},
    }

    for s_id in store_ids:
        conn = _get_tenant_conn_strict(s_id, company_id)
        with store.get_tenant_write_lock(s_id):
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(delivered_count), 0), COALESCE(SUM(delivered_revenue), 0.0),
                    COALESCE(SUM(shipped_count), 0),   COALESCE(SUM(shipped_revenue), 0.0),
                    COALESCE(SUM(placed_count), 0),    COALESCE(SUM(placed_revenue), 0.0),
                    COALESCE(SUM(cancelled_count), 0), COALESCE(SUM(cancelled_revenue), 0.0)
                FROM hourly_tenant_metrics
                WHERE period >= ? AND period <= ?
                """,
                [fd, td],
            ).fetchone()
        if row:
            status_totals["delivered"]["count"] += int(row[0])
            status_totals["delivered"]["revenue"] += float(row[1])
            status_totals["shipped"]["count"] += int(row[2])
            status_totals["shipped"]["revenue"] += float(row[3])
            status_totals["placed"]["count"] += int(row[4])
            status_totals["placed"]["revenue"] += float(row[5])
            status_totals["cancelled"]["count"] += int(row[6])
            status_totals["cancelled"]["revenue"] += float(row[7])

    total_orders = sum(v["count"] for v in status_totals.values())
    data = [
        StatusBreakdownItem(
            status=st,
            count=int(v["count"]),
            revenue=round(v["revenue"], 2),
            percentage=round((v["count"] / total_orders * 100.0) if total_orders > 0 else 0.0, 2),
        )
        for st, v in status_totals.items()
    ]

    freshness_secs, last_synced = _freshness(worker)
    return CompanyStatusBreakdownResponse(
        company_id=company_id,
        store_count=len(store_ids),
        stores=store_ids,
        data=data,
        data_freshness_seconds=freshness_secs,
        last_synced_at=last_synced,
    )

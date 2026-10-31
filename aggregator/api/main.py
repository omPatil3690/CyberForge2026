"""
aggregator/api/main.py — FastAPI application entry point for CyberForge aggregation layer.

Lifecycle:
  1. ``lifespan`` startup: init DuckDB, start SyncWorker, wait for initial sync.
  2. Requests are served through the metrics and admin routers.
  3. ``lifespan`` shutdown: stop SyncWorker, close PostgreSQL pool.

CORS is configured to allow all origins (suitable for hackathon development).

Usage::

    uvicorn aggregator.api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from aggregator.api.dependencies import set_duckdb_connection, set_sync_worker
from aggregator.api.routers import admin as admin_router
from aggregator.api.routers import company as company_router
from aggregator.api.routers import metrics as metrics_router
from aggregator.database.duckdb_store import init_duckdb
from aggregator.database.postgres import close_pool
from aggregator.models.schemas import APIInfoResponse
from aggregator.sync.worker import SyncWorker

# Configure root logger so that aggregator.* logs appear on stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan context manager
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of stateful resources.

    Startup sequence:
        1. Open DuckDB connection and create schema (idempotent).
        2. Register DuckDB connection with the dependency layer.
        3. Create and start SyncWorker daemon thread.
        4. Register SyncWorker with the dependency layer.
        5. Block (with a short poll) until the initial sync marks ``is_ready``.

    Shutdown sequence:
        1. Stop SyncWorker (signals the daemon thread and waits).
        2. Close PostgreSQL connection pool.

    Yields:
        Control to the FastAPI request handler loop while resources are live.
    """
    logger.info("=== CyberForge Aggregation Layer — starting up ===")

    # 1. Init DuckDB
    duck_conn = init_duckdb()
    set_duckdb_connection(duck_conn)

    # 2. Create and start sync worker
    worker = SyncWorker(duck_conn)
    set_sync_worker(worker)
    worker.start()

    # 3. Wait for the initial sync to complete before serving requests
    logger.info("Waiting for initial sync to complete…")
    _wait_start = time.monotonic()
    while not worker.is_ready:
        time.sleep(0.5)
        elapsed = time.monotonic() - _wait_start
        if elapsed > 600:  # 10-minute guard for extremely large datasets
            logger.warning(
                "Initial sync taking longer than 10 minutes — starting API anyway."
            )
            break

    logger.info(
        "Initial sync complete in %.1f s. API is ready.",
        time.monotonic() - _wait_start,
    )

    # ---- serve requests ----
    yield

    # ---- shutdown ----
    logger.info("=== CyberForge Aggregation Layer — shutting down ===")
    worker.stop()
    close_pool()
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CyberForge Aggregation API",
    description=(
        "Multi-Tenant OLAP Aggregation Layer for CyberForge2026. "
        "Provides sub-50ms dashboard queries backed by an incremental "
        "DuckDB replica synced from PostgreSQL every 5 seconds."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS middleware (allow all origins for hackathon dashboard integration)
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

# Metrics router: /tenants/{tenant_id}/metrics/...
app.include_router(metrics_router.router)

# Company owner multi-store router: /companies/{company_id}/metrics/...
app.include_router(company_router.router)

# Admin router: /admin/...
app.include_router(admin_router.router)

# ---------------------------------------------------------------------------
# Root endpoint
# ---------------------------------------------------------------------------

@app.get(
    "/",
    response_model=APIInfoResponse,
    summary="API information",
    tags=["Info"],
)
def root() -> APIInfoResponse:
    """Return basic API metadata and available endpoint groups.

    Returns:
        ``APIInfoResponse`` describing the API name, version, and endpoints.
    """
    return APIInfoResponse(
        name="CyberForge Aggregation API",
        version="1.0.0",
        description=(
            "Multi-Tenant OLAP Aggregation Layer — sub-50ms dashboard queries "
            "backed by an incremental DuckDB replica (≤10s freshness)."
        ),
        docs_url="/docs",
        endpoints={
            "metrics": {
                "summary":          "GET /tenants/{tenant_id}/metrics/summary",
                "trend":            "GET /tenants/{tenant_id}/metrics/trend",
                "top_products":     "GET /tenants/{tenant_id}/metrics/top-products",
                "status_breakdown": "GET /tenants/{tenant_id}/metrics/breakdown/status",
            },
            "company": {
                "summary":          "GET /companies/{company_id}/metrics/summary",
                "trend":            "GET /companies/{company_id}/metrics/trend",
                "top_products":     "GET /companies/{company_id}/metrics/top-products",
                "status_breakdown": "GET /companies/{company_id}/metrics/breakdown/status",
            },
            "admin": {
                "health":  "GET  /admin/health",
                "metrics": "GET  /admin/metrics",
                "rebuild": "POST /admin/rebuild",
            },
        },
    )

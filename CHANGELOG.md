# Changelog

All notable changes to the CyberForge2026 Multi-Tenant Aggregation Layer project are documented in this file according to the standards defined in `INSTRUCTIONS.md`.

---

#### Change #001 — Initialize Changelog & Adopt Development Standards

- **Timestamp:** 2026-10-31 12:29:00 IST (UTC+05:30)
- **Agent/Author:** Antigravity
- **Type:** Documentation
- **Description:**
  - Created and formatted `CHANGELOG.md` to establish strict change tracking across all development work.
  - Aligned changelog schema with the official instructions specified in `INSTRUCTIONS.md` (sequential 3-digit numbering, explicit timestamps with timezone, structured metadata, and detailed impact descriptions).
  - Documented baseline project setup and problem statement review for the Multi-Tenant Aggregation Layer.
- **Affected Files:**
  - `CHANGELOG.md`

---

#### Change #002 — PostgreSQL Schema & Synthetic Data Generation Pipeline

- **Timestamp:** 2026-10-31 12:51:00 IST (UTC+05:30)
- **Agent/Author:** Antigravity
- **Type:** Database
- **Description:**
  - Designed and authored the relational PostgreSQL database schema in `schema/schema.sql` supporting tenants, customers, products, orders, and order items.
  - Implemented multi-tenant isolation via mandatory `tenant_id` foreign keys and optimized composite indexing for range queries (`tenant_id, created_at`), status breakdowns (`tenant_id, status`), and product performance (`tenant_id, product_id`).
  - Created the dedicated PostgreSQL database `cyberforge` and initialized all tables, constraints, and indexes.
  - Implemented `scripts/generate_data.py` providing parametric synthetic data generation directly into PostgreSQL using high-speed streaming `COPY FROM STDIN` buffers.
  - Modeled realistic real-world e-commerce dynamics: Zipfian/power-law volume skew across tenants, product popularity power curves, diurnal order timestamp distributions, and multi-state order transitions (`placed`, `shipped`, `delivered`, `cancelled`).
  - Implemented `scripts/verify_data.py` to inspect volume distributions and execute baseline validation queries for FR-1 (time-range aggregates), FR-2 (time-series), FR-3 (top-N products), and FR-4 (status breakdowns).
  - Configured Python virtual environment `.venv` with `psycopg[binary]` and generated `requirements.txt`.
- **Affected Files:**
  - `schema/schema.sql`
  - `scripts/generate_data.py`
  - `scripts/verify_data.py`
  - `requirements.txt`
  - `.gitignore`
  - `CHANGELOG.md`

---

#### Change #003 — Database Migration Framework & Initial Migration Setup

- **Timestamp:** 2026-10-31 12:53:00 IST (UTC+05:30)
- **Agent/Author:** Antigravity
- **Type:** Database
- **Description:**
  - Implemented an automated, transaction-safe database migration framework for schema maintenance.
  - Created `scripts/migrate.py` supporting CLI subcommands: `up` (apply pending migrations), `down` (atomic rollbacks with configurable `--steps`), `status` (overview of applied vs pending versions), and `create` (scaffolds new numbered migration templates).
  - Configured PostgreSQL `schema_migrations` audit table to record migration versions, names, and exact execution timestamps.
  - Converted the raw database schema into versioned migration pair `migrations/001_initial_schema.up.sql` and `migrations/001_initial_schema.down.sql`.
  - Added idempotency (`IF NOT EXISTS` / `IF EXISTS`) and transactional guarantees so partial migrations fail safely without leaving the schema in an inconsistent state.
- **Affected Files:**
  - `migrations/001_initial_schema.up.sql`
  - `migrations/001_initial_schema.down.sql`
  - `scripts/migrate.py`
  - `CHANGELOG.md`

---

#### Change #004 — Centralized Database Connection & Environment Configuration

- **Timestamp:** 2026-10-31 12:56:00 IST (UTC+05:30)
- **Agent/Author:** Antigravity
- **Type:** Configuration
- **Description:**
  - Created `.env.example` detailing all required PostgreSQL connection environment variables (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DATABASE_URL`).
  - Implemented centralized database utilities in `scripts/db.py` with an embedded, zero-dependency `.env` loader.
  - Refactored `scripts/migrate.py`, `scripts/generate_data.py`, and `scripts/verify_data.py` to use `get_connection()` for consistent credential and connection resolution.
  - Documented exact commands for setting up `.env`, running database migrations, generating synthetic data, and validating datasets.
- **Affected Files:**
  - `.env.example`
  - `scripts/db.py`
  - `scripts/migrate.py`
  - `scripts/generate_data.py`
  - `scripts/verify_data.py`
  - `CHANGELOG.md`

---

#### Change #005 — Reorder Changelog to Chronological Order (Append-Only at Bottom)

- **Timestamp:** 2026-10-31 12:58:00 IST (UTC+05:30)
- **Agent/Author:** Antigravity
- **Type:** Documentation
- **Description:**
  - Reorganized `CHANGELOG.md` from reverse-chronological order to chronological order (top to bottom).
  - Established convention where historical changes start at the top (Change #001) and all future changes are appended strictly to the bottom of the file.
- **Affected Files:**
  - `CHANGELOG.md`

---

#### Change #006 — Adopt Alembic for Database Migrations

- **Timestamp:** 2026-10-31 13:00:00 IST (UTC+05:30)
- **Agent/Author:** Antigravity
- **Type:** Database
- **Description:**
  - Adopted and configured Alembic as the official schema migration management framework.
  - Installed `alembic` and `sqlalchemy` into the local virtual environment and updated `requirements.txt`.
  - Configured `alembic.ini` and `alembic/env.py` to dynamically construct the PostgreSQL database connection URL from `.env` via `scripts/db.py`.
  - Authored baseline revision `alembic/versions/001_initial_schema.py` covering all tables (`tenants`, `customers`, `products`, `orders`, `order_items`), foreign keys with cascade rules, check constraints, and composite indexes.
  - Refactored `scripts/migrate.py` as an ergonomic CLI wrapper over Alembic commands (`up`, `down`, `status`, `create`).
  - Verified static SQL generation with offline migration check (`alembic upgrade head --sql`).
- **Affected Files:**
  - `alembic.ini`
  - `alembic/env.py`
  - `alembic/script.py.mako`
  - `alembic/versions/001_initial_schema.py`
  - `scripts/migrate.py`
  - `requirements.txt`
  - `migrations/` (removed legacy folder)
  - `CHANGELOG.md`

---

#### Change #007 — Fix PostgreSQL Authentication URL in Environment Configuration

- **Timestamp:** 2026-10-31 13:06:00 IST (UTC+05:30)
- **Agent/Author:** Antigravity
- **Type:** Configuration
- **Description:**
  - Diagnosed connection failure (`FATAL: role "seetara9966" does not exist`) caused by a misplaced credential token in `.env`.
  - Corrected `DATABASE_URL` in `.env` to properly place the username before the colon and the password after (`postgresql://postgres:seetara9966@localhost:5432/cyberforge`).
  - Cleared unmanaged empty tables and executed `alembic upgrade head`, successfully initializing `alembic_version` and all initial schema tables.
- **Affected Files:**
  - `.env`
  - `CHANGELOG.md`

---

#### Change #008 — Generate Baseline Multi-Tenant Synthetic Dataset

- **Timestamp:** 2026-10-31 13:12:00 IST (UTC+05:30)
- **Agent/Author:** Antigravity
- **Type:** Testing
- **Description:**
  - Enhanced `scripts/generate_data.py` CLI default resolution to pull database configuration directly from `scripts/db.py` when flags are omitted.
  - Executed `scripts/generate_data.py` generating 10,000 orders and 17,523 order items across 5 tenants, 200 products, and 750 customers into PostgreSQL in 0.42 seconds.
  - Executed `scripts/verify_data.py` validating Zipfian power-law skew (Tenant #1: 47.1%, Tenant #2: 21.5%, Tenant #3: 14.3%, Tenant #4: 9.4%, Tenant #5: 7.6%) and realistic status distribution.
  - Computed and established baseline query results for FR-1 (7-day range aggregates) and FR-3 (Top 5 products by revenue).
- **Affected Files:**
  - `scripts/generate_data.py`
  - `CHANGELOG.md`

---

#### Change #009 — Add Database and Table Storage Footprint Diagnostics

- **Timestamp:** 2026-10-31 14:26:00 IST (UTC+05:30)
- **Agent/Author:** Antigravity
- **Type:** Enhancement
- **Description:**
  - Added storage diagnostic reporting to `scripts/verify_data.py` using PostgreSQL's `pg_database_size`, `pg_table_size`, and `pg_indexes_size` functions.
  - Enabled automated reporting of overall database disk usage as well as per-table data size, index size, and total relation footprint on disk.
- **Affected Files:**
  - `scripts/verify_data.py`
  - `CHANGELOG.md`

---

#### Change #002 — Architecture Plan, Team Work Split & Hackathon Strategy

- **Timestamp:** 2026-10-31 12:52:45 IST (UTC+05:30)
- **Agent/Author:** Antigravity (AI) & Sandesh Lavshetty
- **Type:** Documentation
- **Description:**
  - Formalized project architecture and work split across the 3 core parts: PostgreSQL DB Generator (Teammate), Core Aggregator Layer (Sandesh & AI), and Analytics Dashboard (Teammate).
  - Identified critical missing hackathon winning factors: automated benchmarking harness (speed, correctness, DB safety), incremental watermark sync pipeline (<30s freshness), and 1-command Docker Compose setup.
  - Outlined the technical stack evaluation recommending FastAPI + Incremental Watermark Worker + DuckDB / Summary Rollup Cache for sub-20ms queries with minimal laptop resource consumption.
  - Specified API endpoint roadmap mapping to requirements FR-1 through FR-7 and bonus capabilities (B-1 to B-5).
- **Affected Files:**
  - `ARCHITECTURE_PLAN.md`
  - `CHANGELOG.md`

---

#### Change #003 — Update Teammate Ownership to Om and Defer Component 4 to Post-MVP

- **Timestamp:** 2026-10-31 12:58:45 IST (UTC+05:30)
- **Agent/Author:** Antigravity (AI) & Sandesh Lavshetty
- **Type:** Documentation
- **Description:**
  - Assigned ownership of Component 1 (Database Generator & Schema) and Component 3 (Analytics Dashboard Frontend) to teammate Om.
  - Deferred Component 4 (Benchmark & Validation Harness) to TBD / Post-MVP status to prioritize delivering a fully functional end-to-end working MVP within hackathon time constraints.
- **Affected Files:**
  - `ARCHITECTURE_PLAN.md`
  - `CHANGELOG.md`



---

#### Change #010 — Core Aggregation Layer Initial Implementation

- **Timestamp:** 2026-10-31 15:22:04 IST (UTC+05:30)
- **Agent/Author:** Antigravity (AI) & Sandesh Lavshetty
- **Type:** Feature
- **Description:**
  - Designed and implemented the full CyberForge Multi-Tenant Aggregation Layer (`aggregator/` package) that sits between the PostgreSQL OLTP database and the dashboard frontend.
  - **Architecture implemented:** PostgreSQL (OLTP, write-only for dashboard) → Incremental Sync Worker (5s watermark poll via `updated_at`) → DuckDB in-process OLAP replica → FastAPI REST API → Dashboard.
  - **`aggregator/__init__.py`**: Package root with version metadata.
  - **`aggregator/config.py`**: Zero-dependency `.env` loader + `Settings` class that exposes `DB_*`, `DATABASE_URL`, `DUCKDB_PATH` (default `./aggregator.duckdb`), `SYNC_INTERVAL_SECONDS` (default 5), and `RATE_LIMIT_PER_MINUTE` (default 60). Module-level `settings` singleton imported everywhere.
  - **`aggregator/models/schemas.py`**: Pydantic v2 response models for all API endpoints: `SummaryResponse`, `TrendResponse`/`TrendDataPoint`, `TopProductsResponse`/`TopProductItem`, `StatusBreakdownResponse`/`StatusBreakdownItem`, `HealthResponse`, `AdminMetricsResponse`, `RebuildResponse`, `APIInfoResponse`. Decimal→float coercion handled natively.
  - **`aggregator/database/postgres.py`**: psycopg v3 connection pool (`psycopg_pool.ConnectionPool`, min=1, max=5) with lazy singleton init; `get_pg_connection()` context manager; `close_pool()` for graceful shutdown; `pg_order_count_estimate()` using `pg_class.reltuples` for non-locking row estimates.
  - **`aggregator/database/duckdb_store.py`**: DuckDB OLAP replica management. Idempotent `init_duckdb()` creates `orders`, `order_items`, `products`, and `sync_state` tables plus covering indexes. `upsert_orders()`, `upsert_order_items()`, `upsert_products()` implement DELETE+INSERT upsert pattern. `get_last_sync_time()` / `set_last_sync_time()` manage the watermark in `sync_state`. Module-level `DUCKDB_WRITE_LOCK` (threading.Lock) serialises all writes and reads to satisfy DuckDB's single-writer constraint.
  - **`aggregator/sync/worker.py`**: `SyncWorker` background daemon thread. On first run with empty DuckDB: full paginated sync of products (all at once), orders (10k batches), and order_items (10k batches). Incremental mode: polls `orders WHERE updated_at > watermark LIMIT 5000`, upserts changed orders, cascades to `order_items` for changed order IDs, advances watermark to `max(updated_at)`. Exposes `is_ready`, `last_sync_time`, `last_sync_row_count`, `sync_lag_seconds`, `total_syncs` for health/metrics endpoints. `trigger_rebuild()` truncates replica and resets for a full re-sync.
  - **`aggregator/api/dependencies.py`**: `get_duckdb()` FastAPI dependency returning the global DuckDB connection; `get_sync_worker()` + `require_ready()` for service readiness gating; sliding-window per-tenant rate limiter (`check_rate_limit(tenant_id)`) using `collections.deque` with a 60-second window.
  - **`aggregator/api/routers/metrics.py`**: All four tenant dashboard endpoints (FR-1 through FR-4), each enforcing tenant isolation (`WHERE tenant_id = ?`), accepting `from_date`/`to_date` query params (defaulting to last 30 days), including freshness metadata, and rate-limiting per tenant.
    - `GET /tenants/{tenant_id}/metrics/summary` — count, total revenue, avg order value.
    - `GET /tenants/{tenant_id}/metrics/trend` — hourly/daily time-series with DATE_TRUNC.
    - `GET /tenants/{tenant_id}/metrics/top-products` — JOIN order_items + products, ranked by revenue (limit 1–50).
    - `GET /tenants/{tenant_id}/metrics/breakdown/status` — order counts/revenue/percentage per status.
  - **`aggregator/api/routers/admin.py`**: Admin endpoints with rolling latency tracking (deque of last 1000 request durations, p50/p95/p99 percentiles).
    - `GET /admin/health` — status, freshness, sync lag, DuckDB row count, pg estimate, uptime.
    - `GET /admin/metrics` — latency percentiles, sync stats, cache hit ratio (always 1.0, all reads from DuckDB).
    - `POST /admin/rebuild` — truncates replica, resets watermarks, triggers full re-sync.
  - **`aggregator/api/main.py`**: FastAPI app with `asynccontextmanager` lifespan that initialises DuckDB, starts SyncWorker, blocks until `is_ready=True` before serving requests, then stops worker and closes pool on shutdown. CORS configured for all origins. Includes both routers.
  - **`run_aggregator.py`**: Project-root entry point — `uvicorn aggregator.api.main:app --host 0.0.0.0 --port 8000`.
  - **`aggregator/requirements.txt`**: Aggregator-specific dependency list (fastapi, uvicorn, duckdb, pydantic, pydantic-settings, python-dotenv, psycopg-pool).
  - **`requirements.txt`** (root): Appended aggregator dependencies to the existing OLTP dependency list.
- **Affected Files:**
  - `aggregator/__init__.py` *(created)*
  - `aggregator/config.py` *(created)*
  - `aggregator/models/__init__.py` *(created)*
  - `aggregator/models/schemas.py` *(created)*
  - `aggregator/database/__init__.py` *(created)*
  - `aggregator/database/postgres.py` *(created)*
  - `aggregator/database/duckdb_store.py` *(created)*
  - `aggregator/sync/__init__.py` *(created)*
  - `aggregator/sync/worker.py` *(created)*
  - `aggregator/api/__init__.py` *(created)*
  - `aggregator/api/dependencies.py` *(created)*
  - `aggregator/api/routers/__init__.py` *(created)*
  - `aggregator/api/routers/metrics.py` *(created)*
  - `aggregator/api/routers/admin.py` *(created)*
  - `aggregator/api/main.py` *(created)*
  - `aggregator/requirements.txt` *(created)*
  - `run_aggregator.py` *(created)*
  - `requirements.txt` *(modified — aggregator deps appended)*
  - `CHANGELOG.md` *(modified — this entry)*

---

#### Change #011 — Environment Configuration for Local PostgreSQL Database

- **Timestamp:** 2026-10-31 15:37:15 IST (UTC+05:30)
- **Agent/Author:** Antigravity (AI) & Sandesh Lavshetty
- **Type:** Configuration
- **Description:**
  - Configured `.env` file pointing to local PostgreSQL 17 instance (`cyberforge` database, user `postgres`).
  - Added default configuration for DuckDB storage path (`./aggregator.duckdb`), sync interval (5 seconds), and per-tenant rate limiter (60 req/min).
- **Affected Files:**
  - `.env`
  - `CHANGELOG.md`

---

#### Change #012 — Fix Route Dependency, Add Timezone Support & Implement End-to-End Test Suite

- **Timestamp:** 2026-10-31 16:02:45 IST (UTC+05:30)
- **Agent/Author:** Antigravity (AI) & Sandesh Lavshetty
- **Type:** Bug Fix
- **Description:**
  - Fixed route dependency scoping error in `aggregator/api/routers/metrics.py` where a lambda default argument caused a `NameError: name 'tenant_id' is not defined` during FastAPI router registration; replaced with direct dependency injection `Depends(check_rate_limit)`.
  - Added `pytz` and `httpx` to `requirements.txt` to enable DuckDB `TIMESTAMPTZ` parsing and FastAPI test harness execution.
  - Created automated live verification suite in `scripts/test_aggregator.py` validating all functional requirements: FR-1 (Summary metrics), FR-2 (Hourly/daily trends), FR-3 (Top-N products), FR-4 (Status breakdown), FR-5 (Tenant data isolation), and B-5 (Query latency diagnostics).
  - Executed live verification against seeded PostgreSQL database (10,000 orders, 17,290 items, 200 products), confirming 100% sync correctness and sub-20ms query execution (p50 latency 16.0ms, subsequent warmed trend queries running in 2.8ms).
- **Affected Files:**
  - `aggregator/api/routers/metrics.py`
  - `requirements.txt`
  - `scripts/test_aggregator.py`
  - `CHANGELOG.md`

---

#### Change #013 — Comprehensive 1M Records Benchmark & Correctness Verification Suite

- **Timestamp:** 2026-10-31 16:18:45 IST (UTC+05:30)
- **Agent/Author:** Antigravity (AI) & Sandesh Lavshetty
- **Type:** Testing
- **Description:**
  - Authored official benchmark and correctness evaluation harness in `scripts/benchmark_1m.py` aligned directly with hackathon targets from `PS.md`.
  - Implemented multi-iteration latency benchmarking (20+ samples per endpoint) measuring p50, p95, and p99 percentiles for FR-1, FR-2, FR-3, and FR-4.
  - Built automated side-by-side correctness checker that runs equivalent raw SQL queries against PostgreSQL and asserts a 100% numerical match (order count discrepancy = 0, revenue delta < 1 cent).
  - Added support for `--generate` (streams 1M synthetic records into PostgreSQL) and `--sync` (forces fresh replica generation).
  - Integrated scorecard generator formatting Markdown and terminal tables showing p50 query execution, raw PostgreSQL speedup comparison, and database safety metrics.
- **Affected Files:**
  - `scripts/benchmark_1m.py`
  - `CHANGELOG.md`




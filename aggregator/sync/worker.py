"""
aggregator/sync/worker.py — True Database-per-Tenant incremental sync worker.

Architecture
------------
Each store owner is a tenant.  Each tenant's orders, products, and order_items
live in their own dedicated PostgreSQL database (``cyberforge_tenant_{id}``)
and are replicated into their own physically isolated DuckDB file
(``data/tenants/tenant_{id}.duckdb``).

There is **no** global ``aggregator.duckdb``; every read and write targets the
per-tenant file exclusively.

Sync strategy
-------------
* **Initial full sync per tenant**: When ``tenant_{id}.duckdb`` has no
  watermark (first run), paginate through that tenant's dedicated PostgreSQL
  database using O(1) keyset pagination and write directly into
  ``data/tenants/tenant_{id}.duckdb``.
* **Incremental sync per tenant**: After the initial sync, poll each tenant's
  PostgreSQL database for rows newer than the tenant's watermark and upsert
  them into the tenant's isolated DuckDB file.
* **Products (full once per tenant)**: Products rarely change; synced once on
  startup or rebuild.
* **Hourly rollup per tenant**: ``hourly_tenant_metrics`` is recomputed inside
  each tenant's DuckDB file after every sync cycle.
* **Watermarks per tenant**: Stored in ``sync_state`` inside each tenant's
  DuckDB file with key ``"orders"`` (tenant-local, not globally shared).

Threading
---------
A single background thread drives all tenant syncs sequentially.  Each tenant
has its own ``threading.Lock`` (via ``get_tenant_write_lock``) so that API
reads on different tenants are never blocked by writes to an unrelated tenant.

State exposed to the API
------------------------
``SyncWorker`` exposes the following attributes for the health/metrics
endpoints:

* ``is_ready``              — True once ALL tenants have completed initial sync.
* ``last_sync_time``        — UTC datetime of the last completed full cycle.
* ``last_sync_row_count``   — Total rows touched across all tenants in last cycle.
* ``sync_lag_seconds``      — Duration (seconds) of the last full cycle.
* ``total_syncs``           — Cumulative number of completed cycles.
* ``startup_time``          — UTC datetime when the worker was started.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import duckdb

import aggregator.database.duckdb_store as store
from aggregator.config import settings
from aggregator.database.postgres import get_pg_connection, get_tenant_pg_connection

logger = logging.getLogger(__name__)

_INITIAL_SYNC_BATCH = 50_000   # rows per page during full initial sync (keyset)
_INCREMENTAL_LIMIT = 5_000     # max rows per incremental poll per tenant

# Canonical list of tenant IDs managed by this worker.
# Loaded from the ``tenants`` table at startup; see ``_discover_tenant_ids``.
_DEFAULT_TENANT_IDS: list[int] = list(range(1, 11))  # fallback: tenants 1–10


def _discover_tenant_ids() -> list[int]:
    """Query the first tenant's PostgreSQL database to discover all tenant IDs.

    Falls back to the hard-coded default (1–10) if the query fails, so the
    worker can still operate even when the tenants table is not yet populated.

    Returns:
        Sorted list of tenant IDs found in the database.
    """
    try:
        with get_pg_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM tenants ORDER BY id"
            ).fetchall()
            if rows:
                return [int(r[0]) for r in rows]
    except Exception as exc:
        logger.warning(
            "Could not discover tenant IDs from PostgreSQL (using defaults 1–10): %s", exc
        )
    return list(_DEFAULT_TENANT_IDS)


class SyncWorker:
    """Background daemon thread that replicates per-tenant PostgreSQL databases
    into per-tenant isolated DuckDB files.

    Args:
        interval: Polling interval in seconds (overrides settings default).

    Attributes:
        is_ready (bool):             True once ALL tenants have completed
                                     their initial sync.
        last_sync_time (datetime):   UTC timestamp of the last completed cycle.
        last_sync_row_count (int):   Rows upserted across all tenants in the
                                     last sync cycle.
        sync_lag_seconds (float):    Wall-clock duration of the last cycle.
        total_syncs (int):           Cumulative count of full sync cycles.
        startup_time (datetime):     UTC timestamp when ``start()`` was called.
    """

    def __init__(self, interval: int | None = None) -> None:
        """Initialise the SyncWorker with configuration only (no global DuckDB)."""
        self._interval = interval or settings.sync_interval_seconds
        self._stop_event = threading.Event()
        self._sync_lock = threading.Lock()
        self._force_full_sync = False
        self._thread: threading.Thread | None = None

        # Public state (read by API endpoints; written only by worker thread)
        self.is_ready: bool = False
        self.last_sync_time: datetime | None = None
        self.last_sync_row_count: int = 0
        self.sync_lag_seconds: float = 0.0
        self.total_syncs: int = 0
        self.startup_time: datetime | None = None

        # Discovered/configured tenant IDs — set on first run
        self._tenant_ids: list[int] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background sync thread.

        The thread is created as a *daemon* so that it does not prevent the
        Python interpreter from exiting when the main thread terminates.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("SyncWorker.start() called but thread already running.")
            return

        self.startup_time = datetime.now(tz=timezone.utc)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="SyncWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info("SyncWorker started (interval=%ds).", self._interval)

    def stop(self) -> None:
        """Signal the sync thread to stop and wait for it to finish.

        Blocks for up to ``interval + 2`` seconds.
        """
        logger.info("SyncWorker stopping…")
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 2)
        logger.info("SyncWorker stopped.")

    def trigger_rebuild(self) -> None:
        """Reset all per-tenant watermarks and mark the worker as not ready,
        forcing a full re-sync of every tenant database.

        Called by the ``POST /admin/rebuild`` endpoint.
        """
        with self._sync_lock:
            logger.info("Rebuild requested — scheduling atomic tenant replica replacement.")
            self.is_ready = False
            self._force_full_sync = True
            self.last_sync_time = None
            self.last_sync_row_count = 0
            self.total_syncs = 0

    # ------------------------------------------------------------------
    # Internal run loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Main loop executed in the background thread.

        On the first iteration, discovers all tenant IDs and performs the
        initial full per-tenant sync.  Subsequent iterations run incremental
        polling across all tenants.
        """
        first_run = True
        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                if first_run or self._force_full_sync:
                    self._tenant_ids = _discover_tenant_ids()
                    logger.info(
                        "Discovered %d tenant(s): %s",
                        len(self._tenant_ids),
                        self._tenant_ids,
                    )
                    with self._sync_lock:
                        completed = self._initial_full_sync_all(force=self._force_full_sync)
                    if completed:
                        first_run = False
                        self._force_full_sync = False
                else:
                    with self._sync_lock:
                        self._incremental_sync_all()
            except Exception as exc:
                logger.exception("Sync cycle failed: %s", exc)

            self.sync_lag_seconds = time.monotonic() - cycle_start
            self._stop_event.wait(timeout=self._interval)

    # ------------------------------------------------------------------
    # Initial full sync (all tenants)
    # ------------------------------------------------------------------

    def _initial_full_sync_all(self, force: bool = False) -> bool:
        """Perform a per-tenant full sync for every tenant that lacks a watermark.

        For each tenant:
        1. Check if ``tenant_{id}.duckdb`` already has a watermark.
        2. If not, pull all products, orders, and order_items from
           ``cyberforge_tenant_{id}`` and write directly into that tenant's
           isolated DuckDB file.
        3. Recompute ``hourly_tenant_metrics`` in the tenant's DuckDB.
        """
        total_rows = 0
        all_ready = True

        for t_id in self._tenant_ids:
            try:
                rows = self._initial_full_sync_tenant(t_id, force=force)
                total_rows += rows
            except Exception as exc:
                logger.error(
                    "Initial sync failed for tenant #%d: %s — skipping (will retry).", t_id, exc
                )
                all_ready = False

        if all_ready:
            self.is_ready = True
            self.last_sync_time = datetime.now(tz=timezone.utc)
            self.last_sync_row_count = total_rows
            self.total_syncs += 1
            logger.info(
                "Initial full sync complete across %d tenants (%d total rows).",
                len(self._tenant_ids),
                total_rows,
            )
            return True
        else:
            # Keep the API unavailable until every tenant has a complete
            # initial replica. Failed tenants are retried on the next cycle.
            self.is_ready = False
            logger.warning(
                "Initial sync finished with tenant failures; API remains unavailable "
                "until every tenant replica is complete."
            )
            return False

    def _initial_full_sync_tenant(self, tenant_id: int, force: bool = False) -> int:
        """Perform a full sync for a single tenant.

        Returns:
            Total number of rows synced into this tenant's DuckDB.
        """
        t_lock = store.get_tenant_write_lock(tenant_id)
        live_conn = None
        if not force:
            try:
                live_conn = store.get_tenant_duckdb(tenant_id)
                watermark = store.get_last_sync_time(live_conn, "orders", t_lock)
                with get_tenant_pg_connection(tenant_id) as source:
                    expected_orders, expected_items = source.execute(
                        "SELECT (SELECT COUNT(*) FROM orders), "
                        "(SELECT COUNT(*) FROM order_items)"
                    ).fetchone()
                with t_lock:
                    actual_orders, actual_items, rollup_orders = live_conn.execute(
                        "SELECT "
                        "(SELECT COUNT(*) FROM orders), "
                        "(SELECT COUNT(*) FROM order_items), "
                        "(SELECT COALESCE(SUM(order_count), 0) FROM hourly_tenant_metrics)"
                    ).fetchone()
                if watermark is not None and actual_orders > 0:
                    if (
                        rollup_orders == expected_orders
                        and actual_orders == expected_orders
                        and actual_items == expected_items
                    ):
                        logger.info(
                            "Tenant #%d already has a validated replica (%s) — "
                            "skipping full sync.",
                            tenant_id,
                            watermark,
                        )
                        return 0
                    else:
                        # Replica exists and has data, but Postgres has newer rows (e.g. simulated orders)
                        logger.info(
                            "Tenant #%d has replica with watermark (%s); syncing delta incrementally.",
                            tenant_id,
                            watermark,
                        )
                        delta = self._incremental_sync_tenant(tenant_id)
                        return delta
                logger.warning(
                    "Tenant #%d replica is incomplete; rebuilding atomically.",
                    tenant_id,
                )
            except Exception as exc:
                logger.warning(
                    "Tenant #%d replica validation failed; rebuilding atomically: %s",
                    tenant_id,
                    exc,
                )

        staging_conn, staging_path = store.create_tenant_staging(tenant_id)
        staging_lock = t_lock
        try:
            logger.info("Starting atomic initial full sync for tenant #%d…", tenant_id)
            total = 0
            total += self._sync_products_full_tenant(tenant_id, staging_conn, staging_lock)
            total += self._sync_orders_full_tenant(tenant_id, staging_conn, staging_lock)
            total += self._sync_order_items_full_tenant(tenant_id, staging_conn, staging_lock)
            store.recompute_hourly_metrics(staging_conn, staging_lock)

            with get_tenant_pg_connection(tenant_id) as source:
                expected_orders, expected_items = source.execute(
                    "SELECT (SELECT COUNT(*) FROM orders), "
                    "(SELECT COUNT(*) FROM order_items)"
                ).fetchone()
            with staging_lock:
                actual_orders, actual_items, rollup_orders = staging_conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM orders), "
                    "(SELECT COUNT(*) FROM order_items), "
                    "(SELECT COALESCE(SUM(order_count), 0) FROM hourly_tenant_metrics)"
                ).fetchone()
            if (actual_orders, actual_items, rollup_orders) != (
                expected_orders,
                expected_items,
                expected_orders,
            ):
                raise RuntimeError(
                    f"tenant #{tenant_id} validation failed: "
                    f"source=({expected_orders}, {expected_items}), "
                    f"replica=({actual_orders}, {actual_items}), "
                    f"rollup={rollup_orders}"
                )

            store.promote_tenant_staging(tenant_id, staging_conn, staging_path)
            logger.info(
                "Atomic initial full sync complete for tenant #%d (%d rows).",
                tenant_id,
                total,
            )
            return total
        except Exception:
            try:
                staging_conn.close()
            finally:
                staging_path.unlink(missing_ok=True)
            raise

    def _sync_products_full_tenant(
        self,
        tenant_id: int,
        t_conn: duckdb.DuckDBPyConnection,
        t_lock: threading.Lock,
    ) -> int:
        """Fetch all products from tenant's PostgreSQL and upsert into its DuckDB.

        Returns:
            Number of product rows synced.
        """
        logger.info("Tenant #%d: Syncing all products…", tenant_id)
        with get_tenant_pg_connection(tenant_id) as conn:
            rows: list[Any] = conn.execute(
                "SELECT id, tenant_id, name, category, price, created_at FROM products"
            ).fetchall()

        count = store.upsert_products(t_conn, rows, t_lock)
        logger.info("Tenant #%d: Products synced: %d rows.", tenant_id, count)
        return count

    def _sync_orders_full_tenant(
        self,
        tenant_id: int,
        t_conn: duckdb.DuckDBPyConnection,
        t_lock: threading.Lock,
    ) -> int:
        """Paginate all orders from tenant's PostgreSQL into its isolated DuckDB.

        Returns:
            Total number of order rows synced.
        """
        logger.info(
            "Tenant #%d: Syncing all orders (batch_size=%d, keyset)…",
            tenant_id,
            _INITIAL_SYNC_BATCH,
        )
        last_id = 0
        total = 0
        max_updated_at: datetime | None = None

        while True:
            with get_tenant_pg_connection(tenant_id) as conn:
                rows = conn.execute(
                    """
                    SELECT id, tenant_id, customer_id, status,
                           total_amount, created_at, updated_at
                    FROM orders
                    WHERE id > %s
                    ORDER BY id
                    LIMIT %s
                    """,
                    [last_id, _INITIAL_SYNC_BATCH],
                ).fetchall()

            if not rows:
                break

            count = store.upsert_orders(t_conn, rows, t_lock)
            total += count
            last_id = rows[-1][0]

            for row in rows:
                row_updated_at = row[6]
                if isinstance(row_updated_at, datetime):
                    if row_updated_at.tzinfo is None:
                        row_updated_at = row_updated_at.replace(tzinfo=timezone.utc)
                    if max_updated_at is None or row_updated_at > max_updated_at:
                        max_updated_at = row_updated_at

            logger.info(
                "Tenant #%d: Orders synced so far: %d (last_id=%d)",
                tenant_id, total, last_id,
            )

            if len(rows) < _INITIAL_SYNC_BATCH:
                break

        if max_updated_at is not None:
            store.set_last_sync_time(t_conn, "orders", max_updated_at, t_lock)

        logger.info("Tenant #%d: Full orders sync complete: %d rows.", tenant_id, total)
        return total

    def _sync_order_items_full_tenant(
        self,
        tenant_id: int,
        t_conn: duckdb.DuckDBPyConnection,
        t_lock: threading.Lock,
    ) -> int:
        """Paginate all order_items from tenant's PostgreSQL into its isolated DuckDB.

        Returns:
            Total number of order_item rows synced.
        """
        logger.info(
            "Tenant #%d: Syncing all order_items (batch_size=%d, keyset)…",
            tenant_id,
            _INITIAL_SYNC_BATCH,
        )
        last_id = 0
        total = 0

        while True:
            with get_tenant_pg_connection(tenant_id) as conn:
                rows = conn.execute(
                    """
                    SELECT id, order_id, tenant_id, product_id,
                           quantity, unit_price, total_price, created_at
                    FROM order_items
                    WHERE id > %s
                    ORDER BY id
                    LIMIT %s
                    """,
                    [last_id, _INITIAL_SYNC_BATCH],
                ).fetchall()

            if not rows:
                break

            count = store.upsert_order_items(t_conn, rows, t_lock)
            total += count
            last_id = rows[-1][0]

            logger.info(
                "Tenant #%d: Order items synced so far: %d (last_id=%d)",
                tenant_id, total, last_id,
            )

            if len(rows) < _INITIAL_SYNC_BATCH:
                break

        logger.info(
            "Tenant #%d: Full order_items sync complete: %d rows.", tenant_id, total
        )
        return total

    # ------------------------------------------------------------------
    # Incremental sync (all tenants)
    # ------------------------------------------------------------------

    def _incremental_sync_all(self) -> None:
        """Poll all tenants for new/changed rows and upsert them into their
        respective isolated DuckDB files.

        Each tenant is polled independently; a failure on one tenant does not
        block the others.
        """
        total = 0
        all_succeeded = True
        for t_id in self._tenant_ids:
            try:
                total += self._incremental_sync_tenant(t_id)
            except Exception as exc:
                all_succeeded = False
                logger.warning(
                    "Incremental sync failed for tenant #%d: %s — will retry.", t_id, exc
                )

        self.last_sync_time = datetime.now(tz=timezone.utc)
        self.last_sync_row_count = total
        self.total_syncs += 1
        if not self.is_ready and all_succeeded:
            self.is_ready = True
            logger.info(
                "All tenant replicas completed successfully; API is ready."
            )
        logger.debug("Incremental sync complete across %d tenants (%d rows).",
                     len(self._tenant_ids), total)

    def _incremental_sync_tenant(self, tenant_id: int) -> int:
        """Poll a single tenant's PostgreSQL for new orders and sync into DuckDB.

        Steps:
        1. Read this tenant's watermark from its DuckDB.
        2. Fetch orders from ``cyberforge_tenant_{id}`` WHERE updated_at > watermark.
        3. Upsert into ``tenant_{id}.duckdb``.
        4. Fetch and upsert associated order_items.
        5. Recompute ``hourly_tenant_metrics`` for this tenant.
        6. Advance the watermark.

        Returns:
            Number of rows upserted for this tenant.
        """
        t_conn = store.get_tenant_duckdb(tenant_id)
        t_lock = store.get_tenant_write_lock(tenant_id)

        watermark = store.get_last_sync_time(t_conn, "orders", t_lock)
        if watermark is None:
            # Watermark missing — fall back to full sync for this tenant
            logger.warning(
                "Tenant #%d: watermark missing in incremental sync; re-running full sync.",
                tenant_id,
            )
            return self._initial_full_sync_tenant(tenant_id)

        with get_tenant_pg_connection(tenant_id) as conn:
            rows = conn.execute(
                """
                SELECT id, tenant_id, customer_id, status,
                       total_amount, created_at, updated_at
                FROM orders
                WHERE updated_at > %s
                ORDER BY updated_at ASC
                LIMIT %s
                """,
                [watermark, _INCREMENTAL_LIMIT],
            ).fetchall()

        if not rows:
            logger.debug(
                "Tenant #%d: no new rows since %s.", tenant_id, watermark
            )
            return 0

        # Upsert orders into tenant's isolated DuckDB
        count = store.upsert_orders(t_conn, rows, t_lock)
        total = count

        # Fetch and upsert order_items for changed orders
        changed_ids = [row[0] for row in rows]
        total += self._sync_items_for_orders_tenant(tenant_id, t_conn, t_lock, changed_ids)

        # Recompute privacy-safe hourly rollup in this tenant's DuckDB
        store.recompute_hourly_metrics(t_conn, t_lock)

        # Advance watermark
        max_updated_at: datetime | None = None
        for row in rows:
            row_updated_at = row[6]
            if isinstance(row_updated_at, datetime):
                if row_updated_at.tzinfo is None:
                    row_updated_at = row_updated_at.replace(tzinfo=timezone.utc)
                if max_updated_at is None or row_updated_at > max_updated_at:
                    max_updated_at = row_updated_at

        if max_updated_at is not None:
            store.set_last_sync_time(t_conn, "orders", max_updated_at, t_lock)

        logger.info(
            "Tenant #%d: Incremental sync — %d orders, watermark → %s.",
            tenant_id, count, max_updated_at,
        )
        return total

    def _sync_items_for_orders_tenant(
        self,
        tenant_id: int,
        t_conn: duckdb.DuckDBPyConnection,
        t_lock: threading.Lock,
        order_ids: list[int],
    ) -> int:
        """Fetch and upsert order_items from the tenant's PostgreSQL into its DuckDB.

        Args:
            tenant_id: The tenant whose PostgreSQL and DuckDB to use.
            t_conn:    Open connection to this tenant's DuckDB.
            t_lock:    Write lock for this tenant's DuckDB.
            order_ids: IDs of orders whose items need to be refreshed.

        Returns:
            Number of order_item rows upserted.
        """
        if not order_ids:
            return 0
        placeholders = ",".join(["%s"] * len(order_ids))
        with get_tenant_pg_connection(tenant_id) as conn:
            rows = conn.execute(
                f"""
                SELECT id, order_id, tenant_id, product_id,
                       quantity, unit_price, total_price, created_at
                FROM order_items
                WHERE order_id IN ({placeholders})
                """,
                order_ids,
            ).fetchall()
        return store.upsert_order_items(t_conn, rows, t_lock)

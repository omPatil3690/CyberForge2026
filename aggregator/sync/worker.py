"""
aggregator/sync/worker.py — Incremental watermark-based sync worker.

The SyncWorker runs as a background daemon thread and continuously polls
PostgreSQL for new/updated rows, upserting them into the DuckDB OLAP replica.

Sync strategy
-------------
* **Orders (incremental)**: Poll ``orders WHERE updated_at > watermark``
  in batches of up to 5 000 rows.  After each batch the watermark is
  advanced to ``max(updated_at)`` of the fetched rows.
* **Order items (cascaded)**: For every changed order_id, fetch *all*
  associated ``order_items`` rows and upsert them.  This keeps item data
  consistent even when a single item within an order is updated.
* **Products (full sync once)**: Products change rarely; a full sync is
  performed once on startup and is not repeated unless a rebuild is
  requested.
* **Initial full sync**: When DuckDB is empty (first run), paginated
  ``LIMIT / OFFSET`` batches of 10 000 rows walk the entire ``orders``
  table before incremental polling begins.

Threading
---------
The worker acquires ``DUCKDB_WRITE_LOCK`` indirectly through the
``duckdb_store`` helper functions; it must NOT attempt to acquire the lock
itself to avoid deadlocks.

State exposed to the API
------------------------
``SyncWorker`` exposes the following attributes for the health/metrics
endpoints:

* ``is_ready``              — True once the initial sync is complete.
* ``last_sync_time``        — UTC datetime of the last completed sync.
* ``last_sync_row_count``   — Rows touched in the last sync cycle.
* ``sync_lag_seconds``      — Duration (seconds) of the last cycle.
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
from aggregator.database.postgres import get_pg_connection

logger = logging.getLogger(__name__)

_INITIAL_SYNC_BATCH = 10_000   # rows per page during full initial sync
_INCREMENTAL_LIMIT = 5_000     # max rows per incremental poll


class SyncWorker:
    """Background daemon thread that replicates PostgreSQL data into DuckDB.

    Args:
        duckdb_conn: The shared DuckDB connection returned by ``init_duckdb()``.
        interval:    Polling interval in seconds (overrides settings default).

    Attributes:
        is_ready (bool):             True once the initial sync has finished.
        last_sync_time (datetime):   UTC timestamp of the last completed sync.
        last_sync_row_count (int):   Rows upserted in the last sync cycle.
        sync_lag_seconds (float):    Wall-clock duration of the last sync cycle.
        total_syncs (int):           Cumulative count of sync cycles completed.
        startup_time (datetime):     UTC timestamp when ``start()`` was called.
    """

    def __init__(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        interval: int | None = None,
    ) -> None:
        """Initialise the SyncWorker with a DuckDB connection and config."""
        self._conn = duckdb_conn
        self._interval = interval or settings.sync_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Public state (read by API endpoints; written only by worker thread)
        self.is_ready: bool = False
        self.last_sync_time: datetime | None = None
        self.last_sync_row_count: int = 0
        self.sync_lag_seconds: float = 0.0
        self.total_syncs: int = 0
        self.startup_time: datetime | None = None

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
        """Reset watermarks and mark the worker as not ready, forcing a full re-sync.

        Called by the ``POST /admin/rebuild`` endpoint.  The next iteration of
        the worker loop will detect the cleared watermark and execute a full
        paginated sync.
        """
        logger.info("Rebuild triggered — clearing DuckDB replica and watermarks.")
        store.truncate_all(self._conn)
        self.is_ready = False
        self.last_sync_time = None
        self.last_sync_row_count = 0
        self.total_syncs = 0

    # ------------------------------------------------------------------
    # Internal run loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Main loop executed in the background thread.

        Performs the initial sync on the first iteration, then switches to
        incremental polling at the configured interval.
        """
        first_run = True
        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                if first_run:
                    self._initial_full_sync()
                    first_run = False
                else:
                    self._incremental_sync()
            except Exception as exc:
                logger.exception("Sync cycle failed: %s", exc)

            self.sync_lag_seconds = time.monotonic() - cycle_start

            # Wait for the next cycle (or bail out if stopped)
            self._stop_event.wait(timeout=self._interval)

    # ------------------------------------------------------------------
    # Initial full sync
    # ------------------------------------------------------------------

    def _initial_full_sync(self) -> None:
        """Perform a paginated full sync of *all* PostgreSQL data into DuckDB.

        This runs once when DuckDB is empty (no watermark found).  It syncs:
        1. All ``products`` rows (one shot — they fit in memory easily).
        2. All ``orders`` rows in batches of ``_INITIAL_SYNC_BATCH``.
        3. All ``order_items`` rows in batches of ``_INITIAL_SYNC_BATCH``.
        """
        watermark = store.get_last_sync_time(self._conn, "orders")
        if watermark is not None:
            # DuckDB already has data — skip full sync and go incremental
            logger.info(
                "DuckDB has existing data (watermark=%s). Skipping full sync.",
                watermark,
            )
            self.is_ready = True
            return

        logger.info("Starting initial full sync from PostgreSQL…")
        total_rows = 0

        # --- Products (full, single batch) ---
        total_rows += self._sync_products_full()

        # --- Orders (paginated) ---
        total_rows += self._sync_orders_full()

        # --- Order items (paginated) ---
        total_rows += self._sync_order_items_full()

        self.is_ready = True
        self.last_sync_time = datetime.now(tz=timezone.utc)
        self.last_sync_row_count = total_rows
        self.total_syncs += 1
        logger.info("Initial full sync complete (%d rows).", total_rows)

    def _sync_products_full(self) -> int:
        """Fetch and upsert all products from PostgreSQL.

        Returns:
            Number of product rows synced.
        """
        logger.info("Syncing all products…")
        with get_pg_connection() as conn:
            rows: list[Any] = conn.execute(
                "SELECT id, tenant_id, name, category, price, created_at FROM products"
            ).fetchall()

        count = store.upsert_products(self._conn, rows)
        logger.info("Products synced: %d rows.", count)
        return count

    def _sync_orders_full(self) -> int:
        """Paginate through all orders and upsert into DuckDB.

        Also records the watermark as ``max(updated_at)`` of all fetched rows.

        Returns:
            Total number of order rows synced.
        """
        logger.info("Syncing all orders (batch_size=%d)…", _INITIAL_SYNC_BATCH)
        offset = 0
        total = 0
        max_updated_at: datetime | None = None

        while True:
            with get_pg_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, tenant_id, customer_id, status,
                           total_amount, created_at, updated_at
                    FROM orders
                    ORDER BY id
                    LIMIT %s OFFSET %s
                    """,
                    [_INITIAL_SYNC_BATCH, offset],
                ).fetchall()

            if not rows:
                break

            count = store.upsert_orders(self._conn, rows)
            total += count
            offset += len(rows)

            # Track the highest updated_at seen so far
            for row in rows:
                row_updated_at = row[6]  # updated_at is index 6
                if isinstance(row_updated_at, datetime):
                    if row_updated_at.tzinfo is None:
                        row_updated_at = row_updated_at.replace(tzinfo=timezone.utc)
                    if max_updated_at is None or row_updated_at > max_updated_at:
                        max_updated_at = row_updated_at

            logger.info("Orders synced so far: %d (offset=%d)", total, offset)

            if len(rows) < _INITIAL_SYNC_BATCH:
                break  # Last page

        if max_updated_at is not None:
            store.set_last_sync_time(self._conn, "orders", max_updated_at)

        logger.info("Full orders sync complete: %d rows.", total)
        return total

    def _sync_order_items_full(self) -> int:
        """Paginate through all order_items and upsert into DuckDB.

        Returns:
            Total number of order_item rows synced.
        """
        logger.info("Syncing all order_items (batch_size=%d)…", _INITIAL_SYNC_BATCH)
        offset = 0
        total = 0

        while True:
            with get_pg_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, order_id, tenant_id, product_id,
                           quantity, unit_price, total_price, created_at
                    FROM order_items
                    ORDER BY id
                    LIMIT %s OFFSET %s
                    """,
                    [_INITIAL_SYNC_BATCH, offset],
                ).fetchall()

            if not rows:
                break

            count = store.upsert_order_items(self._conn, rows)
            total += count
            offset += len(rows)

            logger.info("Order items synced so far: %d (offset=%d)", total, offset)

            if len(rows) < _INITIAL_SYNC_BATCH:
                break

        logger.info("Full order_items sync complete: %d rows.", total)
        return total

    # ------------------------------------------------------------------
    # Incremental sync
    # ------------------------------------------------------------------

    def _incremental_sync(self) -> None:
        """Poll PostgreSQL for rows newer than the current watermark and upsert them.

        Steps:
        1. Read ``orders WHERE updated_at > watermark LIMIT 5000``.
        2. Upsert fetched orders into DuckDB.
        3. For changed order IDs, fetch and upsert associated ``order_items``.
        4. Advance the watermark to ``max(updated_at)`` of the fetched batch.
        """
        watermark = store.get_last_sync_time(self._conn, "orders")
        if watermark is None:
            # Somehow we lost the watermark — fall back to full sync
            logger.warning("Watermark missing in incremental sync; re-running full sync.")
            self._initial_full_sync()
            return

        with get_pg_connection() as conn:
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
            logger.debug("Incremental sync: no new rows since %s.", watermark)
            self.last_sync_row_count = 0
            self.last_sync_time = datetime.now(tz=timezone.utc)
            self.total_syncs += 1
            return

        # Upsert orders
        count = store.upsert_orders(self._conn, rows)
        total = count

        # Fetch and upsert order_items for changed orders
        changed_ids = [row[0] for row in rows]
        total += self._sync_items_for_orders(changed_ids)

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
            store.set_last_sync_time(self._conn, "orders", max_updated_at)

        self.last_sync_time = datetime.now(tz=timezone.utc)
        self.last_sync_row_count = total
        self.total_syncs += 1
        logger.info(
            "Incremental sync complete: %d order rows, watermark → %s.",
            count,
            max_updated_at,
        )

    def _sync_items_for_orders(self, order_ids: list[int]) -> int:
        """Fetch and upsert all order_items belonging to the given order IDs.

        Args:
            order_ids: List of order primary keys whose items need to be synced.

        Returns:
            Number of order_item rows upserted.
        """
        if not order_ids:
            return 0

        placeholders = ",".join(["%s"] * len(order_ids))
        with get_pg_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, order_id, tenant_id, product_id,
                       quantity, unit_price, total_price, created_at
                FROM order_items
                WHERE order_id IN ({placeholders})
                """,
                order_ids,
            ).fetchall()

        return store.upsert_order_items(self._conn, rows)

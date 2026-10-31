"""
aggregator/database/duckdb_store.py — DuckDB in-process OLAP replica for CyberForge.

DuckDB serves as the fast read layer for all dashboard queries.  Data is
synced incrementally from PostgreSQL by the SyncWorker; this module owns all
schema creation, upsert logic, and watermark management.

Threading model
---------------
DuckDB has a *single-writer* constraint: only one thread may hold a write
transaction at a time.  All writes (upserts, watermark updates) must be
serialised through ``DUCKDB_WRITE_LOCK``.  Reads also acquire the same lock
to avoid races with in-flight writes (DuckDB's MVCC is not exposed through
the Python driver).

Usage::

    import aggregator.database.duckdb_store as store

    conn = store.init_duckdb()            # call once at startup
    store.upsert_orders(conn, rows)       # called by sync worker
    store.set_last_sync_time(conn, "orders", datetime.utcnow())
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Sequence

import duckdb

from aggregator.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global write lock — ALL callers must acquire this before touching DuckDB
# ---------------------------------------------------------------------------

DUCKDB_WRITE_LOCK: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# DDL: replica schema
# ---------------------------------------------------------------------------

_DDL_ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER NOT NULL PRIMARY KEY,
    tenant_id    INTEGER NOT NULL,
    customer_id  INTEGER NOT NULL,
    status       VARCHAR(50) NOT NULL,
    total_amount DOUBLE NOT NULL DEFAULT 0.0,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);
"""

_DDL_ORDER_ITEMS = """
CREATE TABLE IF NOT EXISTS order_items (
    id          INTEGER NOT NULL PRIMARY KEY,
    order_id    INTEGER NOT NULL,
    tenant_id   INTEGER NOT NULL,
    product_id  INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    unit_price  DOUBLE NOT NULL,
    total_price DOUBLE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);
"""

_DDL_PRODUCTS = """
CREATE TABLE IF NOT EXISTS products (
    id         INTEGER NOT NULL PRIMARY KEY,
    tenant_id  INTEGER NOT NULL,
    name       VARCHAR(255) NOT NULL,
    category   VARCHAR(100) NOT NULL,
    price      DOUBLE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
"""

_DDL_SYNC_STATE = """
CREATE TABLE IF NOT EXISTS sync_state (
    table_name     VARCHAR(100) NOT NULL PRIMARY KEY,
    last_synced_at TIMESTAMPTZ
);
"""

# Covering indexes to accelerate the dashboard queries (tenant-scoped ranges)
_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_orders_tenant_created
    ON orders (tenant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_orders_tenant_status
    ON orders (tenant_id, status);

CREATE INDEX IF NOT EXISTS idx_order_items_tenant_created
    ON order_items (tenant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_order_items_tenant_product
    ON order_items (tenant_id, product_id);

CREATE INDEX IF NOT EXISTS idx_products_tenant
    ON products (tenant_id);
"""

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_duckdb() -> duckdb.DuckDBPyConnection:
    """Open (or create) the DuckDB file and initialise the replica schema.

    Creates all tables and indexes if they do not already exist.  Safe to
    call repeatedly — all DDL is idempotent (``IF NOT EXISTS``).

    Returns:
        An open ``duckdb.DuckDBPyConnection`` in ``read_write`` mode.
        The caller should treat this as a process-lifetime singleton.
    """
    path = settings.duckdb_path
    logger.info("Opening DuckDB replica at %r", path)

    conn = duckdb.connect(database=path, read_only=False)

    with DUCKDB_WRITE_LOCK:
        conn.execute(_DDL_ORDERS)
        conn.execute(_DDL_ORDER_ITEMS)
        conn.execute(_DDL_PRODUCTS)
        conn.execute(_DDL_SYNC_STATE)
        conn.execute(_DDL_INDEXES)

    logger.info("DuckDB schema initialised successfully.")
    return conn


# ---------------------------------------------------------------------------
# Watermark helpers
# ---------------------------------------------------------------------------

def get_last_sync_time(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
) -> datetime | None:
    """Return the last watermark timestamp recorded for *table_name*.

    Args:
        conn:       Open DuckDB connection.
        table_name: One of ``"orders"``, ``"order_items"``, ``"products"``.

    Returns:
        A timezone-aware ``datetime`` (UTC) or ``None`` if the table has never
        been synced (i.e. this is the first run).
    """
    with DUCKDB_WRITE_LOCK:
        row = conn.execute(
            "SELECT last_synced_at FROM sync_state WHERE table_name = ?",
            [table_name],
        ).fetchone()

    if row is None or row[0] is None:
        return None

    ts = row[0]
    # DuckDB returns timezone-aware datetimes for TIMESTAMPTZ columns
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def set_last_sync_time(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    ts: datetime,
) -> None:
    """Persist the sync watermark for *table_name*.

    Uses an INSERT OR REPLACE (upsert) so the first call creates the row and
    subsequent calls update it.

    Args:
        conn:       Open DuckDB connection.
        table_name: Name of the synced PostgreSQL table.
        ts:         The new watermark (typically ``max(updated_at)`` of the
                    most recent batch).
    """
    with DUCKDB_WRITE_LOCK:
        conn.execute(
            """
            INSERT OR REPLACE INTO sync_state (table_name, last_synced_at)
            VALUES (?, ?)
            """,
            [table_name, ts],
        )


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

def _rows_to_tuples(rows: Sequence[Any]) -> list[tuple]:
    """Normalise rows to plain tuples for DuckDB parameter binding.

    psycopg returns ``psycopg.rows.TupleRow`` objects which DuckDB handles
    fine, but being explicit avoids any future driver quirks.

    Args:
        rows: Sequence of row objects (tuples, namedtuples, or mappings).

    Returns:
        List of plain Python tuples.
    """
    result = []
    for r in rows:
        if isinstance(r, (list, tuple)):
            result.append(tuple(r))
        elif hasattr(r, "_asdict"):
            result.append(tuple(r._asdict().values()))
        elif hasattr(r, "__iter__"):
            result.append(tuple(r))
        else:
            result.append((r,))
    return result


def upsert_orders(
    conn: duckdb.DuckDBPyConnection,
    rows: Sequence[Any],
) -> int:
    """Upsert a batch of order rows into the DuckDB ``orders`` table.

    Columns expected (in order, matching PostgreSQL schema):
        id, tenant_id, customer_id, status, total_amount, created_at, updated_at

    Strategy: DELETE rows whose ``id`` appears in the batch, then INSERT all
    batch rows.  This is equivalent to ``INSERT OR REPLACE`` and avoids any
    DuckDB ``ON CONFLICT`` limitations.

    Args:
        conn: Open DuckDB connection.
        rows: Sequence of 7-element iterables matching the column order above.

    Returns:
        Number of rows upserted.
    """
    if not rows:
        return 0

    tuples = _rows_to_tuples(rows)
    ids = [t[0] for t in tuples]

    with DUCKDB_WRITE_LOCK:
        # Remove stale versions
        conn.execute(
            f"DELETE FROM orders WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        # Insert fresh batch
        conn.executemany(
            """
            INSERT INTO orders
                (id, tenant_id, customer_id, status, total_amount, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            tuples,
        )

    logger.debug("Upserted %d order rows into DuckDB.", len(tuples))
    return len(tuples)


def upsert_order_items(
    conn: duckdb.DuckDBPyConnection,
    rows: Sequence[Any],
) -> int:
    """Upsert a batch of order_item rows into the DuckDB ``order_items`` table.

    Columns expected (in order, matching PostgreSQL schema):
        id, order_id, tenant_id, product_id, quantity, unit_price, total_price, created_at

    Args:
        conn: Open DuckDB connection.
        rows: Sequence of 8-element iterables matching the column order above.

    Returns:
        Number of rows upserted.
    """
    if not rows:
        return 0

    tuples = _rows_to_tuples(rows)
    ids = [t[0] for t in tuples]

    with DUCKDB_WRITE_LOCK:
        conn.execute(
            f"DELETE FROM order_items WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        conn.executemany(
            """
            INSERT INTO order_items
                (id, order_id, tenant_id, product_id,
                 quantity, unit_price, total_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuples,
        )

    logger.debug("Upserted %d order_item rows into DuckDB.", len(tuples))
    return len(tuples)


def upsert_products(
    conn: duckdb.DuckDBPyConnection,
    rows: Sequence[Any],
) -> int:
    """Upsert a batch of product rows into the DuckDB ``products`` table.

    Columns expected (in order, matching PostgreSQL schema):
        id, tenant_id, name, category, price, created_at

    Args:
        conn: Open DuckDB connection.
        rows: Sequence of 6-element iterables matching the column order above.

    Returns:
        Number of rows upserted.
    """
    if not rows:
        return 0

    tuples = _rows_to_tuples(rows)
    ids = [t[0] for t in tuples]

    with DUCKDB_WRITE_LOCK:
        conn.execute(
            f"DELETE FROM products WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        conn.executemany(
            """
            INSERT INTO products
                (id, tenant_id, name, category, price, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            tuples,
        )

    logger.debug("Upserted %d product rows into DuckDB.", len(tuples))
    return len(tuples)


# ---------------------------------------------------------------------------
# Utility queries
# ---------------------------------------------------------------------------

def duckdb_order_count(conn: duckdb.DuckDBPyConnection) -> int:
    """Return the total number of rows in the DuckDB ``orders`` table.

    Args:
        conn: Open DuckDB connection.

    Returns:
        Row count (integer).
    """
    with DUCKDB_WRITE_LOCK:
        row = conn.execute("SELECT COUNT(*) FROM orders").fetchone()
    return int(row[0]) if row else 0


def truncate_all(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop all replica data and reset sync watermarks.

    Used by the admin ``/rebuild`` endpoint to trigger a full re-sync from
    scratch.

    Args:
        conn: Open DuckDB connection.
    """
    with DUCKDB_WRITE_LOCK:
        conn.execute("DELETE FROM order_items")
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM products")
        conn.execute("DELETE FROM sync_state")
    logger.info("DuckDB replica tables truncated for rebuild.")

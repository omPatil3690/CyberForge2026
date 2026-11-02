#!/usr/bin/env python3
"""Populate isolated tenant PostgreSQL databases directly in bounded batches.

The central ``cyberforge`` database is used only for company/tenant metadata
and population checkpoints. Operational rows are generated in their final
tenant database and are never staged in the central database.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from aggregator.config import settings
from generate_data import (  # noqa: E402
    CATEGORIES,
    EMAIL_DOMAINS,
    FIRST_NAMES,
    LAST_NAMES,
    ORDER_STATUSES,
    ORDER_STATUS_WEIGHTS,
    PRODUCT_NOUNS,
    PRODUCT_PREFIXES,
    generate_product_popularity_weights,
    generate_realistic_timestamp,
)

POPULATION_TABLE = """
CREATE TABLE IF NOT EXISTS tenant_population_status (
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    dataset_version VARCHAR(160) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    expected_orders BIGINT NOT NULL,
    expected_order_items BIGINT NOT NULL DEFAULT 0,
    loaded_orders BIGINT NOT NULL DEFAULT 0,
    loaded_order_items BIGINT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT
)
"""

TENANT_SCHEMA = """
DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    price NUMERIC(10, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status VARCHAR(50) NOT NULL CHECK (status IN ('placed', 'shipped', 'delivered', 'cancelled')),
    total_amount NUMERIC(12, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE order_items (
    id BIGINT PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    tenant_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(10, 2) NOT NULL,
    total_price NUMERIC(12, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
"""

SECONDARY_INDEXES = """
CREATE INDEX idx_customers_tenant ON customers(tenant_id);
CREATE INDEX idx_products_tenant ON products(tenant_id);
CREATE INDEX idx_products_tenant_category ON products(tenant_id, category);
CREATE INDEX idx_orders_tenant_created ON orders(tenant_id, created_at);
CREATE INDEX idx_orders_tenant_status ON orders(tenant_id, status);
CREATE INDEX idx_orders_updated_id ON orders(updated_at, id);
CREATE INDEX idx_order_items_tenant_product ON order_items(tenant_id, product_id);
CREATE INDEX idx_order_items_order_id ON order_items(order_id);
CREATE INDEX idx_order_items_tenant_created ON order_items(tenant_id, created_at);
"""


def connection_info(dbname: str) -> str:
    return make_conninfo(
        dbname=dbname,
        user=settings.db_user,
        password=settings.db_password or None,
        host=settings.db_host,
        port=settings.db_port,
    )


def copy_rows(conn: psycopg.Connection, table: str, columns: str, rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        with cur.copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)


def allocate_orders(total: int, tenants: int) -> list[int]:
    weights = [1.0 / math.pow(index, 1.15) for index in range(1, tenants + 1)]
    scale = total / sum(weights)
    counts = [int(weight * scale) for weight in weights]
    for index in range(total - sum(counts)):
        counts[index % tenants] += 1
    return counts


def ensure_metadata(tenant_count: int) -> None:
    with psycopg.connect(connection_info(settings.db_name)) as conn:
        conn.execute(
            "INSERT INTO companies (id, name) VALUES (1, 'Global Retail Corp') "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name"
        )
        for tenant_id in range(1, tenant_count + 1):
            conn.execute(
                """
                INSERT INTO tenants (id, name, company_id)
                VALUES (%s, %s, 1)
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name, company_id = EXCLUDED.company_id
                """,
                [tenant_id, f"Store-{tenant_id:03d}"],
            )
        conn.execute(POPULATION_TABLE)


def tenant_database_exists(tenant_id: int) -> bool:
    with psycopg.connect(connection_info("postgres")) as conn:
        return conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
            [f"cyberforge_tenant_{tenant_id}"],
        ).fetchone()[0]


def ensure_tenant_database(tenant_id: int) -> None:
    dbname = f"cyberforge_tenant_{tenant_id}"
    with psycopg.connect(connection_info("postgres"), autocommit=True) as conn:
        if not conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)", [dbname]
        ).fetchone()[0]:
            conn.execute(f'CREATE DATABASE "{dbname}"')


def reset_tenant(tenant_id: int) -> None:
    ensure_tenant_database(tenant_id)
    with psycopg.connect(connection_info(f"cyberforge_tenant_{tenant_id}")) as conn:
        conn.execute(TENANT_SCHEMA)
        conn.commit()


def populate_tenant(
    tenant_id: int,
    order_count: int,
    args: argparse.Namespace,
    generation_now: datetime,
    dataset_version: str,
) -> tuple[int, int]:
    reset_tenant(tenant_id)
    rng = random.Random(args.seed + tenant_id)
    start_date = generation_now - timedelta(days=args.days)
    products: list[tuple] = []
    product_lookup: list[tuple[int, float]] = []
    for product_id in range(1, args.products_per_tenant + 1):
        category = rng.choice(CATEGORIES)
        name = f"{rng.choice(PRODUCT_PREFIXES[category])} {rng.choice(PRODUCT_NOUNS[category])} #{product_id}"
        price = round(rng.uniform(8.0, 399.0), 2)
        created_at = start_date - timedelta(days=rng.randint(1, 30))
        products.append((product_id, tenant_id, name, category, price, created_at))
        product_lookup.append((product_id, price))

    customers: list[tuple] = []
    for customer_id in range(1, args.customers_per_tenant + 1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        customers.append(
            (
                customer_id,
                tenant_id,
                f"{first} {last}",
                f"{first.lower()}.{last.lower()}_{tenant_id}_{customer_id}@{rng.choice(EMAIL_DOMAINS)}",
                start_date - timedelta(days=rng.randint(1, 60)),
            )
        )

    with psycopg.connect(connection_info(f"cyberforge_tenant_{tenant_id}")) as conn:
        copy_rows(conn, "products", "id, tenant_id, name, category, price, created_at", products)
        copy_rows(conn, "customers", "id, tenant_id, name, email, created_at", customers)
        conn.commit()

        product_weights = generate_product_popularity_weights(len(product_lookup))
        item_id = 1
        expected_items = 0
        for first_order in range(1, order_count + 1, args.batch_size):
            last_order = min(first_order + args.batch_size, order_count + 1)
            orders: list[tuple] = []
            items: list[tuple] = []
            for order_id in range(first_order, last_order):
                created_at = min(
                    generate_realistic_timestamp(start_date, generation_now),
                    generation_now,
                )
                status = rng.choices(ORDER_STATUSES, weights=ORDER_STATUS_WEIGHTS, k=1)[0]
                updated_at = created_at if status == "placed" else min(
                    generation_now,
                    created_at + timedelta(minutes=rng.randint(10, 2880)),
                )
                total = 0.0
                item_count = rng.choices([1, 2, 3, 4], weights=[0.55, 0.25, 0.12, 0.08], k=1)[0]
                for product_index in rng.choices(
                    range(len(product_lookup)), weights=product_weights, k=item_count
                ):
                    product_id, unit_price = product_lookup[product_index]
                    quantity = rng.choices([1, 2, 3, 5], weights=[0.70, 0.18, 0.08, 0.04], k=1)[0]
                    total_price = round(unit_price * quantity, 2)
                    total += total_price
                    items.append(
                        (item_id, order_id, tenant_id, product_id, quantity, unit_price,
                         total_price, created_at)
                    )
                    item_id += 1
                orders.append(
                    (order_id, tenant_id, rng.randint(1, args.customers_per_tenant), status,
                     round(total, 2), created_at, updated_at)
                )
            copy_rows(
                conn,
                "orders",
                "id, tenant_id, customer_id, status, total_amount, created_at, updated_at",
                orders,
            )
            copy_rows(
                conn,
                "order_items",
                "id, order_id, tenant_id, product_id, quantity, unit_price, total_price, created_at",
                items,
            )
            conn.commit()
            expected_items += len(items)
            update_progress(tenant_id, dataset_version, last_order - 1, expected_items)

        conn.execute(SECONDARY_INDEXES)
        conn.execute("ANALYZE")
        conn.commit()
    return order_count, expected_items


def update_progress(tenant_id: int, version: str, loaded_orders: int, loaded_items: int) -> None:
    with psycopg.connect(connection_info(settings.db_name)) as conn:
        conn.execute(
            """
            UPDATE tenant_population_status
            SET loaded_orders = %s, loaded_order_items = %s
            WHERE tenant_id = %s AND dataset_version = %s
            """,
            [loaded_orders, loaded_items, tenant_id, version],
        )


def validate_tenant(tenant_id: int, expected_orders: int, expected_items: int, generation_now: datetime) -> None:
    with psycopg.connect(connection_info(f"cyberforge_tenant_{tenant_id}")) as conn:
        actual = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM orders),
                (SELECT COUNT(*) FROM order_items),
                (SELECT COUNT(*) FROM products),
                (SELECT COUNT(*) FROM customers),
                (SELECT COUNT(*) FROM orders
                 WHERE created_at > %s OR updated_at > %s
                    OR updated_at < created_at)
            """,
            [generation_now, generation_now],
        ).fetchone()
    if actual[:2] != (expected_orders, expected_items) or actual[4] != 0:
        raise RuntimeError(
            f"tenant #{tenant_id} validation failed: "
            f"orders/items={actual[:2]}, expected={(expected_orders, expected_items)}, "
            f"invalid_timestamps={actual[4]}"
        )


def populate_one_tenant(
    tenant_id: int,
    expected_orders: int,
    args: argparse.Namespace,
    generation_now: datetime,
    dataset_version: str,
) -> None:
    with psycopg.connect(connection_info(settings.db_name)) as conn:
        status = conn.execute(
            "SELECT status, dataset_version FROM tenant_population_status "
            "WHERE tenant_id = %s",
            [tenant_id],
        ).fetchone()
    if status[0] == "completed" and status[1] == dataset_version:
        print(f"Tenant #{tenant_id}: completed; skipping.")
        return

    started = time.monotonic()
    with psycopg.connect(connection_info(settings.db_name)) as conn:
        conn.execute(
            """
            UPDATE tenant_population_status
            SET status = 'running', started_at = NOW(), completed_at = NULL,
                loaded_orders = 0, loaded_order_items = 0, error_message = NULL
            WHERE tenant_id = %s
            """,
            [tenant_id],
        )
    try:
        orders_loaded, items_loaded = populate_tenant(
            tenant_id, expected_orders, args, generation_now, dataset_version
        )
        validate_tenant(tenant_id, orders_loaded, items_loaded, generation_now)
        with psycopg.connect(connection_info(settings.db_name)) as conn:
            conn.execute(
                """
                UPDATE tenant_population_status
                SET status = 'completed', expected_order_items = %s,
                    loaded_orders = %s, loaded_order_items = %s,
                    completed_at = NOW(), error_message = NULL
                WHERE tenant_id = %s
                """,
                [items_loaded, orders_loaded, items_loaded, tenant_id],
            )
        print(
            f"Tenant #{tenant_id}: completed ({orders_loaded:,} orders, "
            f"{items_loaded:,} items, {time.monotonic() - started:.1f}s)."
        )
    except Exception as exc:
        with psycopg.connect(connection_info(settings.db_name)) as conn:
            conn.execute(
                """
                UPDATE tenant_population_status
                SET status = 'failed', error_message = %s
                WHERE tenant_id = %s
                """,
                [str(exc)[:4000], tenant_id],
            )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Populate tenant databases directly in batches.")
    parser.add_argument("--tenants", type=int, default=10)
    parser.add_argument("--orders", type=int, default=10000)
    parser.add_argument("--batch-size", type=int, default=50000)
    parser.add_argument("--products-per-tenant", type=int, default=40)
    parser.add_argument("--customers-per-tenant", type=int, default=150)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--parallel-tenants", type=int, default=2)
    args = parser.parse_args()
    if (
        args.tenants < 1
        or args.orders < 1
        or args.batch_size < 1
        or args.parallel_tenants < 1
    ):
        raise SystemExit("tenants, orders, batch-size, and parallel-tenants must be positive")

    generation_now = datetime.now(timezone.utc).replace(microsecond=0)
    dataset_version = (
        f"direct-v1:tenants={args.tenants}:orders={args.orders}:"
        f"days={args.days}:seed={args.seed}:batch={args.batch_size}"
    )
    order_counts = allocate_orders(args.orders, args.tenants)
    ensure_metadata(args.tenants)

    with psycopg.connect(connection_info(settings.db_name)) as conn:
        for tenant_id, expected_orders in enumerate(order_counts, start=1):
            conn.execute(
                """
                INSERT INTO tenant_population_status
                    (tenant_id, dataset_version, status, expected_orders,
                     loaded_orders, loaded_order_items)
                VALUES (%s, %s, 'pending', %s, 0, 0)
                ON CONFLICT (tenant_id) DO UPDATE
                SET dataset_version = EXCLUDED.dataset_version,
                    expected_orders = EXCLUDED.expected_orders,
                    status = CASE
                        WHEN tenant_population_status.dataset_version = EXCLUDED.dataset_version
                             AND tenant_population_status.status = 'completed'
                        THEN 'completed' ELSE 'pending' END,
                    loaded_orders = CASE
                        WHEN tenant_population_status.dataset_version = EXCLUDED.dataset_version
                             AND tenant_population_status.status = 'completed'
                        THEN tenant_population_status.loaded_orders ELSE 0 END,
                    loaded_order_items = CASE
                        WHEN tenant_population_status.dataset_version = EXCLUDED.dataset_version
                             AND tenant_population_status.status = 'completed'
                        THEN tenant_population_status.loaded_order_items ELSE 0 END,
                    error_message = NULL
                """,
                [tenant_id, dataset_version, expected_orders],
            )

    tenant_jobs = list(enumerate(order_counts, start=1))
    with ThreadPoolExecutor(
        max_workers=min(args.parallel_tenants, args.tenants),
        thread_name_prefix="TenantPopulate",
    ) as executor:
        futures = [
            executor.submit(
                populate_one_tenant,
                tenant_id,
                expected_orders,
                args,
                generation_now,
                dataset_version,
            )
            for tenant_id, expected_orders in tenant_jobs
        ]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()

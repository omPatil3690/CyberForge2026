#!/usr/bin/env python3
"""
scripts/setup_tenant_databases.py — Create and seed 10 physical PostgreSQL databases.

Sets up true Database-per-Tenant physical isolation at the source layer:
  • cyberforge_tenant_1
  • cyberforge_tenant_2
  ...
  • cyberforge_tenant_10

Copies the tenant's products, customers, orders, and order_items from the central
cyberforge database directly into their dedicated PostgreSQL database.
"""

import sys
import time
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg
from aggregator.config import settings

DDL_TENANT_SCHEMA = """
DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS customers CASCADE;
DROP TABLE IF EXISTS tenants CASCADE;

CREATE TABLE tenants (
    id INT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE customers (
    id INT PRIMARY KEY,
    tenant_id INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_customers_tenant ON customers(tenant_id);

CREATE TABLE products (
    id INT PRIMARY KEY,
    tenant_id INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    price NUMERIC(10, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_products_tenant ON products(tenant_id);

CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    tenant_id INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id INT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    status VARCHAR(50) NOT NULL,
    total_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_orders_tenant_created ON orders(tenant_id, created_at);
CREATE INDEX idx_orders_tenant_status ON orders(tenant_id, status);
CREATE INDEX idx_orders_updated_id ON orders(updated_at, id);

CREATE TABLE order_items (
    id BIGINT PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    tenant_id INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    product_id INT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    quantity INT NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(10, 2) NOT NULL,
    total_price NUMERIC(12, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_order_items_tenant_order ON order_items(tenant_id, order_id);
CREATE INDEX idx_order_items_tenant_product ON order_items(tenant_id, product_id);
"""

def main():
    print("=" * 72)
    print("  CREATING 10 DEDICATED POSTGRESQL DATABASES FOR TENANTS 1..10")
    print("=" * 72)

    base_conn_str = f"host={settings.db_host} port={settings.db_port} user={settings.db_user} password={settings.db_password}"
    admin_conn_str = f"{base_conn_str} dbname=postgres"
    source_conn_str = f"{base_conn_str} dbname={settings.db_name}"

    # 1. Connect to admin DB to create tenant databases
    print("\n1. Creating databases in PostgreSQL...")
    with psycopg.connect(admin_conn_str, autocommit=True) as admin_conn:
        for t_id in range(1, 11):
            db_name = f"cyberforge_tenant_{t_id}"
            # Check if exists
            exists = admin_conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", [db_name]
            ).fetchone()
            if not exists:
                admin_conn.execute(f"CREATE DATABASE {db_name}")
                print(f"   -> Created database: {db_name}")
            else:
                print(f"   -> Database already exists: {db_name}")

    # 2. Apply schema and copy tenant data
    print("\n2. Initializing tables & migrating data into each tenant database...")
    with psycopg.connect(source_conn_str) as src_conn:
        for t_id in range(1, 11):
            db_name = f"cyberforge_tenant_{t_id}"
            target_conn_str = f"{base_conn_str} dbname={db_name}"
            t0 = time.time()

            with psycopg.connect(target_conn_str, autocommit=True) as tgt_conn:
                # Apply DDL
                tgt_conn.execute(DDL_TENANT_SCHEMA)

                # Copy tenant info
                tgt_conn.execute(
                    "INSERT INTO tenants (id, name, created_at) VALUES (%s, %s, NOW())",
                    [t_id, f"Store #{t_id}"],
                )

                # Copy customers
                customers = src_conn.execute(
                    "SELECT id, tenant_id, name, email, created_at FROM customers WHERE tenant_id = %s",
                    [t_id],
                ).fetchall()
                if customers:
                    with tgt_conn.cursor() as cur:
                        with cur.copy("COPY customers (id, tenant_id, name, email, created_at) FROM STDIN") as copy:
                            for row in customers:
                                copy.write_row(row)

                # Copy products
                products = src_conn.execute(
                    "SELECT id, tenant_id, name, category, price, created_at FROM products WHERE tenant_id = %s",
                    [t_id],
                ).fetchall()
                if products:
                    with tgt_conn.cursor() as cur:
                        with cur.copy("COPY products (id, tenant_id, name, category, price, created_at) FROM STDIN") as copy:
                            for row in products:
                                copy.write_row(row)

                # Copy orders
                orders = src_conn.execute(
                    """
                    SELECT id, tenant_id, customer_id, status, total_amount, created_at, updated_at
                    FROM orders WHERE tenant_id = %s
                    """,
                    [t_id],
                ).fetchall()
                if orders:
                    with tgt_conn.cursor() as cur:
                        with cur.copy("COPY orders (id, tenant_id, customer_id, status, total_amount, created_at, updated_at) FROM STDIN") as copy:
                            for row in orders:
                                copy.write_row(row)

                # Copy order items
                items = src_conn.execute(
                    """
                    SELECT id, order_id, tenant_id, product_id, quantity, unit_price, total_price, created_at
                    FROM order_items WHERE tenant_id = %s
                    """,
                    [t_id],
                ).fetchall()
                if items:
                    with tgt_conn.cursor() as cur:
                        with cur.copy("COPY order_items (id, order_id, tenant_id, product_id, quantity, unit_price, total_price, created_at) FROM STDIN") as copy:
                            for row in items:
                                copy.write_row(row)

                print(f"   -> [{db_name}] Synced {len(orders):,} orders, {len(items):,} items, {len(products)} products ({time.time() - t0:.2f}s)")

    print("\n" + "=" * 72)
    print("  SUCCESS: 10 Dedicated PostgreSQL Databases Created and Populated!")
    print("  You can now refresh pgAdmin 4 to see cyberforge_tenant_1 .. 10!")
    print("=" * 72)

if __name__ == "__main__":
    main()

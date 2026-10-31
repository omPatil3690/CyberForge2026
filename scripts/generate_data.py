#!/usr/bin/env python3
"""
Synthetic Data Generator for CyberForge Multi-Tenant E-Commerce System
Generates realistic multi-tenant data directly into PostgreSQL using high-speed COPY streaming.
Supports Zipfian tenant distribution, realistic diurnal timestamps, product popularity skews,
and order state distributions according to PS.md specifications.
"""

import argparse
import io
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import psycopg
except ImportError:
    print("Error: psycopg is required. Install via `.venv/bin/pip install 'psycopg[binary]'`")
    sys.exit(1)

from db import get_connection, get_db_config


# E-commerce sample catalogues
CATEGORIES = [
    "Electronics", "Apparel", "Home & Kitchen", "Books", "Beauty", "Sports & Outdoors"
]

PRODUCT_PREFIXES = {
    "Electronics": ["Pro", "Ultra", "Wireless", "Smart", "Mini", "Noise-Cancelling"],
    "Apparel": ["Classic", "Slim-Fit", "Casual", "Vintage", "Thermal", "Organic Cotton"],
    "Home & Kitchen": ["Ceramic", "Stainless Steel", "Non-Stick", "Bamboo", "Ergonomic", "Compact"],
    "Books": ["The Guide to", "Mastering", "Principles of", "The Art of", "Modern", "Advanced"],
    "Beauty": ["Hydrating", "Revitalizing", "Natural", "Purifying", "Glow", "Anti-Aging"],
    "Sports & Outdoors": ["Aerobic", "Trail", "Hydration", "Performance", "Tactical", "Weatherproof"]
}

PRODUCT_NOUNS = {
    "Electronics": ["Headphones", "Earbuds", "Charger", "Keyboard", "Mouse", "Monitor", "Power Bank"],
    "Apparel": ["T-Shirt", "Hoodie", "Jeans", "Jacket", "Sneakers", "Cap", "Socks"],
    "Home & Kitchen": ["Knife Set", "Coffee Maker", "Blender", "Pan", "Toaster", "Kettle", "Food Storage"],
    "Books": ["Programming", "Finance", "Fiction", "Data Engineering", "Architecture", "Cooking"],
    "Beauty": ["Moisturizer", "Serum", "Cleanser", "Sunscreen", "Lip Balm", "Face Mask"],
    "Sports & Outdoors": ["Water Bottle", "Running Shoes", "Yoga Mat", "Backpack", "Resistance Bands", "Towel"]
}

FIRST_NAMES = [
    "Aarav", "Aditi", "Alexander", "Amara", "Carlos", "Chen", "Elena", "Fatima", "Grace", "Hiroshi",
    "Ibrahim", "Isabella", "Kavya", "Liam", "Mei", "Noah", "Olivia", "Priya", "Rohan", "Sofia"
]
LAST_NAMES = [
    "Patil", "Smith", "Zhang", "Garcia", "Kim", "Muller", "Singh", "Tanaka", "Kowalski", "Johnson",
    "Sharma", "Brown", "Ali", "Rossi", "Wang", "Davis", "Fernandez", "Martin", "Das", "Lee"
]
EMAIL_DOMAINS = ["example.com", "storemail.io", "webshop.org", "inbox.net", "testmail.com"]

ORDER_STATUSES = ["delivered", "shipped", "placed", "cancelled"]
ORDER_STATUS_WEIGHTS = [0.65, 0.18, 0.10, 0.07]  # 65% delivered, 18% shipped, 10% placed, 7% cancelled


def generate_zipf_weights(n: int, s: float = 1.15) -> list[float]:
    """Generate normalized Zipfian weights to simulate real-world Pareto tenant volume skew."""
    weights = [1.0 / math.pow(i, s) for i in range(1, n + 1)]
    total = sum(weights)
    return [w / total for w in weights]


def generate_product_popularity_weights(n: int) -> list[float]:
    """Generate weights where top products get a disproportionately high share of orders."""
    weights = [1.0 / math.pow(i, 0.85) for i in range(1, n + 1)]
    total = sum(weights)
    return [w / total for w in weights]


def generate_realistic_timestamp(start_date: datetime, end_date: datetime) -> datetime:
    """Generate timestamp with diurnal seasonality (peaks during 11 AM - 9 PM, low overnight)."""
    total_seconds = int((end_date - start_date).total_seconds())
    random_seconds = random.randint(0, total_seconds)
    base_dt = start_date + timedelta(seconds=random_seconds)

    # Bias hour towards peak commerce hours
    hour_weights = [
        1, 1, 1, 1, 1, 2,  # 00:00 - 05:59
        3, 5, 8, 12, 16, 18, # 06:00 - 11:59
        20, 22, 22, 20, 19, 21, # 12:00 - 17:59
        22, 20, 16, 12, 6, 3   # 18:00 - 23:59
    ]
    sampled_hour = random.choices(range(24), weights=hour_weights, k=1)[0]
    minute = random.randint(0, 59)
    second = random.randint(0, 59)
    return base_dt.replace(hour=sampled_hour, minute=minute, second=second)


def bulk_copy(cursor, table_name: str, columns: list[str], rows_generator):
    """Stream tabular data into PostgreSQL using COPY FROM STDIN for maximum speed."""
    buf = io.StringIO()
    count = 0
    col_str = ", ".join(columns)
    copy_sql = f"COPY {table_name} ({col_str}) FROM STDIN WITH (FORMAT CSV, NULL '\\N')"

    with cursor.copy(copy_sql) as copy:
        for row in rows_generator:
            formatted_fields = []
            for item in row:
                if item is None:
                    formatted_fields.append("\\N")
                elif isinstance(item, (int, float)):
                    formatted_fields.append(str(item))
                elif isinstance(item, datetime):
                    formatted_fields.append(item.isoformat())
                else:
                    # Escape CSV quotes
                    escaped = str(item).replace('"', '""')
                    formatted_fields.append(f'"{escaped}"')
            buf.write(",".join(formatted_fields) + "\n")
            count += 1

            if buf.tell() > 2 * 1024 * 1024:  # Flush 2MB chunks
                buf.seek(0)
                copy.write(buf.read())
                buf.seek(0)
                buf.truncate()

        if buf.tell() > 0:
            buf.seek(0)
            copy.write(buf.read())

    return count


def main():
    db_cfg = get_db_config()
    parser = argparse.ArgumentParser(description="Multi-Tenant Synthetic Data Generator for PostgreSQL")
    parser.add_argument("--tenants", type=int, default=5, help="Number of tenants (default: 5)")
    parser.add_argument("--orders", type=int, default=10000, help="Total number of orders (default: 10,000)")
    parser.add_argument("--products-per-tenant", type=int, default=40, help="Products per tenant (default: 40)")
    parser.add_argument("--customers-per-tenant", type=int, default=150, help="Customers per tenant (default: 150)")
    parser.add_argument("--days", type=int, default=60, help="Historical time window in days (default: 60)")
    parser.add_argument("--db-name", type=str, default=db_cfg["dbname"], help=f"PostgreSQL database name (default: {db_cfg['dbname']})")
    parser.add_argument("--db-user", type=str, default=db_cfg["user"], help=f"PostgreSQL user (default: {db_cfg['user']})")
    parser.add_argument("--db-host", type=str, default=db_cfg["host"], help=f"PostgreSQL host (default: {db_cfg['host']})")
    parser.add_argument("--db-port", type=int, default=db_cfg["port"], help=f"PostgreSQL port (default: {db_cfg['port']})")
    parser.add_argument("--db-password", type=str, default=db_cfg["password"], help="PostgreSQL password")
    args = parser.parse_args()

    conn_info = f"dbname={args.db_name} user={args.db_user} host={args.db_host} port={args.db_port}"
    if args.db_password:
        conn_info += f" password={args.db_password}"

    print("================================================================")
    print("CyberForge Multi-Tenant Synthetic Data Generator")
    print("================================================================")
    print(f"Target Database      : {args.db_name} @ {args.db_host}:{args.db_port}")
    print(f"Tenants              : {args.tenants}")
    print(f"Target Orders        : {args.orders:,}")
    print(f"Products / Tenant    : {args.products_per_tenant}")
    print(f"Customers / Tenant   : {args.customers_per_tenant}")
    print(f"Time Range           : Past {args.days} days")
    print("----------------------------------------------------------------")

    start_total_time = time.time()

    try:
        conn = get_connection(args)
        conn.autocommit = False
    except Exception as e:
        print(f"Error connecting to PostgreSQL: {e}")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=args.days)

    with conn:
        with conn.cursor() as cur:
            print("1. Truncating existing tables...")
            cur.execute("TRUNCATE TABLE order_items, orders, products, customers, tenants RESTART IDENTITY CASCADE;")

            # Company ownership is control-plane metadata and must survive
            # dataset resets so company-owner APIs can resolve all tenants.
            cur.execute(
                """
                INSERT INTO companies (id, name)
                VALUES (1, 'Global Retail Corp')
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
                """
            )

            # -------------------------------------------------------------
            # Generate Tenants
            # -------------------------------------------------------------
            print(f"2. Generating {args.tenants} tenants...")
            t0 = time.time()
            tenant_rows = []
            for t_id in range(1, args.tenants + 1):
                created_at = start_date - timedelta(days=random.randint(10, 180))
                tenant_rows.append((t_id, f"Store-{t_id:03d} - {random.choice(['Apparel', 'Direct', 'Outlet', 'Goods', 'Boutique'])}", created_at))

            tenant_rows = [
                (t_id, name, created_at, 1)
                for t_id, name, created_at in tenant_rows
            ]
            bulk_copy(cur, "tenants", ["id", "name", "created_at", "company_id"], tenant_rows)
            print(f"   -> Inserted {len(tenant_rows)} tenants ({time.time() - t0:.2f}s)")

            # -------------------------------------------------------------
            # Generate Products
            # -------------------------------------------------------------
            print(f"3. Generating products ({args.products_per_tenant} per tenant)...")
            t0 = time.time()
            product_rows = []
            products_by_tenant = {t_id: [] for t_id in range(1, args.tenants + 1)}
            prod_id = 1

            for t_id in range(1, args.tenants + 1):
                for p_idx in range(1, args.products_per_tenant + 1):
                    cat = random.choice(CATEGORIES)
                    prefix = random.choice(PRODUCT_PREFIXES[cat])
                    noun = random.choice(PRODUCT_NOUNS[cat])
                    name = f"{prefix} {noun} #{p_idx}"
                    # Prices with realistic distribution ($8.00 to $399.00)
                    price = round(random.uniform(8.0, 399.0), 2)
                    created_at = start_date - timedelta(days=random.randint(1, 30))
                    product_rows.append((prod_id, t_id, name, cat, price, created_at))
                    products_by_tenant[t_id].append((prod_id, price))
                    prod_id += 1

            bulk_copy(cur, "products", ["id", "tenant_id", "name", "category", "price", "created_at"], product_rows)
            print(f"   -> Inserted {len(product_rows):,} products ({time.time() - t0:.2f}s)")

            # -------------------------------------------------------------
            # Generate Customers
            # -------------------------------------------------------------
            print(f"4. Generating customers ({args.customers_per_tenant} per tenant)...")
            t0 = time.time()
            customer_rows = []
            customers_by_tenant = {t_id: [] for t_id in range(1, args.tenants + 1)}
            cust_id = 1

            for t_id in range(1, args.tenants + 1):
                for c_idx in range(1, args.customers_per_tenant + 1):
                    first = random.choice(FIRST_NAMES)
                    last = random.choice(LAST_NAMES)
                    name = f"{first} {last}"
                    domain = random.choice(EMAIL_DOMAINS)
                    email = f"{first.lower()}.{last.lower()}_{t_id}_{c_idx}@{domain}"
                    created_at = start_date - timedelta(days=random.randint(1, 60))
                    customer_rows.append((cust_id, t_id, name, email, created_at))
                    customers_by_tenant[t_id].append(cust_id)
                    cust_id += 1

            bulk_copy(cur, "customers", ["id", "tenant_id", "name", "email", "created_at"], customer_rows)
            print(f"   -> Inserted {len(customer_rows):,} customers ({time.time() - t0:.2f}s)")

            # -------------------------------------------------------------
            # Generate Orders and Order Items
            # -------------------------------------------------------------
            print(f"5. Generating {args.orders:,} orders & line items with Zipfian tenant distribution...")
            t0 = time.time()

            tenant_ids = list(range(1, args.tenants + 1))
            tenant_weights = generate_zipf_weights(args.tenants, s=1.15)
            # Precompute product popularity weights per tenant
            prod_pop_weights = {
                t_id: generate_product_popularity_weights(len(products_by_tenant[t_id]))
                for t_id in tenant_ids
            }

            orders_data = []
            order_items_data = []
            order_item_id = 1

            for order_id in range(1, args.orders + 1):
                # Pick tenant based on Zipfian volume distribution
                t_id = random.choices(tenant_ids, weights=tenant_weights, k=1)[0]
                customer_id = random.choice(customers_by_tenant[t_id])

                order_created_at = generate_realistic_timestamp(start_date, now)
                status = random.choices(ORDER_STATUSES, weights=ORDER_STATUS_WEIGHTS, k=1)[0]
                if status == "placed":
                    updated_at = order_created_at
                else:
                    updated_at = min(now, order_created_at + timedelta(minutes=random.randint(10, 2880)))

                # 1 to 4 line items per order
                num_items = random.choices([1, 2, 3, 4], weights=[0.55, 0.25, 0.12, 0.08], k=1)[0]
                t_products = products_by_tenant[t_id]
                t_weights = prod_pop_weights[t_id]

                selected_indices = random.choices(range(len(t_products)), weights=t_weights, k=num_items)
                total_order_amount = 0.0

                for idx in selected_indices:
                    p_id, unit_price = t_products[idx]
                    quantity = random.choices([1, 2, 3, 5], weights=[0.70, 0.18, 0.08, 0.04], k=1)[0]
                    total_price = round(unit_price * quantity, 2)
                    total_order_amount += total_price

                    order_items_data.append((
                        order_item_id,
                        order_id,
                        t_id,
                        p_id,
                        quantity,
                        unit_price,
                        total_price,
                        order_created_at
                    ))
                    order_item_id += 1

                total_order_amount = round(total_order_amount, 2)
                orders_data.append((
                    order_id,
                    t_id,
                    customer_id,
                    status,
                    total_order_amount,
                    order_created_at,
                    updated_at
                ))

            # Stream orders
            bulk_copy(
                cur,
                "orders",
                ["id", "tenant_id", "customer_id", "status", "total_amount", "created_at", "updated_at"],
                orders_data
            )
            # Stream order items
            bulk_copy(
                cur,
                "order_items",
                ["id", "order_id", "tenant_id", "product_id", "quantity", "unit_price", "total_price", "created_at"],
                order_items_data
            )
            print(f"   -> Inserted {len(orders_data):,} orders and {len(order_items_data):,} order items ({time.time() - t0:.2f}s)")

            # Reset sequences to match max ids
            cur.execute("SELECT setval('tenants_id_seq', (SELECT MAX(id) FROM tenants));")
            cur.execute("SELECT setval('products_id_seq', (SELECT MAX(id) FROM products));")
            cur.execute("SELECT setval('customers_id_seq', (SELECT MAX(id) FROM customers));")
            cur.execute("SELECT setval('orders_id_seq', (SELECT MAX(id) FROM orders));")
            cur.execute("SELECT setval('order_items_id_seq', (SELECT MAX(id) FROM order_items));")

            # Run ANALYZE for query planner accuracy
            print("6. Updating query planner statistics (ANALYZE)...")
            cur.execute("ANALYZE tenants, customers, products, orders, order_items;")

    conn.close()
    elapsed = time.time() - start_total_time
    print("----------------------------------------------------------------")
    print(f"SUCCESS: Generated & loaded data into PostgreSQL in {elapsed:.2f} seconds.")
    print("================================================================")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Verification and Baseline Diagnostic Script for CyberForge
Validates synthetic data integrity, tenant distribution, status counts,
and computes ground-truth baseline answers for FR-1, FR-2, FR-3, and FR-4.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import psycopg
except ImportError:
    print("Error: psycopg is required. Install via `.venv/bin/pip install 'psycopg[binary]'`")
    sys.exit(1)

from db import get_connection


def main():
    try:
        conn = get_connection()
    except Exception as e:
        print(f"Error connecting to PostgreSQL: {e}")
        sys.exit(1)

    print("================================================================")
    print("CyberForge Synthetic Data Verification & Diagnostics")
    print("================================================================")

    with conn.cursor() as cur:
        # Table counts
        cur.execute("""
            SELECT 
                (SELECT count(*) FROM tenants) AS tenants,
                (SELECT count(*) FROM customers) AS customers,
                (SELECT count(*) FROM products) AS products,
                (SELECT count(*) FROM orders) AS orders,
                (SELECT count(*) FROM order_items) AS order_items;
        """)
        counts = cur.fetchone()
        print(f"Total Tenants       : {counts[0]:,}")
        print(f"Total Customers     : {counts[1]:,}")
        print(f"Total Products      : {counts[2]:,}")
        print(f"Total Orders        : {counts[3]:,}")
        print(f"Total Order Items   : {counts[4]:,}")
        print("----------------------------------------------------------------")

        # Tenant Volume Distribution (Checking Zipfian skew)
        print("Tenant Order Distribution (Verifying Skew):")
        cur.execute("""
            SELECT 
                t.id, 
                t.name, 
                count(o.id) as order_count,
                round(100.0 * count(o.id) / (SELECT count(*) FROM orders), 2) as pct,
                coalesce(sum(o.total_amount), 0) as total_revenue
            FROM tenants t
            LEFT JOIN orders o ON t.id = o.tenant_id
            GROUP BY t.id, t.name
            ORDER BY order_count DESC;
        """)
        for row in cur.fetchall():
            print(f"  Tenant #{row[0]:02d} ({row[1]:<28}): {row[2]:>6,} orders ({row[3]:>5.1f}%) | Revenue: ${row[4]:>10,.2f}")
        print("----------------------------------------------------------------")

        # Status Breakdown (FR-4 check)
        print("Order Status Breakdown (FR-4 baseline):")
        cur.execute("""
            SELECT status, count(*) as count,
                   round(100.0 * count(*) / (SELECT count(*) FROM orders), 2) as pct
            FROM orders
            GROUP BY status
            ORDER BY count DESC;
        """)
        for row in cur.fetchall():
            print(f"  {row[0]:<12}: {row[1]:>6,} orders ({row[2]:>5.1f}%)")
        print("----------------------------------------------------------------")

        # Sample FR-1: Last 7 days counts and total revenue for top tenant
        cur.execute("SELECT id FROM tenants ORDER BY id LIMIT 1;")
        sample_tenant_id = cur.fetchone()[0]

        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)
        cur.execute("""
            SELECT count(*), coalesce(sum(total_amount), 0)
            FROM orders
            WHERE tenant_id = %s AND created_at >= %s;
        """, (sample_tenant_id, seven_days_ago))
        fr1_res = cur.fetchone()
        print(f"FR-1 Sample Query (Tenant #{sample_tenant_id}, Last 7 Days):")
        print(f"  Orders : {fr1_res[0]:,}")
        print(f"  Revenue: ${fr1_res[1]:,.2f}")
        print("----------------------------------------------------------------")

        # Sample FR-3: Top 5 products by revenue for top tenant
        cur.execute("""
            SELECT p.id, p.name, p.category, sum(oi.quantity) as qty_sold, sum(oi.total_price) as total_sales
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE oi.tenant_id = %s
            GROUP BY p.id, p.name, p.category
            ORDER BY total_sales DESC
            LIMIT 5;
        """, (sample_tenant_id,))
        print(f"FR-3 Sample Query (Tenant #{sample_tenant_id}, Top 5 Products by Sales):")
        for rank, p in enumerate(cur.fetchall(), 1):
            print(f"  #{rank}. [{p[2]}] {p[1]} -> {p[3]} units, ${p[4]:,.2f}")
        print("================================================================")

    conn.close()


if __name__ == "__main__":
    main()

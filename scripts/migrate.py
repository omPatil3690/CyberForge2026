#!/usr/bin/env python3
"""
Database Migration Runner for CyberForge
Manages database schema migrations using versioned SQL files (up/down).
Tracks applied migrations in a dedicated `schema_migrations` table in PostgreSQL.

Usage:
    python3 scripts/migrate.py up
    python3 scripts/migrate.py down [--steps 1]
    python3 scripts/migrate.py status
    python3 scripts/migrate.py create <migration_name>
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg
except ImportError:
    print("Error: psycopg is required. Run via `.venv/bin/python3 scripts/migrate.py`")
    sys.exit(1)

from db import get_connection, get_db_config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def ensure_migrations_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(50) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)


def get_applied_migrations(cur):
    ensure_migrations_table(cur)
    cur.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version ASC;")
    return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def get_migration_files():
    if not MIGRATIONS_DIR.exists():
        MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)

    up_files = {}
    down_files = {}

    for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = re.match(r"^(\d+)_(.+)\.(up|down)\.sql$", file.name)
        if match:
            version, name, direction = match.groups()
            if direction == "up":
                up_files[version] = (name, file)
            else:
                down_files[version] = (name, file)

    return up_files, down_files


def cmd_status(args):
    conn = get_connection(args)
    with conn.cursor() as cur:
        applied = get_applied_migrations(cur)
        up_files, _ = get_migration_files()

        all_versions = sorted(set(list(applied.keys()) + list(up_files.keys())))

        print("================================================================")
        print("Database Migration Status")
        print("================================================================")
        print(f"{'Version':<10} {'Name':<35} {'Status':<12} {'Applied At'}")
        print("-" * 75)

        if not all_versions:
            print("No migration files found.")
        else:
            for v in all_versions:
                name = up_files.get(v, (applied.get(v, ("Unknown", None))[0], None))[0]
                if v in applied:
                    applied_time = applied[v][1].strftime("%Y-%m-%d %H:%M:%S %Z")
                    status = "APPLIED"
                else:
                    applied_time = "-"
                    status = "PENDING"
                print(f"{v:<10} {name:<35} {status:<12} {applied_time}")
        print("================================================================")
    conn.close()


def cmd_up(args):
    conn = get_connection(args)
    with conn:
        with conn.cursor() as cur:
            applied = get_applied_migrations(cur)
            up_files, _ = get_migration_files()

            pending_versions = [v for v in sorted(up_files.keys()) if v not in applied]

            if not pending_versions:
                print("Database is up to date. No pending migrations.")
                return

            print(f"Applying {len(pending_versions)} pending migration(s)...")
            for v in pending_versions:
                name, file_path = up_files[v]
                sql_content = file_path.read_text()
                t0 = time.time()
                try:
                    cur.execute(sql_content)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, NOW());",
                        (v, name)
                    )
                    elapsed = time.time() - t0
                    print(f"  [OK] Applied {v}_{name} ({elapsed:.3f}s)")
                except Exception as e:
                    print(f"  [FAIL] Error executing migration {v}_{name}: {e}")
                    raise
    conn.close()
    print("All pending migrations applied successfully.")


def cmd_down(args):
    conn = get_connection(args)
    with conn:
        with conn.cursor() as cur:
            applied = get_applied_migrations(cur)
            _, down_files = get_migration_files()

            applied_versions = sorted(applied.keys(), reverse=True)
            if not applied_versions:
                print("No migrations to revert.")
                return

            steps = min(args.steps, len(applied_versions))
            to_revert = applied_versions[:steps]

            print(f"Reverting {len(to_revert)} migration(s)...")
            for v in to_revert:
                if v not in down_files:
                    print(f"Error: Missing down migration file for version {v}")
                    sys.exit(1)

                name, file_path = down_files[v]
                sql_content = file_path.read_text()
                t0 = time.time()
                try:
                    cur.execute(sql_content)
                    cur.execute("DELETE FROM schema_migrations WHERE version = %s;", (v,))
                    elapsed = time.time() - t0
                    print(f"  [OK] Reverted {v}_{name} ({elapsed:.3f}s)")
                except Exception as e:
                    print(f"  [FAIL] Error reverting migration {v}_{name}: {e}")
                    raise
    conn.close()
    print("Rollback completed successfully.")


def cmd_create(args):
    up_files, _ = get_migration_files()
    if up_files:
        latest_num = max(int(v) for v in up_files.keys() if v.isdigit())
        next_num = latest_num + 1
    else:
        next_num = 1

    clean_name = re.sub(r"[^a-zA-Z0-9_]+", "_", args.name.strip().lower())
    prefix = f"{next_num:03d}_{clean_name}"

    up_file = MIGRATIONS_DIR / f"{prefix}.up.sql"
    down_file = MIGRATIONS_DIR / f"{prefix}.down.sql"

    up_file.write_text(f"-- Migration: {up_file.name}\n-- Description: {args.name}\n\n")
    down_file.write_text(f"-- Migration: {down_file.name}\n-- Description: Revert {args.name}\n\n")

    print(f"Created migration template files:")
    print(f"  - {up_file}")
    print(f"  - {down_file}")


def main():
    parser = argparse.ArgumentParser(description="PostgreSQL Database Migration Runner")
    parser.add_argument("--db-name", type=str, default=os.getenv("DB_NAME", "cyberforge"))
    parser.add_argument("--db-user", type=str, default=os.getenv("DB_USER", os.getenv("USER", "postgres")))
    parser.add_argument("--db-host", type=str, default=os.getenv("DB_HOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", 5432)))
    parser.add_argument("--db-password", type=str, default=os.getenv("DB_PASSWORD", ""))

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: up
    subparsers.add_parser("up", help="Apply all pending migrations")

    # Subcommand: down
    down_parser = subparsers.add_parser("down", help="Revert the latest migration(s)")
    down_parser.add_argument("--steps", type=int, default=1, help="Number of migrations to revert (default: 1)")

    # Subcommand: status
    subparsers.add_parser("status", help="Show migration status")

    # Subcommand: create
    create_parser = subparsers.add_parser("create", help="Create a new migration file pair")
    create_parser.add_argument("name", type=str, help="Name of the migration (e.g., add_user_avatar)")

    args = parser.parse_args()

    if args.command == "up":
        cmd_up(args)
    elif args.command == "down":
        cmd_down(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "create":
        cmd_create(args)


if __name__ == "__main__":
    main()

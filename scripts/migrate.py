#!/usr/bin/env python3
"""
Database Migration CLI (Alembic Wrapper) for CyberForge
Executes and manages versioned schema migrations via Alembic.

Usage:
    python3 scripts/migrate.py up                   # Apply all pending migrations (head)
    python3 scripts/migrate.py down [--steps 1]     # Roll back migration(s)
    python3 scripts/migrate.py status               # Show current revision and history
    python3 scripts/migrate.py create <name>        # Generate a new migration revision
"""

import argparse
import sys
from pathlib import Path

try:
    from alembic.config import Config
    from alembic import command
except ImportError:
    print("Error: alembic is required. Run via `.venv/bin/python3 scripts/migrate.py`")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def get_alembic_config() -> Config:
    if not ALEMBIC_INI.is_file():
        print(f"Error: Could not find alembic.ini at {ALEMBIC_INI}")
        sys.exit(1)
    cfg = Config(str(ALEMBIC_INI))
    return cfg


def cmd_up(args):
    cfg = get_alembic_config()
    target = args.revision if hasattr(args, "revision") and args.revision else "head"
    print(f"Running Alembic migrations up to: {target}...")
    command.upgrade(cfg, target)
    print("Migrations applied successfully.")


def cmd_down(args):
    cfg = get_alembic_config()
    steps = f"-{args.steps}"
    print(f"Rolling back {args.steps} migration step(s)...")
    command.downgrade(cfg, steps)
    print("Rollback completed successfully.")


def cmd_status(args):
    cfg = get_alembic_config()
    print("================================================================")
    print("Alembic Current Migration Revision:")
    command.current(cfg, verbose=True)
    print("----------------------------------------------------------------")
    print("Migration History:")
    command.history(cfg, verbose=True)
    print("================================================================")


def cmd_create(args):
    cfg = get_alembic_config()
    print(f"Creating new revision: {args.name}...")
    command.revision(cfg, message=args.name, autogenerate=False)


def main():
    parser = argparse.ArgumentParser(description="CyberForge Database Migration Runner (Alembic)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: up
    up_parser = subparsers.add_parser("up", help="Apply pending migrations to head")
    up_parser.add_argument("--revision", type=str, default="head", help="Target revision (default: head)")

    # Subcommand: down
    down_parser = subparsers.add_parser("down", help="Revert the latest migration(s)")
    down_parser.add_argument("--steps", type=int, default=1, help="Number of migrations to revert (default: 1)")

    # Subcommand: status
    subparsers.add_parser("status", help="Show current revision and migration history")

    # Subcommand: create
    create_parser = subparsers.add_parser("create", help="Create a new migration revision")
    create_parser.add_argument("name", type=str, help="Descriptive name of the revision")

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

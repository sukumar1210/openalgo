#!/usr/bin/env python
"""
Sandbox Accounts Migration Script for OpenAlgo

Sandbox accounts give a scenario money of its own inside one user's Sandbox
(sandbox/accounts.py). They are off unless SANDBOX_ACCOUNTS_ENABLED is true.

Changes:
- Creates sandbox_accounts: each account's principal, owner, purpose, starting
  capital, charge setting and status.
- Creates sandbox_account_events: money moved by anything but trading
  (opening, deposits, withdrawals, transfers, per-fill charges).
- Creates sandbox_follows and sandbox_copies: which accounts copy a source
  account's position, and the claim that copies each of its trades once
  (sandbox/copy_engine.py).

Both tables are new, so nothing that exists is changed and there is nothing to
backfill. The DDL is read from the ORM models.

Idempotent - safe to run multiple times.

Usage:
    cd upgrade
    uv run migrate_sandbox_accounts.py           # Apply migration
    uv run migrate_sandbox_accounts.py --status  # Check status

Migration: 007
Created: 2026-10-15
"""

import argparse
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Register the app's SQLite pragmas on this process's engines, so a migration
# waits the same 15s for a write lock the running app does instead of the
# sqlite3 default of 5s (GitHub issue #1726).
import _pragmas  # noqa: F401,E402
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect

from utils.logging import get_logger

logger = get_logger(__name__)

MIGRATION_NAME = "sandbox_accounts"
MIGRATION_VERSION = "007"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

TABLES = ("sandbox_accounts", "sandbox_account_events", "sandbox_follows", "sandbox_copies")


def get_sandbox_db_engine():
    """Get sandbox database engine"""
    sandbox_db_url = os.getenv("SANDBOX_DATABASE_URL", "sqlite:///db/sandbox.db")

    if sandbox_db_url.startswith("sqlite:///"):
        db_path = sandbox_db_url.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = os.path.join(parent_dir, db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        sandbox_db_url = f"sqlite:///{db_path}"
        logger.info(f"Sandbox DB path: {db_path}")

    return create_engine(sandbox_db_url)


def missing(engine) -> list[str]:
    present = set(inspect(engine).get_table_names())
    return [name for name in TABLES if name not in present]


def upgrade() -> bool:
    from database.sandbox_db import Base

    try:
        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")
        engine = get_sandbox_db_engine()
        todo = missing(engine)
        if todo:
            Base.metadata.create_all(bind=engine, tables=[Base.metadata.tables[n] for n in todo])
            logger.info(f"Created {', '.join(todo)}")
        else:
            logger.info("Nothing to do: the Sandbox account tables already exist")
        logger.info(f"Migration {MIGRATION_NAME} completed successfully")
        return True
    except Exception as e:
        logger.exception(f"Migration {MIGRATION_NAME} failed: {e}")
        return False


def status() -> bool:
    todo = missing(get_sandbox_db_engine())
    if todo:
        logger.info(f"Pending: create {', '.join(todo)}")
        return False
    logger.info("Applied: the Sandbox account tables exist")
    return True


def main():
    parser = argparse.ArgumentParser(description=f"OpenAlgo migration: {MIGRATION_NAME}")
    parser.add_argument("--status", action="store_true", help="Report without changing anything")
    args = parser.parse_args()
    if args.status:
        return 0 if status() else 1
    return 0 if upgrade() else 1


if __name__ == "__main__":
    sys.exit(main())

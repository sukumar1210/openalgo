#!/usr/bin/env python
"""
Sandbox MIS Loss Limit Migration Script for OpenAlgo

The sandbox can now square off intraday (MIS) positions on a loss, the way a
broker's risk system does: once the day's loss on MIS positions reaches a set
percentage of capital, every MIS position is squared off and new MIS orders
are refused until the next session.

Changes:
- Adds sandbox_funds.loss_limit_hit_at (nullable DATETIME): when the account
  reached the limit. NULL for every existing account, which is the truth: the
  limit is new and off by default, so no account has reached it.
- Seeds the sandbox_config key mis_loss_limit_percent = '0' (off) where it is
  missing. An existing value is never touched.

Idempotent - safe to run multiple times.

Usage:
    cd upgrade
    uv run migrate_sandbox_loss_limit.py           # Apply migration
    uv run migrate_sandbox_loss_limit.py --status  # Check status

Migration: 006
Created: 2026-10-10
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
from sqlalchemy import create_engine, text

from utils.logging import get_logger

logger = get_logger(__name__)

MIGRATION_NAME = "sandbox_mis_loss_limit"
MIGRATION_VERSION = "006"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

CONFIG_KEY = "mis_loss_limit_percent"


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


def _columns(conn, table):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def pending(conn):
    """What this migration would change: (missing column, missing config key)."""
    funds = _columns(conn, "sandbox_funds")
    config = _columns(conn, "sandbox_config")
    column = bool(funds) and "loss_limit_hit_at" not in funds
    key = (
        bool(config)
        and not conn.execute(
            text("SELECT 1 FROM sandbox_config WHERE config_key = :key"), {"key": CONFIG_KEY}
        ).fetchone()
    )
    return column, key


def upgrade():
    """Add the column and the config key where they are missing."""
    from database.sandbox_db import MIS_LOSS_LIMIT_DESCRIPTION

    try:
        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")
        engine = get_sandbox_db_engine()
        with engine.connect() as conn:
            if not _columns(conn, "sandbox_funds") or not _columns(conn, "sandbox_config"):
                logger.info(
                    "Sandbox tables not created yet; the app creates them with this in place"
                )
                return True
            column, key = pending(conn)
            if column:
                conn.execute(
                    text("ALTER TABLE sandbox_funds ADD COLUMN loss_limit_hit_at DATETIME")
                )
                logger.info("Added sandbox_funds.loss_limit_hit_at")
            if key:
                # Explicit updated_at: sandbox_config may have been created by
                # create_all(), whose default lives in the ORM only.
                conn.execute(
                    text(
                        "INSERT INTO sandbox_config (config_key, config_value, description, updated_at) "
                        "VALUES (:key, '0', :description, CURRENT_TIMESTAMP)"
                    ),
                    {"key": CONFIG_KEY, "description": MIS_LOSS_LIMIT_DESCRIPTION},
                )
                logger.info(f"Added sandbox config {CONFIG_KEY} = 0 (off)")
            conn.commit()
            if not (column or key):
                logger.info("Nothing to do: the MIS loss limit is already in place")
        logger.info(f"Migration {MIGRATION_NAME} completed successfully")
        return True
    except Exception:
        logger.exception(f"Migration {MIGRATION_NAME} failed")
        return False


def status():
    """Report what upgrade() would change, changing nothing."""
    try:
        engine = get_sandbox_db_engine()
        with engine.connect() as conn:
            if not _columns(conn, "sandbox_funds") or not _columns(conn, "sandbox_config"):
                logger.info("Sandbox tables not created yet; run migrate_sandbox.py first")
                return False
            column, key = pending(conn)
        if column:
            logger.info("[PENDING] column sandbox_funds.loss_limit_hit_at")
        if key:
            logger.info(f"[PENDING] sandbox config {CONFIG_KEY} (would be added as 0, off)")
        if not (column or key):
            logger.info("[OK] MIS loss limit column and config key are in place")
        return not (column or key)
    except Exception:
        logger.exception("Status check failed")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=f"Migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--status", action="store_true", help="Check migration status")

    args = parser.parse_args()
    success = status() if args.status else upgrade()
    sys.exit(0 if success else 1)

#!/usr/bin/env python
"""
WhatsApp Updates Chat Migration Script for OpenAlgo

Sending /update-here from the owner's phone in any WhatsApp chat or group makes
that chat the destination for every update meant for the owner (order alerts,
/notify with self, chart alerts, Flow's WhatsApp node), instead of their own
chat.

Changes:
- Adds whatsapp_config.update_jid (nullable VARCHAR(120)): the chat
  /update-here named. NULL for every existing install, which is the truth:
  updates have always gone to the owner's own chat, and keep going there until
  someone sends /update-here.

Idempotent - safe to run multiple times.

Usage:
    cd upgrade
    uv run migrate_whatsapp_update_chat.py           # Apply migration
    uv run migrate_whatsapp_update_chat.py --status  # Check status

Migration: 008
Created: 2026-10-26
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

MIGRATION_NAME = "whatsapp_update_chat"
MIGRATION_VERSION = "008"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))


def get_main_db_engine():
    """Get the main database engine, where whatsapp_config lives"""
    db_url = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")

    if db_url.startswith("sqlite:///"):
        db_path = db_url.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = os.path.join(parent_dir, db_path)
        db_url = f"sqlite:///{db_path}"
        logger.info(f"Main DB path: {db_path}")

    return create_engine(db_url)


def _columns(conn):
    return {row[1] for row in conn.execute(text("PRAGMA table_info(whatsapp_config)"))}


def upgrade():
    """Add the column where it is missing."""
    try:
        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")
        engine = get_main_db_engine()
        with engine.connect() as conn:
            columns = _columns(conn)
            if not columns:
                logger.info(
                    "WhatsApp is not set up yet; the app creates its table with this in place"
                )
            elif "update_jid" in columns:
                logger.info("Nothing to do: whatsapp_config.update_jid is already in place")
            else:
                conn.execute(text("ALTER TABLE whatsapp_config ADD COLUMN update_jid VARCHAR(120)"))
                conn.commit()
                logger.info("Added whatsapp_config.update_jid (updates stay in your own chat)")
        logger.info(f"Migration {MIGRATION_NAME} completed successfully")
        return True
    except Exception:
        logger.exception(f"Migration {MIGRATION_NAME} failed")
        return False


def status():
    """Report what upgrade() would change, changing nothing."""
    try:
        engine = get_main_db_engine()
        with engine.connect() as conn:
            columns = _columns(conn)
        if not columns:
            logger.info(
                "[OK] WhatsApp is not set up yet; the app creates its table with this in place"
            )
            return True
        if "update_jid" not in columns:
            logger.info("[PENDING] column whatsapp_config.update_jid")
            return False
        logger.info("[OK] whatsapp_config.update_jid is in place")
        return True
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

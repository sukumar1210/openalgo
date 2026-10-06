#!/usr/bin/env python3
"""
Migration: Research (/research)

Creates the research database tables, all prefixed ``research_`` or
``ledger_``, in their own file (RESEARCH_DATABASE_URL, default db/research.db):
strategy versions, config versions, experiments, scenarios, runs, datasets,
artifacts, and the per-run ledger.

Every table is new, in a database that did not exist before, so there is
nothing to backfill and no value an operator could have customised. init_db()
in database/research_db.py creates them too; this script exists so the
upgrade path (`cd upgrade && uv run migrate_all.py`) reports the same thing the
app would do, and so the next schema change has a script to extend.

The DDL is read from the ORM metadata rather than written out by hand, as in
migrate_agent.py: a hand-written CREATE TABLE that drifts from the model is the
worst kind of bug, because the app starts and one column silently holds the
wrong thing.

Column drift on a table that already exists is reported and refused, never
guessed at. A later change to one of these tables ships its own script with an
ALTER, or a rebuild where SQLite cannot add the column in place (see CLAUDE.md
on migrations).

This migration is idempotent - safe to run multiple times.

Usage:
    cd upgrade
    uv run migrate_research.py           # Apply migration
    uv run migrate_research.py --status  # Check status without changing anything
"""

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Add parent directory to path for imports
sys.path.insert(0, PROJECT_ROOT)
# Register the app's SQLite pragmas on this process's engines, so a migration
# waits the same 15s for a write lock the running app does (GitHub issue #1726).
import _pragmas  # noqa: F401,E402
from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402


def resolve_sqlite_path(db_url):
    """Make a relative sqlite:/// path absolute against the project root.

    The documented invocation runs from upgrade/, where a relative path would
    point at upgrade/db/research.db, a file SQLAlchemy creates empty on
    connect. The migration would then succeed against a database the app never
    opens.
    """
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return db_url
    path = db_url[len(prefix) :]
    if os.path.isabs(path):
        return db_url
    return prefix + os.path.join(PROJECT_ROOT, path).replace("\\", "/")


def get_database_url():
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    return resolve_sqlite_path(os.getenv("RESEARCH_DATABASE_URL", "sqlite:///db/research.db"))


def load_metadata():
    """The research ORM metadata. Importing it builds a lazy engine and opens nothing."""
    from database.research_db import Base

    return Base.metadata


def plan(engine):
    """What apply would do: missing tables, and drift it will refuse to fix."""
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    metadata = load_metadata()
    missing = sorted(name for name in metadata.tables if name not in present)
    drift = []
    for name in sorted(present & set(metadata.tables)):
        live = {column["name"] for column in inspector.get_columns(name)}
        for column in metadata.tables[name].columns:
            if column.name not in live:
                drift.append(f"{name}.{column.name}")
    return missing, drift


def status(engine, db_url):
    """Report what would change, without changing anything."""
    prefix = "sqlite:///"
    if db_url.startswith(prefix) and not os.path.exists(db_url[len(prefix) :]):
        print("  [PENDING] research.db does not exist yet; apply will create it")
        return False
    missing, drift = plan(engine)
    for name in missing:
        print(f"  [PENDING] table {name}")
    for item in drift:
        print(f"  [DRIFT] column {item} is missing and needs its own migration")
    if not missing and not drift:
        print("  [OK] Up to date. Nothing to do.")
    return not missing and not drift


def apply(engine):
    missing, drift = plan(engine)
    if drift:
        for item in drift:
            print(f"  [FAIL] column {item} is missing; this script does not alter tables")
        return False
    if not missing:
        print("  [SKIP] All research tables already exist")
        return True
    metadata = load_metadata()
    metadata.create_all(bind=engine, tables=[metadata.tables[name] for name in missing])
    for name in missing:
        print(f"  [CREATE] {name}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Research tables migration")
    parser.add_argument("--status", action="store_true", help="Report without changing anything")
    args = parser.parse_args()

    print()
    print("Research Migration")
    print("-" * 46)

    try:
        db_url = get_database_url()
        print(f"Database: {db_url.split('://')[0]}://...")
        if "sqlite" in db_url:
            path = db_url[len("sqlite:///") :]
            if not args.status:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            engine = create_engine(db_url, poolclass=NullPool)
        else:
            engine = create_engine(db_url)

        if args.status:
            return 0 if status(engine, db_url) else 1

        if not apply(engine):
            return 1
        print()
        print("[OK] Research migration completed successfully!")
        return 0
    except Exception as e:
        print(f"\n[ERROR] Migration failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

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

Drift on a table that already exists is read from the same metadata. A missing
column is added in place when SQLite allows it (nullable, or with a constant
default); a missing index is created. Anything SQLite cannot add in place (a
new primary key, a UNIQUE column, NOT NULL with no default) is reported and
refused, because the fix is a table rebuild with a backfill and a migration
must not report success it did not earn (see CLAUDE.md on migrations).

The Sandbox release (Phase 8) adds, to a database created by the backtest
release: ``research_deployments``, ``ledger_events``, ``research_session_bars``;
and on ``research_runs`` the columns ``environment`` (NOT NULL DEFAULT
'BACKTEST', which is exactly what every existing row is), ``deployment_id``,
``trading_date``, ``lease_owner``, ``heartbeat_at``, ``live_state`` and
``provenance`` (all nullable), plus the index ``uq_research_session_day``.
Nothing existing is rewritten.

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
from sqlalchemy.schema import CreateIndex  # noqa: E402


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


def _default_literal(column):
    """The constant to fill existing rows with, or None when there is none."""
    if column.server_default is not None:
        arg = getattr(column.server_default, "arg", None)
        if arg is None:
            return None
        text = str(getattr(arg, "text", arg))
        return text if text.startswith("'") else "'" + text.replace("'", "''") + "'"
    return None


def column_add_clause(engine, column):
    """The ADD COLUMN clause, or None when SQLite cannot add this column in place."""
    if column.primary_key or column.unique:
        return None
    spec = engine.dialect.ddl_compiler(engine.dialect, None).get_column_specification(column)
    if column.nullable or " DEFAULT " in spec.upper():
        return spec
    literal = _default_literal(column)
    return None if literal is None else f"{spec} DEFAULT {literal}"


def plan(engine):
    """What apply would do: (missing tables, columns, indexes, refused)."""
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    metadata = load_metadata()
    missing = sorted(name for name in metadata.tables if name not in present)
    columns, indexes, refused = [], [], []
    for name in sorted(present & set(metadata.tables)):
        table = metadata.tables[name]
        live = {column["name"] for column in inspector.get_columns(name)}
        for column in table.columns:
            if column.name in live:
                continue
            clause = column_add_clause(engine, column)
            if clause is None:
                refused.append(f"{name}.{column.name}")
            else:
                columns.append((name, column.name, clause))
        live_indexes = {item["name"] for item in inspector.get_indexes(name)}
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            if index.name not in live_indexes:
                indexes.append((name, index.name, str(CreateIndex(index).compile(engine)).strip()))
    return missing, columns, indexes, refused


def status(engine, db_url):
    """Report what would change, without changing anything."""
    prefix = "sqlite:///"
    if db_url.startswith(prefix) and not os.path.exists(db_url[len(prefix) :]):
        print("  [PENDING] research.db does not exist yet; apply will create it")
        return False
    missing, columns, indexes, refused = plan(engine)
    for name in missing:
        print(f"  [PENDING] table {name}")
    for table, name, _ in columns:
        print(f"  [PENDING] column {table}.{name}")
    for table, name, _ in indexes:
        print(f"  [PENDING] index {name} on {table}")
    for item in refused:
        print(f"  [REFUSED] column {item} cannot be added in place and needs its own migration")
    clean = not (missing or columns or indexes or refused)
    if clean:
        print("  [OK] Up to date. Nothing to do.")
    return clean


def apply(engine):
    missing, columns, indexes, refused = plan(engine)
    if refused:
        for item in refused:
            print(
                f"  [FAIL] column {item} cannot be added in place; this script will not rebuild it"
            )
        return False
    metadata = load_metadata()
    if missing:
        metadata.create_all(bind=engine, tables=[metadata.tables[name] for name in missing])
        for name in missing:
            print(f"  [CREATE] {name}")
    # Columns before indexes: an index over a column that is not there yet
    # cannot be created, and a partly applied run must finish on the next one.
    for table, name, clause in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {clause}")
        print(f"  [ADD] column {table}.{name}")
    for table, name, ddl in indexes:
        with engine.begin() as connection:
            connection.exec_driver_sql(ddl)
        print(f"  [ADD] index {name} on {table}")
    if not (missing or columns or indexes):
        print("  [SKIP] All research tables, columns and indexes already exist")
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

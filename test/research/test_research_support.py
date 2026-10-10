"""
The supporting pieces: intraday costs, dataset identity, metrics rules, and the
research migration.
"""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from portfolio.costs import india_intraday, schedule_for
from research.dataset import DatasetError, DatasetSpec, dataset_hash, read_snapshot, write_snapshot
from research.metrics import max_drawdown, summarize

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_intraday_costs_match_a_contract_note_by_hand():
    """Rs 1,00,000 bought and sold intraday on NSE, one order each way."""
    schedule = india_intraday("NSE")
    buy = schedule.breakdown(100_000.0, 0.0, orders=1)
    sell = schedule.breakdown(0.0, 100_000.0, orders=1)

    # Brokerage: 0.03% of 1,00,000 is Rs 30, capped at Rs 20 per order.
    assert buy["brokerage"] == pytest.approx(20.0)
    assert buy["stt"] == 0.0 and sell["stt"] == pytest.approx(25.0)
    assert buy["stamp_duty"] == pytest.approx(3.0) and sell["stamp_duty"] == 0.0
    assert buy["exchange_txn"] == pytest.approx(3.07)
    assert buy["sebi"] == pytest.approx(0.10)
    assert buy["tax"] == pytest.approx(0.18 * (20.0 + 3.07 + 0.10))
    # A small order pays the percentage, not the cap.
    assert india_intraday().breakdown(10_000.0, 0.0, orders=1)["brokerage"] == pytest.approx(3.0)
    assert schedule_for("india_intraday_bse").name == "India intraday (BSE)"


def _frame(closes):
    n = len(closes)
    return pd.DataFrame(
        {
            "timestamp": np.arange(n, dtype="int64") * 300 + 1_700_000_000,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * n,
        }
    )


def test_dataset_hash_follows_the_bar_values_not_the_file(tmp_path):
    frames = {"AAA": _frame([1.0, 2.0, 3.0]), "BBB": _frame([5.0, 4.0, 3.0])}
    reordered = {"BBB": frames["BBB"], "AAA": frames["AAA"]}
    assert dataset_hash(frames) == dataset_hash(reordered)

    path = tmp_path / "snap.parquet"
    write_snapshot(frames, path)
    assert dataset_hash(read_snapshot(path)) == dataset_hash(frames)

    changed = {"AAA": _frame([1.0, 2.0, 3.0001]), "BBB": frames["BBB"]}
    assert dataset_hash(changed) != dataset_hash(frames)


def test_dataset_spec_refuses_a_backwards_window():
    with pytest.raises(DatasetError, match="end date"):
        DatasetSpec(("AAA",), "NSE", "5m", "2024-02-01", "2024-01-01")
    spec = DatasetSpec(("AAA",), "NSE", "5m", "2024-01-01", "2024-01-01")
    start, end = spec.epoch_window()
    assert end - start == 86_400 - 1


def test_drawdown_uses_every_bar_and_cagr_needs_a_year():
    equity = np.array([100.0, 120.0, 90.0, 130.0])
    assert max_drawdown(equity) == pytest.approx(90.0 / 120.0 - 1.0)

    day = 86_400
    short = np.arange(30) * day + 1_700_000_000
    curve = 100_000 * np.cumprod(1 + np.linspace(-0.01, 0.012, 30))
    metrics = summarize(short, curve, 100_000.0, [], 0.0)
    assert metrics["cagr_pct"] is None
    assert metrics["sharpe"] is not None

    long = np.arange(400) * day + 1_700_000_000
    curve = 100_000 * np.cumprod(1 + np.sin(np.arange(400)) * 0.01 + 0.0005)
    assert summarize(long, curve, 100_000.0, [], 0.0)["cagr_pct"] is not None


def _migrate(env, *args):
    return subprocess.run(
        [sys.executable, "migrate_research.py", *args],
        cwd=PROJECT_ROOT / "upgrade",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_migration_is_idempotent_and_status_reports_honestly(tmp_path):
    import os

    env = dict(os.environ, RESEARCH_DATABASE_URL=f"sqlite:///{tmp_path / 'research.db'}")

    before = _migrate(env, "--status")
    assert before.returncode == 1 and "does not exist yet" in before.stdout
    assert not (tmp_path / "research.db").exists(), "--status must not create the file"

    first = _migrate(env)
    assert first.returncode == 0 and "[CREATE] research_runs" in first.stdout
    second = _migrate(env)
    assert second.returncode == 0 and "[SKIP]" in second.stdout

    after = _migrate(env, "--status")
    assert after.returncode == 0 and "Up to date" in after.stdout


PHASE4_RUNS_DDL = [
    """CREATE TABLE research_runs (
        id INTEGER NOT NULL, scenario_id INTEGER NOT NULL, run_hash VARCHAR(64),
        status VARCHAR(12) NOT NULL, progress FLOAT NOT NULL, message TEXT, pid INTEGER,
        engine_version VARCHAR(20), engine_hash VARCHAR(64), metrics JSON, attribution JSON,
        queued_at DATETIME NOT NULL, started_at DATETIME, finished_at DATETIME,
        PRIMARY KEY (id), FOREIGN KEY(scenario_id) REFERENCES research_scenarios (id))""",
    "CREATE INDEX ix_research_runs_scenario ON research_runs (scenario_id, id)",
    "CREATE UNIQUE INDEX uq_research_run_hash_completed ON research_runs (run_hash) "
    "WHERE status = 'COMPLETED'",
]
SANDBOX_TABLES = {"research_deployments", "ledger_events", "research_session_bars"}


def test_the_migration_brings_a_populated_backtest_release_database_forward(tmp_path):
    """A research.db from the backtest release, with a completed run in it."""
    import os
    import sqlite3

    from sqlalchemy import create_engine

    from database.research_db import Base

    path = tmp_path / "research.db"
    engine = create_engine(f"sqlite:///{path}")
    old_tables = [
        t for name, t in Base.metadata.tables.items()
        if name not in SANDBOX_TABLES and name != "research_runs"
    ]  # fmt: skip
    Base.metadata.create_all(engine, tables=old_tables)
    engine.dispose()
    with sqlite3.connect(path) as db:
        for ddl in PHASE4_RUNS_DDL:
            db.execute(ddl)
        db.execute(
            "INSERT INTO research_runs (id, scenario_id, run_hash, status, progress, queued_at) "
            "VALUES (7, 1, 'abc', 'COMPLETED', 1.0, '2026-10-06 10:00:00')"
        )
    env = dict(os.environ, RESEARCH_DATABASE_URL=f"sqlite:///{path}")

    pending = _migrate(env, "--status")
    assert pending.returncode == 1
    assert "column research_runs.environment" in pending.stdout
    assert "table ledger_events" in pending.stdout

    applied = _migrate(env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert _migrate(env).returncode == 0
    assert _migrate(env, "--status").returncode == 0

    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT id, status, run_hash, environment, deployment_id FROM research_runs"
        ).fetchall()
        indexes = {r[1] for r in db.execute("PRAGMA index_list(research_runs)")}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert row == [(7, "COMPLETED", "abc", "BACKTEST", None)]
    assert "uq_research_session_day" in indexes
    assert SANDBOX_TABLES <= tables


def test_the_migration_backfills_dataset_facts_from_their_snapshots(tmp_path):
    """Data-derived, guarded, idempotent; a missing snapshot is reported, not invented."""
    import os
    import sqlite3

    import pandas as pd

    from research.dataset import write_snapshot

    frames = {
        s: pd.DataFrame({"timestamp": [1704080700 + 300 * n for n in range(3)], "open": 1.0,
                         "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
        for s in ("AAA", "BBB")
    }  # fmt: skip
    snapshot = tmp_path / "snap.parquet"
    write_snapshot(frames, snapshot)
    path = tmp_path / "research.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE research_datasets (id INTEGER PRIMARY KEY, dataset_hash VARCHAR(64) "
            "NOT NULL UNIQUE, spec JSON NOT NULL, bar_count INTEGER NOT NULL, uri VARCHAR(500) "
            "NOT NULL, size_bytes BIGINT NOT NULL, created_at DATETIME NOT NULL)"
        )
        db.execute(
            "INSERT INTO research_datasets VALUES (1, 'h1', '{}', 6, ?, 10, '2026-10-01')",
            (str(snapshot),),
        )
        db.execute(
            "INSERT INTO research_datasets VALUES (2, 'h2', '{}', 6, ?, 10, '2026-10-01')",
            (str(tmp_path / "gone.parquet"),),
        )
    env = dict(os.environ, RESEARCH_DATABASE_URL=f"sqlite:///{path}")

    assert "backfill dataset facts once their columns exist" in _migrate(env, "--status").stdout
    applied = _migrate(env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "dataset 2: snapshot" in applied.stdout and "is missing" in applied.stdout
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT id, source, symbol_count, first_ts, last_ts FROM research_datasets ORDER BY id"
        ).fetchall()
    assert rows == [(1, "historify", 2, 1704080700, 1704081300), (2, "historify", None, None, None)]
    again = _migrate(env)
    assert (
        again.returncode == 0 and "[BACKFILL]" not in again.stdout and "[WARN]" not in again.stdout
    )
    assert _migrate(env, "--status").returncode == 0, "a missing snapshot does not stay pending"


def test_existing_deployments_come_through_the_migration_as_sandbox(tmp_path):
    """Every deployment before Shadow and Live existed was a Sandbox one, and says so."""
    import os
    import sqlite3

    path = tmp_path / "research.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE research_deployments (id INTEGER PRIMARY KEY, scenario_id INTEGER NOT "
            "NULL, name VARCHAR(200) NOT NULL, user_id VARCHAR(100) NOT NULL, status VARCHAR(10) "
            "NOT NULL, schedule_start VARCHAR(5) NOT NULL, schedule_stop VARCHAR(5) NOT NULL, "
            "schedule_days JSON NOT NULL, stale_policy VARCHAR(10) NOT NULL, created_at DATETIME "
            "NOT NULL, stopped_at DATETIME)"
        )
        db.execute(
            "INSERT INTO research_deployments VALUES (1, 1, 'd', 'u', 'ACTIVE', '09:10', '15:25', "
            "'[\"mon\"]', 'PAUSE', '2026-10-08', NULL)"
        )
    env = dict(os.environ, RESEARCH_DATABASE_URL=f"sqlite:///{path}")

    assert "column research_deployments.environment" in _migrate(env, "--status").stdout
    assert _migrate(env).returncode == 0
    assert _migrate(env, "--status").returncode == 0
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT environment, capital_limit, confirmed_at FROM research_deployments"
        ).fetchone()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert row == ("SANDBOX", None, None)
    assert {"research_live_orders", "research_live_approvals", "research_controls"} <= tables

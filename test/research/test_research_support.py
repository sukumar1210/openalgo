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

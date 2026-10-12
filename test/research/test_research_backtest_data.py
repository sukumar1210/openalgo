"""
A backtest fetches the bars it needs instead of failing on an empty Historify.

The first run of a scenario reads Historify once and freezes what it read. When
Historify does not hold a symbol over the window, the run asks Historify to
download it from the broker, waits, and starts when the download ends. What
these tests pin: a run waiting for data is never started early; a finished
download releases it; a failed one fails it saying what to do; a stored range
that only misses a weekend at either end counts as covering the window; and a
pinned scenario never downloads anything.
"""

import pytest
import test_research_runs as runs
from test_research_runs import save_config

from research.strategy import IST_OFFSET_SECONDS

#: The run tests' service, database and registry, shared.
env = runs.env

DAY = 86400
JAN_1 = 1704067200 - IST_OFFSET_SECONDS  # 2024-01-01 00:00 IST, a Monday


@pytest.fixture
def historify(monkeypatch):
    """Historify's catalogue and download jobs, held in memory."""
    from database import historify_db
    from services import historify_service

    state = {"ranges": {}, "jobs": {}, "started": []}

    def start(job_type, symbols, interval, start_date, end_date, api_key, config=None):
        job_id = f"job{len(state['jobs']) + 1}"
        state["jobs"][job_id] = {"status": "running", "total_symbols": len(symbols)}
        state["started"].append((job_id, [s["symbol"] for s in symbols], interval, api_key))
        return True, {"job_id": job_id}, 200

    monkeypatch.setattr(historify_db, "get_data_range", lambda s, e, i: state["ranges"].get((s, i)))
    monkeypatch.setattr(historify_db, "get_download_job", lambda j: state["jobs"].get(j))
    monkeypatch.setattr(historify_service, "create_and_start_job", start)
    monkeypatch.setattr(
        "database.auth_db.get_api_key_for_tradingview", lambda user: f"key-of-{user}"
    )
    return state


def scenario(svc, rdb, strategy_id, version_id):
    experiment_id = svc.create_experiment("data")["id"]
    config_id = save_config(svc, strategy_id)
    return svc.create_scenario(
        experiment_id,
        {
            "name": "first run",
            "mode": "ISOLATED",
            "start": "2024-01-01",
            "end": "2024-01-31",
            "instances": [{"strategy_version_id": version_id, "config_version_id": config_id}],
        },
    )["id"]


def test_a_stored_range_missing_only_a_weekend_covers_the_window(env, historify):
    svc = env[0]
    spec = {
        "symbols": ["AAA", "BBB", "CCC"],
        "exchange": "NSE",
        "interval": "5m",
        "start": "2024-01-06",  # a Saturday
        "end": "2024-01-31",
    }
    end = JAN_1 + 31 * DAY
    historify["ranges"][("AAA", "1m")] = {"first_timestamp": JAN_1 + 7 * DAY, "last_timestamp": end}
    historify["ranges"][("BBB", "1m")] = {
        "first_timestamp": JAN_1 + 20 * DAY,
        "last_timestamp": end,
    }

    assert svc.missing_symbols(spec, now=end + DAY) == ["BBB", "CCC"]


def test_a_first_run_waits_for_its_download_and_starts_when_it_ends(env, historify, monkeypatch):
    svc, rdb, strategy_id, version_id = env
    spawned = []
    monkeypatch.setattr(svc, "_spawn", lambda run_id, *a, **k: spawned.append(run_id))
    scenario_id = scenario(svc, rdb, strategy_id, version_id)

    run, created = svc.enqueue_run(scenario_id, "trader")

    assert created and spawned == []
    [(job_id, symbols, interval, api_key)] = historify["started"]
    assert (symbols, interval, api_key) == (["AAA", "BBB"], "1m", "key-of-trader")
    row = rdb.db_session.get(rdb.ResearchRun, run["id"])
    assert row.data_job == job_id and "Downloading market data" in row.message

    historify["jobs"][job_id].update(completed_symbols=1, failed_symbols=0)
    svc.dispatch()
    rdb.db_session.expire_all()
    assert (
        spawned == [] and "1 of 2 symbols" in rdb.db_session.get(rdb.ResearchRun, run["id"]).message
    )

    historify["jobs"][job_id]["status"] = "completed_with_errors"
    svc.dispatch()
    assert spawned == [run["id"]]
    rdb.db_session.expire_all()
    assert rdb.db_session.get(rdb.ResearchRun, run["id"]).data_job is None


def test_a_download_that_fails_fails_the_run_saying_what_to_do(env, historify, monkeypatch):
    svc, rdb, strategy_id, version_id = env
    monkeypatch.setattr(svc, "_spawn", lambda *a, **k: pytest.fail("started without its data"))
    run, _ = svc.enqueue_run(scenario(svc, rdb, strategy_id, version_id), "trader")
    job_id = historify["started"][0][0]
    historify["jobs"][job_id].update(status="failed", error_message="Session expired")

    svc.dispatch()

    rdb.db_session.expire_all()
    row = rdb.db_session.get(rdb.ResearchRun, run["id"])
    assert row.status == "FAILED"
    assert "Session expired" in row.message and "logged in to your broker" in row.message


def test_a_run_with_its_data_stored_or_pinned_downloads_nothing(env, historify, monkeypatch):
    svc, rdb, strategy_id, version_id = env
    spawned = []
    monkeypatch.setattr(svc, "_spawn", lambda run_id, *a, **k: spawned.append(run_id))
    whole = {"first_timestamp": JAN_1, "last_timestamp": JAN_1 + 40 * DAY}
    historify["ranges"].update({("AAA", "1m"): whole, ("BBB", "1m"): whole})

    run, _ = svc.enqueue_run(scenario(svc, rdb, strategy_id, version_id), "trader")

    assert historify["started"] == [] and spawned == [run["id"]]

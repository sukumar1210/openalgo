"""
Sandbox deployments: validation, one session a day, the schedule, controls,
boot recovery, and the session worker's broker boundary (PRD Phase 8.1, 8.9,
8.10, 8.14; UAT PAPER-007).
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@pytest.fixture
def deploy_env(research_db, monkeypatch):
    from services import research_sandbox_service as sandbox
    from services import research_service as svc

    svc.sync_registry()
    strategy_id = research_db.ResearchStrategy.query.one().id
    version_id = research_db.ResearchStrategyVersion.query.one().id
    spawned = []
    monkeypatch.setattr(
        svc, "spawn_sandbox_session", lambda run_id, env: spawned.append((run_id, env)) or True
    )
    monkeypatch.setattr(sandbox, "_trading_today", lambda exchange: True)
    import database.auth_db as auth_db

    monkeypatch.setattr(auth_db, "get_api_key_for_tradingview", lambda user: "key-" + user)

    def scenario(product="MIS", mode="ISOLATED"):
        config, _ = svc.save_config(
            strategy_id,
            {"capital": 100000, "universe": ["AAA"], "product": product,
             "entry_time": "09:15", "exit_time": "15:10" if product == "MIS" else None},
        )  # fmt: skip
        exp = svc.create_experiment("e")["id"]
        return svc.create_scenario(
            exp,
            {"name": "s", "mode": mode, "start": "2026-01-01", "end": "2026-01-02",
             "instances": [{"strategy_version_id": version_id, "config_version_id": config["id"]}]},
        )["id"]  # fmt: skip

    today = WEEKDAYS[datetime.now(sandbox.IST).weekday()]
    yield sandbox, research_db, scenario, spawned, today


def test_only_intraday_scenarios_on_a_sane_schedule_can_be_deployed(deploy_env):
    sandbox, rdb, scenario, _spawned, _today = deploy_env
    with pytest.raises(sandbox.SandboxRefused, match="MIS"):
        sandbox.create_deployment("trader", {"scenario_id": scenario(product="CNC")})
    good = scenario()
    with pytest.raises(sandbox.SandboxRefused, match="after the start"):
        sandbox.create_deployment(
            "trader", {"scenario_id": good, "schedule_start": "15:00", "schedule_stop": "09:00"}
        )
    with pytest.raises(sandbox.SandboxRefused, match="mon to sun"):
        sandbox.create_deployment("trader", {"scenario_id": good, "schedule_days": ["funday"]})

    made = sandbox.create_deployment("trader", {"scenario_id": good})
    assert made["status"] == "ACTIVE" and made["schedule_days"] == [
        "mon",
        "tue",
        "wed",
        "thu",
        "fri",
    ]
    assert [d["id"] for d in sandbox.list_deployments("trader")] == [made["id"]]
    assert sandbox.list_deployments("someone-else") == []


def test_one_session_a_day_however_often_it_is_started(deploy_env):
    sandbox, rdb, scenario, spawned, today = deploy_env
    dep = sandbox.create_deployment(
        "trader",
        {
            "scenario_id": scenario(),
            "schedule_start": "00:00",
            "schedule_stop": "23:59",
            "schedule_days": [today],
        },
    )

    first = sandbox.open_session(dep["id"], scheduled=True)
    second = sandbox.start_now("trader", dep["id"])

    assert first["id"] == second["id"]
    assert rdb.ResearchRun.query.filter_by(deployment_id=dep["id"]).count() == 1
    assert spawned[0] == (
        first["id"],
        {"OPENALGO_API_KEY": "key-trader", "OPENALGO_HOST": spawned[0][1]["OPENALGO_HOST"]},
    )


def test_the_schedule_skips_unscheduled_days_and_closed_exchanges(deploy_env, monkeypatch):
    sandbox, rdb, scenario, spawned, today = deploy_env
    other = next(d for d in WEEKDAYS if d != today)
    dep = sandbox.create_deployment("trader", {"scenario_id": scenario(), "schedule_days": [other]})
    assert sandbox.open_session(dep["id"], scheduled=True) is None

    dep2 = sandbox.create_deployment(
        "trader",
        {
            "scenario_id": scenario(),
            "schedule_start": "00:00",
            "schedule_stop": "23:59",
            "schedule_days": [today],
        },
    )
    monkeypatch.setattr(sandbox, "_trading_today", lambda exchange: False)
    assert sandbox.open_session(dep2["id"], scheduled=True) is None
    with pytest.raises(sandbox.SandboxRefused, match="closed today"):
        sandbox.start_now("trader", dep2["id"])
    assert spawned == []


def test_stop_pause_and_resume_move_the_session_safely(deploy_env):
    sandbox, rdb, scenario, _spawned, today = deploy_env
    dep = sandbox.create_deployment(
        "trader",
        {
            "scenario_id": scenario(),
            "schedule_start": "00:00",
            "schedule_stop": "23:59",
            "schedule_days": [today],
        },
    )
    run_id = sandbox.start_now("trader", dep["id"])["id"]
    rdb.db_session.query(rdb.ResearchRun).filter_by(id=run_id).update(
        {"status": "RUNNING", "lease_owner": "w"}
    )
    rdb.db_session.commit()

    assert sandbox.control_session("trader", run_id, "pause")["status"] == "PAUSED"
    with pytest.raises(sandbox.SandboxRefused, match="not running"):
        sandbox.control_session("trader", run_id, "pause")
    assert sandbox.control_session("trader", run_id, "resume")["status"] == "RUNNING"
    with pytest.raises(sandbox.SandboxRefused, match="does not exist"):
        sandbox.control_session("someone-else", run_id, "stop")

    stopped = sandbox.stop_deployment("trader", dep["id"])
    assert stopped["status"] == "STOPPED"
    rdb.db_session.expire_all()
    assert rdb.db_session.get(rdb.ResearchRun, run_id).status == "STOPPING"


def test_a_backtest_style_cancel_is_refused_for_a_session(deploy_env):
    from services import research_service as svc

    sandbox, rdb, scenario, _spawned, today = deploy_env
    dep = sandbox.create_deployment(
        "trader",
        {
            "scenario_id": scenario(),
            "schedule_start": "00:00",
            "schedule_stop": "23:59",
            "schedule_days": [today],
        },
    )
    run_id = sandbox.start_now("trader", dep["id"])["id"]
    with pytest.raises(svc.ResearchError, match="deployment"):
        svc.cancel_run(run_id)


def test_boot_resumes_todays_sessions_and_settles_older_ones(deploy_env):
    sandbox, rdb, scenario, spawned, today = deploy_env
    dep = sandbox.create_deployment(
        "trader",
        {
            "scenario_id": scenario(),
            "schedule_start": "00:00",
            "schedule_stop": "23:59",
            "schedule_days": [today],
        },
    )
    session = rdb.db_session
    old = rdb.ResearchRun(scenario_id=dep["scenario_id"], status="RUNNING", environment="SANDBOX",
                          deployment_id=dep["id"], trading_date="2026-01-05")  # fmt: skip
    now = rdb.ResearchRun(scenario_id=dep["scenario_id"], status="RUNNING", environment="SANDBOX",
                          deployment_id=dep["id"], trading_date=sandbox._today())  # fmt: skip
    session.add_all([old, now])
    session.commit()

    sandbox.recover_sessions()

    session.expire_all()
    assert session.get(rdb.ResearchRun, old.id).status == "FAILED"
    assert "sandbox positions" in session.get(rdb.ResearchRun, old.id).message
    assert [run for run, _ in spawned] == [now.id]
    assert rdb.fail_orphaned_runs() == 0, "the backtest sweep leaves sessions alone"


def test_the_scheduler_holds_a_start_and_stop_job_per_active_deployment(deploy_env, monkeypatch):
    sandbox, rdb, scenario, _spawned, today = deploy_env
    monkeypatch.setattr(sandbox, "_scheduler", None)
    active = sandbox.create_deployment("trader", {"scenario_id": scenario()})
    stopped = sandbox.create_deployment("trader", {"scenario_id": scenario()})
    sandbox.stop_deployment("trader", stopped["id"])

    sandbox.start_research_sandbox()
    try:
        ids = {job.id for job in sandbox._scheduler.get_jobs()}
        assert ids == set(sandbox._job_ids(active["id"]))
        sandbox.stop_deployment("trader", active["id"])
        assert sandbox._scheduler.get_jobs() == []
    finally:
        sandbox._scheduler.shutdown(wait=False)


# --------------------------------------------------------------------------- #
# The session worker's broker boundary (PAPER-007)
# --------------------------------------------------------------------------- #

SESSION_PATH = (
    "research.sandbox_worker",
    "research.live",
    "research.engine",
    "research.metrics",
    "research.reference",
    "database.research_db",
    "services.websocket_client",
    "utils.httpx_client",
    "portfolio.costs",
    "portfolio.analytics",
)


def _python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60
    )


def test_a_session_worker_loads_nothing_that_can_place_an_order():
    code = (
        "import sys, importlib\n"
        "from research.sandbox_worker import sandbox_forbidden\n"
        "from research.worker import install_guard\n"
        "install_guard(sandbox_forbidden, where='a Sandbox session')\n"
        f"for name in {SESSION_PATH!r}: importlib.import_module(name)\n"
        "print(sorted(m for m in sys.modules if sandbox_forbidden(m)))\n"
    )
    result = _python(code)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "[]"


@pytest.mark.parametrize(
    "module",
    ["services.place_order_service", "sandbox.order_manager", "broker.zerodha.api.order_api",
     "services.sandbox_service", "database.auth_db", "restx_api.place_order"],
)  # fmt: skip
def test_a_session_cannot_reach_any_order_path(module):
    code = (
        "from research.sandbox_worker import sandbox_forbidden\n"
        "from research.worker import install_guard\n"
        "install_guard(sandbox_forbidden, where='a Sandbox session')\n"
        "try:\n"
        f"    import {module}\n"
        "except ImportError as exc:\n"
        "    print('refused:', exc)\n"
        "else:\n"
        "    print('IMPORTED')\n"
    )
    result = _python(code)
    # The guard may stop the parent package first (sandbox, broker, restx_api),
    # which is the stronger refusal; either way nothing on the path loads.
    assert "cannot be used inside a Sandbox session" in result.stdout, result.stdout + result.stderr
    assert module.startswith(result.stdout.split("refused: ")[1].split(" ")[0])
    assert "IMPORTED" not in result.stdout

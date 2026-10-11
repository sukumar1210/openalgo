"""
Runs end to end: queue, subprocess, ledger, provenance, failure isolation,
cancellation, deduplication, and the broker boundary (PRD UAT phases 2, 5, 6, 7,
8 and section 65).

These tests start real worker subprocesses against a temporary research
database, because the properties they pin only exist across a process
boundary: a scenario that fails must not touch another scenario's state, and a
backtest process must be unable to import the order path at all.
"""

import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

from research.dataset import bar_count, dataset_hash, write_snapshot
from research.strategy import IST_OFFSET_SECONDS

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def synthetic_frames(symbols=("AAA", "BBB"), days=3):
    """Deterministic 5m bars: AAA rises through each morning, BBB falls."""
    frames = {}
    base = 1704067200 - IST_OFFSET_SECONDS + (9 * 60 + 15) * 60  # 2024-01-01 09:15 IST
    stamps = [base + d * 86400 + 300 * n for d in range(days) for n in range(75)]
    for k, symbol in enumerate(symbols):
        closes = []
        for d in range(days):
            for n in range(75):
                drift = (1 if k % 2 == 0 else -1) * min(n, 10) * 0.2
                closes.append(100.0 + 10 * k + d + drift)
        opens = [closes[0], *closes[:-1]]
        frames[symbol] = pd.DataFrame(
            {
                "timestamp": stamps,
                "open": opens,
                "high": [max(o, c) for o, c in zip(opens, closes, strict=True)],
                "low": [min(o, c) for o, c in zip(opens, closes, strict=True)],
                "close": closes,
                "volume": [1000.0] * len(stamps),
            }
        )
    return frames


@pytest.fixture
def env(research_db, tmp_path, monkeypatch):
    """The service wired to the temporary database and data directory."""
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "log"))
    from services import research_service as svc

    svc.sync_registry()
    strategy_id = research_db.ResearchStrategy.query.one().id
    version_id = research_db.ResearchStrategyVersion.query.one().id
    yield svc, research_db, strategy_id, version_id
    # Run ids restart at 1 in every test's database, so a worker left over from
    # this test must be gone before the next one, or the module-level child
    # table would reap it against the wrong run.
    svc.terminate_all()
    with svc._lock:
        for child in svc._children.values():
            child["process"].wait(timeout=10)
        svc._children.clear()
    svc._shutting_down = False


def save_config(svc, strategy_id, **overrides):
    config = {
        "capital": 100000,
        "universe": ["AAA", "BBB"],
        "entry_time": "09:15",
        "exit_time": "15:10",
    }
    config.update(overrides)
    return svc.save_config(strategy_id, config)[0]["id"]


def make_scenario(
    svc, rdb, experiment_id, version_id, config_ids, mode="ISOLATED", frames=None, **kw
):
    scenario = svc.create_scenario(
        experiment_id,
        {
            "name": kw.get("name", mode),
            "mode": mode,
            "start": "2024-01-01",
            "end": "2024-01-03",
            "cost_model": {"preset": "india_intraday_nse", "slippage_bps": 2},
            "instances": [
                {"strategy_version_id": version_id, "config_version_id": c} for c in config_ids
            ],
            **kw.get("extra", {}),
        },
    )
    pin_dataset(rdb, scenario["id"], frames or synthetic_frames())
    return scenario["id"]


def pin_dataset(rdb, scenario_id, frames):
    """Stand in for the first Historify load: a snapshot plus its row."""
    from research.worker import data_root

    digest = dataset_hash(frames)
    path = data_root() / "datasets" / f"{digest}.parquet"
    session = rdb.db_session
    row = session.query(rdb.ResearchDataset).filter_by(dataset_hash=digest).first()
    if row is None:
        size = write_snapshot(frames, path)
        row = rdb.ResearchDataset(
            dataset_hash=digest,
            spec={},
            bar_count=bar_count(frames),
            uri=str(path),
            size_bytes=size,
        )
        session.add(row)
        session.flush()
    session.query(rdb.ResearchScenario).filter_by(id=scenario_id).update(
        {rdb.ResearchScenario.dataset_id: row.id}, synchronize_session=False
    )
    session.commit()


def wait_final(rdb, run_ids, timeout=50.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = rdb.db_session
        session.expire_all()
        rows = session.query(rdb.ResearchRun).filter(rdb.ResearchRun.id.in_(run_ids)).all()
        if rows and all(r.status in rdb.FINAL_STATUSES for r in rows):
            return {r.id: r for r in rows}
        time.sleep(0.2)
    raise AssertionError("runs did not finish in time")


def test_isolated_and_shared_runs_complete_with_a_reconciled_ledger(env):
    svc, rdb, strategy_id, version_id = env
    exp = svc.create_experiment("UAT")["id"]
    a = save_config(svc, strategy_id)
    b = save_config(svc, strategy_id, capital=50000)
    isolated = make_scenario(svc, rdb, exp, version_id, [a, b], "ISOLATED")
    shared = make_scenario(svc, rdb, exp, version_id, [a, b], "SHARED")

    runs = svc.enqueue_experiment(exp)
    final = wait_final(rdb, [r["id"] for r in runs])

    assert {r.status for r in final.values()} == {"COMPLETED"}, [r.message for r in final.values()]
    session = rdb.db_session
    for run in final.values():
        assert run.run_hash and run.engine_version and len(run.engine_hash) == 64
        trades = session.query(rdb.LedgerTrade).filter_by(run_id=run.id).all()
        assert trades, "the reference strategy trades every day"
        for row in run.attribution:
            net = sum(t.net_pnl for t in trades if t.instance == row["instance"])
            assert row["net_pnl"] == pytest.approx(net, abs=1e-6)
        assert run.metrics["net_pnl"] == pytest.approx(
            sum(row["net_pnl"] for row in run.attribution), abs=1e-6
        )
        artifact = session.query(rdb.ResearchArtifact).filter_by(run_id=run.id, kind="equity").one()
        assert Path(artifact.uri).stat().st_size == artifact.size_bytes
    assert final[runs[0]["id"]].run_hash != final[runs[1]["id"]].run_hash
    assert {isolated, shared} == {r["scenario_id"] for r in runs}

    from services import research_ledger_service as ledger

    for run in final.values():
        assert run.provenance["execution_environment"] == "BACKTEST"
        seeds = [i["seed"] for i in run.provenance["instances"]]
        assert len(set(seeds)) == len(seeds) == 2 and run.provenance["global_seed"]
        report = ledger.reconcile(run.id)
        assert run.metrics["final_equity"] and run.metrics["capital_used_pct"] is not None
        assert report["passed"], report
        assert all(c["passed"] for c in report["checks"]), report
        assert_journal_is_whole(ledger, run.id)

    configs = svc.get_strategy(strategy_id)["configs"]
    shown = {r["run_id"] for c in configs for r in c["results"]}
    assert shown == set(final), "each configuration lists the runs that used it"
    by_config = {c["id"]: c["results"] for c in configs}
    smaller = next(r for r in by_config[b] if r["mode"] == "ISOLATED")
    run = final[smaller["run_id"]]
    book = next(a for a in run.attribution if a["capital"] == 50000)
    assert smaller["net_pnl"] == book["net_pnl"], "its own book, not the portfolio"


def assert_journal_is_whole(ledger, run_id):
    """Every ledger row appears in the journal once, in order, with the PRD's fields."""
    from database import research_db as rdb

    entries, cursor = [], 0
    while cursor is not None:
        page = ledger.journal(run_id, cursor=cursor, limit=37)  # an awkward page size
        entries += page["items"]
        cursor = page["next_cursor"]
    seqs = [e["seq"] for e in entries]
    assert seqs == sorted(seqs)
    kinds = {e["kind"] for e in entries}
    assert {"SIGNAL", "TARGET_POSITION", "SQUARE_OFF", "ORDER_INTENT", "ORDER_SUBMITTED",
            "ORDER_FILLED", "FILL", "FEE", "POSITION_UPDATE", "REALIZED_PNL"} <= kinds  # fmt: skip
    session = rdb.db_session
    fills = session.query(rdb.LedgerFill).filter_by(run_id=run_id).count()
    trades = session.query(rdb.LedgerTrade).filter_by(run_id=run_id).count()
    assert sum(e["kind"] == "FILL" for e in entries) == fills
    assert sum(e["kind"] == "REALIZED_PNL" for e in entries) == trades
    fill = next(e for e in entries if e["kind"] == "FILL" and e["platform_order_id"])
    assert fill["strategy_version_id"] and fill["config_version_id"] and fill["experiment_id"]
    assert fill["platform_order_id"].startswith(f"research:{run_id}:")
    assert fill["execution_order_id"] == "simulated" and fill["requested_price"]
    target = next(e for e in entries if e["kind"] == "TARGET_POSITION")
    assert target["signal_id"] in {e["seq"] for e in entries if e["kind"] == "SIGNAL"}
    only_fills = ledger.journal(run_id, kinds={"FILL"}, limit=1000)["items"]
    assert {e["kind"] for e in only_fills} == {"FILL"}


def test_a_rerun_of_identical_inputs_returns_the_existing_result(env):
    svc, rdb, strategy_id, version_id = env
    exp = svc.create_experiment("Dedup")["id"]
    scenario = make_scenario(svc, rdb, exp, version_id, [save_config(svc, strategy_id)])

    first, created = svc.enqueue_run(scenario)
    assert created
    wait_final(rdb, [first["id"]])
    again, created = svc.enqueue_run(scenario)

    assert not created and again["id"] == first["id"]
    assert rdb.ResearchRun.query.count() == 1


def test_one_failing_scenario_does_not_touch_the_others(env):
    svc, rdb, strategy_id, version_id = env
    exp = svc.create_experiment("Failure isolation")["id"]
    config = save_config(svc, strategy_id)
    good_1 = make_scenario(svc, rdb, exp, version_id, [config], name="one")
    # The second scenario's pinned data is missing BBB, which its strategy trades.
    broken = make_scenario(
        svc, rdb, exp, version_id, [config], name="two", frames=synthetic_frames(("AAA",))
    )
    good_3 = make_scenario(svc, rdb, exp, version_id, [config], "SHARED", name="three")

    runs = {r["scenario_id"]: r["id"] for r in svc.enqueue_experiment(exp)}
    final = wait_final(rdb, list(runs.values()))

    assert final[runs[good_1]].status == "COMPLETED"
    assert final[runs[broken]].status == "FAILED"
    assert "BBB" in final[runs[broken]].message
    assert final[runs[good_3]].status == "COMPLETED"
    assert rdb.LedgerTrade.query.filter_by(run_id=runs[broken]).count() == 0
    assert rdb.LedgerTrade.query.filter_by(run_id=runs[good_1]).count() > 0


def test_a_cancelled_run_never_becomes_completed(env):
    svc, rdb, strategy_id, version_id = env
    exp = svc.create_experiment("Cancel")["id"]
    scenario = make_scenario(
        svc, rdb, exp, version_id, [save_config(svc, strategy_id)], frames=synthetic_frames(days=60)
    )

    run, _ = svc.enqueue_run(scenario)
    cancelled = svc.cancel_run(run["id"])
    final = wait_final(rdb, [run["id"]])

    assert cancelled["status"] == "CANCELLED"
    assert final[run["id"]].status == "CANCELLED"
    assert rdb.LedgerFill.query.filter_by(run_id=run["id"]).count() == 0


def test_an_old_run_keeps_its_configuration_after_the_config_changes(env):
    svc, rdb, strategy_id, version_id = env
    exp = svc.create_experiment("Provenance")["id"]
    v1 = save_config(svc, strategy_id)
    scenario = make_scenario(svc, rdb, exp, version_id, [v1])
    run, _ = svc.enqueue_run(scenario)
    wait_final(rdb, [run["id"]])

    save_config(svc, strategy_id, leverage=2.0)
    detail = svc.get_run(run["id"])

    config = detail["scenario"]["instances"][0]["config_version"]
    assert config["id"] == v1 and config["config"]["leverage"] == 1.0
    assert detail["dataset"]["dataset_hash"]
    used_by = {c["version_no"]: c["used_by"] for c in svc.get_strategy(strategy_id)["configs"]}
    assert used_by == {1: 1, 2: 0}


def test_startup_sweep_fails_runs_a_restart_orphaned(env):
    svc, rdb, *_ = env
    session = rdb.db_session
    exp = rdb.ResearchExperiment(name="x")
    session.add(exp)
    session.flush()
    scenario = rdb.ResearchScenario(
        experiment_id=exp.id, name="s", mode="ISOLATED", dataset_spec={}, cost_model={},
        execution_model={}, risk_policy={},
    )  # fmt: skip
    session.add(scenario)
    session.flush()
    session.add(rdb.ResearchRun(scenario_id=scenario.id, status="RUNNING"))
    session.commit()

    assert rdb.fail_orphaned_runs() == 1
    run = rdb.ResearchRun.query.one()
    assert run.status == "FAILED" and "restart" in run.message


def test_scenarios_refuse_mismatched_inputs(env):
    svc, rdb, strategy_id, version_id = env
    exp = svc.create_experiment("Validation")["id"]
    nse = save_config(svc, strategy_id)
    bse = save_config(svc, strategy_id, exchange="BSE")
    base = {
        "name": "x",
        "mode": "ISOLATED",
        "start": "2024-01-01",
        "end": "2024-01-03",
        "instances": [{"strategy_version_id": version_id, "config_version_id": nse}],
    }
    with pytest.raises(svc.ResearchError, match="same exchange"):
        svc.create_scenario(
            exp,
            dict(
                base,
                instances=base["instances"]
                + [{"strategy_version_id": version_id, "config_version_id": bse}],
            ),
        )
    with pytest.raises(svc.ResearchError, match="end date"):
        svc.create_scenario(exp, dict(base, end="2023-12-31"))
    with pytest.raises(svc.ResearchError, match="cost model"):
        svc.create_scenario(exp, dict(base, cost_model={"preset": "free_money"}))
    assert rdb.ResearchScenario.query.count() == 0


# --------------------------------------------------------------------------- #
# The broker boundary (PRD section 65)
# --------------------------------------------------------------------------- #

WORKER_PATH_MODULES = (
    "research.engine",
    "research.dataset",
    "research.metrics",
    "research.reference",
    "database.research_db",
    "database.historify_db",
    "portfolio.costs",
    "portfolio.analytics",
)


def _python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_backtest_path_imports_nothing_on_the_order_path():
    code = (
        "import sys, importlib\n"
        "from research.worker import install_guard, is_forbidden\n"
        "install_guard()\n"
        f"for name in {WORKER_PATH_MODULES!r}: importlib.import_module(name)\n"
        "print(sorted(m for m in sys.modules if is_forbidden(m)))\n"
    )
    result = _python(code)

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "[]"


def test_a_strategy_that_reaches_for_order_placement_is_refused():
    code = (
        "from research.worker import install_guard\n"
        "install_guard()\n"
        "try:\n"
        "    import services.place_order_service\n"
        "except ImportError as exc:\n"
        "    print('refused:', exc)\n"
        "else:\n"
        "    print('IMPORTED')\n"
    )
    result = _python(code)

    assert "refused: services.place_order_service cannot be used inside a backtest" in result.stdout
    assert "IMPORTED" not in result.stdout


def test_strategy_code_in_a_backtest_worker_cannot_open_a_connection():
    """Critical safety test 1, through ``python -m research.worker`` itself.

    The worker runs as ``__main__``, a second copy of its module. Had the scope
    and its audit hook lived there, the hook would watch a scope strategy code
    never enters, and nothing would be refused.
    """
    code = (
        "import runpy, socket, sys\n"
        "sys.argv = ['research.worker']\n"  # no run id: guard installed, then exit 2
        "try:\n"
        "    runpy.run_module('research.worker', run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 2, exc.code\n"
        "from research.strategy import StrategyRefused, strategy_code\n"
        "try:\n"
        "    with strategy_code():\n"
        "        socket.create_connection(('127.0.0.1', 9), timeout=1)\n"
        "except StrategyRefused as exc:\n"
        "    print('refused:', exc.action)\n"
    )
    result = _python(code)

    assert "refused: open a network connection" in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize(
    "name",
    ["broker.zerodha.api.order_api", "sandbox.order_manager", "services.place_smart_order_service",
     "services.sandbox_service", "database.auth_db", "blueprints.orders"],
)  # fmt: skip
def test_the_guard_covers_every_route_to_a_broker(name):
    from research.worker import is_forbidden

    assert is_forbidden(name)
    assert not is_forbidden("database.research_db")
    assert not is_forbidden("services.risk.position")


def test_workers_never_receive_the_broker_credentials(monkeypatch):
    """Master PRD section 46: strategy plugin code runs in a worker."""
    from services import research_service as svc

    for name in ("BROKER_API_KEY", "BROKER_API_SECRET", "BROKER_API_SECRET_MARKET", "APP_KEY",
                 "API_KEY_PEPPER", "FERNET_SALT", "OPENAI_API_KEY", "SMTP_PASSWORD"):  # fmt: skip
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("RESEARCH_DATABASE_URL", "sqlite:///db/research.db")

    env = svc._child_env()

    assert not [name for name in env if env[name] == "secret"]
    assert env["RESEARCH_DATABASE_URL"] == "sqlite:///db/research.db"
    assert env["OPENBLAS_NUM_THREADS"] == "1"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits")
@pytest.mark.parametrize(("kind", "cpu", "nice"), [("backtest", 3600, 10), ("sandbox", None, 0)])
def test_a_worker_gets_its_limits_after_exec_never_before(tmp_path, kind, cpu, nice):
    """The strategy host's pattern: limits set by the child, not in preexec_fn.

    preexec_fn runs Python between fork and exec, which hangs a child forked
    while another of the parent's threads held a lock (the gthread worker and
    the development server both have many). The module still runs as __main__.
    """
    import os
    import resource

    from services import research_service as svc

    (tmp_path / "probe.py").write_text(
        "import os, resource, sys\n"
        "if __name__ == '__main__':\n"
        "    print(resource.getrlimit(resource.RLIMIT_AS)[0], resource.getrlimit(resource.RLIMIT_CPU)[0],\n"
        "          resource.getrlimit(resource.RLIMIT_NOFILE)[0], os.nice(0), sys.argv[1:])\n"
    )
    env = {**os.environ, **svc._limits_env(kind), "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", svc._LIMITS_BOOTSTRAP, "probe", "41"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    memory, cpu_limit, files, niceness, argv = result.stdout.split(maxsplit=4)

    assert int(memory) == svc.MAX_MEMORY_MB * 1024 * 1024, result.stderr
    assert int(cpu_limit) == (cpu if cpu else resource.RLIM_INFINITY)
    assert int(files) == 256
    assert int(niceness) >= nice
    assert argv.strip() == "['41']"


def test_a_backtest_past_its_time_limit_is_stopped_saying_why(env):
    svc, rdb, strategy_id, version_id = env
    experiment_id = svc.create_experiment("timeout")["id"]
    scenario_id = make_scenario(
        svc, rdb, experiment_id, version_id, [save_config(svc, strategy_id)]
    )
    session = rdb.db_session
    run = rdb.ResearchRun(scenario_id=scenario_id, status="RUNNING")
    session.add(run)
    session.commit()

    class Running:
        killed = False

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

    process = Running()
    started = time.monotonic() - svc.JOB_TIMEOUT_SECONDS - 1
    svc._children[run.id] = {"process": process, "cancelled_at": None, "started_at": started,
                             "kind": "backtest"}  # fmt: skip
    try:
        with svc._lock:
            svc._reap_locked(session)  # past the limit: settled, then killed
            svc._reap_locked(session)  # reaped: the kill is not read as memory
    finally:
        svc._children.pop(run.id, None)

    session.expire_all()
    row = session.get(rdb.ResearchRun, run.id)
    assert process.killed and row.status == "FAILED"
    assert row.message.startswith("The backtest ran longer than its 120-minute limit")


def test_a_backtest_out_of_processor_time_is_told_so():
    import signal

    from services import research_service as svc

    if not hasattr(signal, "SIGXCPU"):
        pytest.skip("POSIX only")
    assert "minutes of processor time" in svc._exit_message(-signal.SIGXCPU)
    assert "ran out of memory" in svc._exit_message(-9)


def test_a_backtest_worker_refuses_the_network_for_its_whole_life():
    """Not only inside strategy code: a library or the platform cannot reach it either.

    A backtest reads its snapshot and writes files; it never needs the network.
    A Unix socket is a local file and is still allowed.
    """
    code = (
        "import runpy, socket, sys\n"
        "sys.argv = ['research.worker']\n"  # no run id: guards installed, then exit 2
        "try:\n"
        "    runpy.run_module('research.worker', run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 2, exc.code\n"
        "for attempt in (lambda: socket.create_connection(('127.0.0.1', 9), timeout=1),\n"
        "                lambda: socket.getaddrinfo('example.com', 443),\n"
        "                lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('127.0.0.1', 9))):\n"
        "    try:\n"
        "        attempt()\n"
        "        print('reached')\n"
        "    except PermissionError as exc:\n"
        "        print('refused:', type(exc).__name__)\n"
        "try:\n"
        "    socket.socket(socket.AF_UNIX).connect('/nonexistent/research.sock')\n"
        "except FileNotFoundError:\n"
        "    print('unix allowed')\n"
    )
    result = _python(code)

    assert result.stdout.split("\n")[:4] == ["refused: NetworkRefused"] * 3 + ["unix allowed"], (
        result.stdout + result.stderr
    )


def test_run_logs_past_their_retention_are_pruned_once_a_day(tmp_path, monkeypatch):
    import os

    from services import research_service as svc

    old, recent, other = (tmp_path / name for name in ("run_1.log", "run_2.log", "notes.log"))
    for path in (old, recent, other):
        path.write_text("x")
    long_ago = time.time() - (svc.LOG_RETENTION_DAYS + 1) * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    monkeypatch.setattr(svc, "_pruned_at", None)

    svc._prune_logs(tmp_path)
    assert not old.exists() and recent.exists() and other.exists()

    os.utime(recent, (long_ago, long_ago))
    svc._prune_logs(tmp_path)  # the same day: not again
    assert recent.exists()


def test_a_run_keeps_its_strategys_properties_as_they_were_when_it_ran(env):
    svc, rdb, strategy_id, version_id = env
    svc.set_version_properties(
        "trader", version_id,
        {"training_start": "2023-01-01", "training_end": "2023-12-31",
         "properties": [{"name": "Model", "value": "v1"}]},
    )  # fmt: skip
    experiment_id = svc.create_experiment("properties")["id"]
    make_scenario(svc, rdb, experiment_id, version_id, [save_config(svc, strategy_id)])
    (run,) = svc.enqueue_experiment(experiment_id)
    final = wait_final(rdb, [run["id"]])[run["id"]]
    svc.set_version_properties(
        "trader", version_id, {"properties": [{"name": "Model", "value": "v2"}]}
    )

    assert final.status == "COMPLETED", final.message
    (instance,) = final.provenance["instances"]
    assert instance["properties"]["training_end"] == "2023-12-31"
    assert instance["properties"]["properties"] == {"Model": "v1"}


def test_a_strategys_recorded_panel_is_kept_with_its_run_and_hashed(env):
    """PRD section 19: a model's per-symbol scores stay auditable beside its trades.

    The reference strategy records the first-bar move it ranks on, every day,
    for every symbol it could rank.
    """
    import hashlib

    svc, rdb, strategy_id, version_id = env
    experiment_id = svc.create_experiment("records")["id"]
    make_scenario(svc, rdb, experiment_id, version_id, [save_config(svc, strategy_id)])
    (run,) = svc.enqueue_experiment(experiment_id)
    final = wait_final(rdb, [run["id"]])[run["id"]]

    assert final.status == "COMPLETED", final.message
    artifact = (
        rdb.db_session.query(rdb.ResearchArtifact).filter_by(run_id=run["id"], kind="records").one()
    )
    data = Path(artifact.uri).read_bytes()
    assert (len(data), hashlib.sha256(data).hexdigest()) == (artifact.size_bytes, artifact.sha256)
    panel = pd.read_parquet(artifact.uri)
    assert list(panel.columns) == ["timestamp", "instance", "name", "symbol", "value"]
    assert set(panel["name"]) == {"first_bar_move"} and set(panel["symbol"]) == {"AAA", "BBB"}
    assert panel.groupby("timestamp").size().eq(2).all(), "one row per symbol per decision"


def test_a_run_that_does_not_complete_leaves_no_records_file(tmp_path):
    from research.worker import RecordWriter

    writer = RecordWriter(tmp_path / "records.parquet")
    writer(1, 0, "score", {"AAA": 1.0})
    writer.CHUNK = 1
    writer(2, 0, "score", {"AAA": 2.0})  # flushed: the file exists
    assert writer.path.exists()
    writer.discard()
    assert not writer.path.exists()
    assert RecordWriter(tmp_path / "none.parquet").close() is None, "nothing recorded, no file"

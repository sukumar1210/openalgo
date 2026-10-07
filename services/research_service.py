"""
Research service: the strategy registry, config versions, experiments and runs.

The blueprint translates HTTP; this module decides. Everything here works on
plain dicts at its boundary so a caller never holds an ORM object past the
session that produced it.

Registry sync is pull-based. ``sync_registry`` discovers every strategy this
process can load (built-ins plus installed ``openalgo.strategies`` packages),
computes each one's version identity, and records any version it has not seen
before. Nothing is ever updated: a changed package is a new version row, and the
old row stays exactly as it was so every run that used it still resolves to the
code that produced it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from database import research_db as rdb
from research import strategy as rs
from utils.logging import get_logger

logger = get_logger(__name__)


class ResearchError(ValueError):
    """A request the service refuses, with a message written for the trader."""


# --------------------------------------------------------------------------- #
# Strategy registry
# --------------------------------------------------------------------------- #


def sync_registry() -> list[dict]:
    """Record every loadable strategy and any new version of it.

    A strategy that fails to load is skipped and reported rather than allowed
    to hide the others: one broken package must not empty the registry page.

    Returns:
        One dict per problem found, ``{"key", "message"}``. Empty when every
        strategy loaded.
    """
    problems = []
    session = rdb.db_session
    for spec in rs.discover().values():
        try:
            identity = rs.version_identity(spec)
        except Exception:
            logger.exception("Research: could not load strategy %s", spec.key)
            problems.append(
                {
                    "key": spec.key,
                    "message": "This strategy could not be loaded. Its package may be "
                    "incomplete or out of date; the server log has the details.",
                }
            )
            continue
        try:
            row = session.query(rdb.ResearchStrategy).filter_by(key=spec.key).one_or_none()
            if row is None:
                row = rdb.ResearchStrategy(key=spec.key, source=spec.source)
                session.add(row)
                session.flush()
            exists = (
                session.query(rdb.ResearchStrategyVersion.id)
                .filter_by(strategy_id=row.id, version_hash=identity["version_hash"])
                .first()
            )
            if not exists:
                session.add(
                    rdb.ResearchStrategyVersion(
                        strategy_id=row.id,
                        version_hash=identity["version_hash"],
                        dist_name=identity["dist_name"],
                        dist_version=identity["dist_version"],
                        code_hash=identity["code_hash"],
                        artifact_hash=identity["artifact_hash"],
                        interval=identity["interval"],
                        meta=identity["meta"],
                    )
                )
            session.commit()
        except IntegrityError:
            # A concurrent sync recorded the same row first. That is the outcome
            # we wanted, so there is nothing to report.
            session.rollback()
    return problems


def list_strategies() -> dict:
    problems = sync_registry()
    session = rdb.db_session
    items = []
    for row in session.query(rdb.ResearchStrategy).order_by(rdb.ResearchStrategy.key):
        latest = (
            session.query(rdb.ResearchStrategyVersion)
            .filter_by(strategy_id=row.id)
            .order_by(rdb.ResearchStrategyVersion.id.desc())
            .first()
        )
        config = (
            session.query(rdb.ResearchConfigVersion)
            .filter_by(strategy_id=row.id)
            .order_by(rdb.ResearchConfigVersion.version_no.desc())
            .first()
        )
        items.append(
            {
                **rdb.strategy_to_dict(row),
                "latest_version": rdb.version_to_dict(latest) if latest else None,
                "latest_config": rdb.config_to_dict(config) if config else None,
            }
        )
    return {"items": items, "problems": problems}


def get_strategy(strategy_id: int) -> dict:
    session = rdb.db_session
    row = session.get(rdb.ResearchStrategy, strategy_id)
    if row is None:
        raise ResearchError("That strategy does not exist.")
    versions = (
        session.query(rdb.ResearchStrategyVersion)
        .filter_by(strategy_id=row.id)
        .order_by(rdb.ResearchStrategyVersion.id.desc())
        .all()
    )
    configs = (
        session.query(rdb.ResearchConfigVersion)
        .filter_by(strategy_id=row.id)
        .order_by(rdb.ResearchConfigVersion.version_no.desc())
        .all()
    )
    used_by = dict(
        session.query(
            rdb.ResearchScenarioInstance.config_version_id,
            func.count(func.distinct(rdb.ResearchScenarioInstance.scenario_id)),
        )
        .filter(rdb.ResearchScenarioInstance.config_version_id.in_([c.id for c in configs]))
        .group_by(rdb.ResearchScenarioInstance.config_version_id)
        .all()
    )
    return {
        **rdb.strategy_to_dict(row),
        "versions": [rdb.version_to_dict(v) for v in versions],
        "configs": [{**rdb.config_to_dict(c), "used_by": used_by.get(c.id, 0)} for c in configs],
    }


# --------------------------------------------------------------------------- #
# Config versions
# --------------------------------------------------------------------------- #


def save_config(
    strategy_id: int, data: dict, parent_id: int | None = None, note: str | None = None
) -> tuple[dict, bool]:
    """Save a configuration as a new version, or return the identical one.

    A configuration is never edited. Saving one that matches an existing
    version field for field returns that version instead of minting a
    duplicate, so the version history only grows when something really changed.

    Returns:
        ``(config_version, created)``.
    """
    try:
        config = rs.Config.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise ResearchError(str(exc)) from exc

    session = rdb.db_session
    if session.get(rdb.ResearchStrategy, strategy_id) is None:
        raise ResearchError("That strategy does not exist.")
    if parent_id is not None:
        parent = session.get(rdb.ResearchConfigVersion, parent_id)
        if parent is None or parent.strategy_id != strategy_id:
            raise ResearchError("The configuration this was based on does not exist.")

    config_hash = config.config_hash
    for _attempt in range(2):
        existing = (
            session.query(rdb.ResearchConfigVersion)
            .filter_by(strategy_id=strategy_id, config_hash=config_hash)
            .one_or_none()
        )
        if existing is not None:
            return rdb.config_to_dict(existing), False
        next_no = (
            session.query(func.coalesce(func.max(rdb.ResearchConfigVersion.version_no), 0))
            .filter_by(strategy_id=strategy_id)
            .scalar()
            + 1
        )
        row = rdb.ResearchConfigVersion(
            strategy_id=strategy_id,
            parent_id=parent_id,
            version_no=next_no,
            config=config.to_dict(),
            config_hash=config_hash,
            note=(note or "").strip()[:500] or None,
        )
        session.add(row)
        try:
            session.commit()
            return rdb.config_to_dict(row), True
        except IntegrityError:
            # Two saves raced for the same version number or the same config.
            # Re-read: the second pass either finds the identical config or
            # takes the next free number.
            session.rollback()
    raise ResearchError("The configuration could not be saved. Please try again.")


# --------------------------------------------------------------------------- #
# Experiments and scenarios
# --------------------------------------------------------------------------- #

MAX_SCENARIO_INSTANCES = 10


def create_experiment(name: str, description: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ResearchError("Give the experiment a name.")
    row = rdb.ResearchExperiment(
        name=name[:200], description=(description or "").strip()[:2000] or None
    )
    session = rdb.db_session
    session.add(row)
    session.commit()
    return rdb.experiment_to_dict(row)


def list_experiments() -> list[dict]:
    session = rdb.db_session
    out = []
    for row in session.query(rdb.ResearchExperiment).order_by(rdb.ResearchExperiment.id.desc()):
        scenario_ids = [
            s.id for s in session.query(rdb.ResearchScenario.id).filter_by(experiment_id=row.id)
        ]
        statuses = {}
        if scenario_ids:
            for status, count in (
                session.query(rdb.ResearchRun.status, func.count())
                .filter(rdb.ResearchRun.scenario_id.in_(scenario_ids))
                .group_by(rdb.ResearchRun.status)
            ):
                statuses[status] = count
        out.append(
            {**rdb.experiment_to_dict(row), "scenarios": len(scenario_ids), "runs": statuses}
        )
    return out


def _instance_dicts(session, scenario_id: int) -> list[dict]:
    out = []
    for inst in (
        session.query(rdb.ResearchScenarioInstance)
        .filter_by(scenario_id=scenario_id)
        .order_by(rdb.ResearchScenarioInstance.position)
    ):
        version = session.get(rdb.ResearchStrategyVersion, inst.strategy_version_id)
        strategy = session.get(rdb.ResearchStrategy, version.strategy_id)
        config = session.get(rdb.ResearchConfigVersion, inst.config_version_id)
        out.append(
            {
                "position": inst.position,
                "strategy_id": strategy.id,
                "strategy_key": strategy.key,
                "strategy_version": rdb.version_to_dict(version),
                "config_version": rdb.config_to_dict(config),
            }
        )
    return out


def get_experiment(experiment_id: int) -> dict:
    session = rdb.db_session
    row = session.get(rdb.ResearchExperiment, experiment_id)
    if row is None:
        raise ResearchError("That experiment does not exist.")
    scenarios = []
    for scenario in (
        session.query(rdb.ResearchScenario)
        .filter_by(experiment_id=row.id)
        .order_by(rdb.ResearchScenario.id)
    ):
        runs = (
            session.query(rdb.ResearchRun)
            .filter_by(scenario_id=scenario.id, environment=rdb.BACKTEST)
            .order_by(rdb.ResearchRun.id.desc())
            .all()
        )
        scenarios.append(
            {
                **rdb.scenario_to_dict(scenario),
                "instances": _instance_dicts(session, scenario.id),
                "runs": [rdb.run_to_dict(r) for r in runs],
            }
        )
    return {**rdb.experiment_to_dict(row), "scenarios": scenarios}


def create_scenario(experiment_id: int, data: dict) -> dict:
    """Validate and store one scenario. Scenarios are never edited afterwards.

    Everything the run will need is checked here, so a scenario that saves is
    one that can run: one bar interval across its strategies, one exchange
    across its configurations, a known cost schedule and a valid window. The
    dataset's symbols are the union of the instances' universes.
    """
    from portfolio.costs import PRESETS
    from research.dataset import DatasetError, DatasetSpec

    session = rdb.db_session
    if session.get(rdb.ResearchExperiment, experiment_id) is None:
        raise ResearchError("That experiment does not exist.")
    name = (data.get("name") or "").strip()
    if not name:
        raise ResearchError("Give the scenario a name.")
    mode = (data.get("mode") or "").upper()
    if mode not in rdb.MODES:
        raise ResearchError("Choose isolated or shared mode.")
    pairs = data.get("instances") or []
    if not isinstance(pairs, list) or not pairs:
        raise ResearchError("Add at least one strategy to the scenario.")
    if len(pairs) > MAX_SCENARIO_INSTANCES:
        raise ResearchError(f"A scenario can hold at most {MAX_SCENARIO_INSTANCES} strategies.")

    versions, configs = [], []
    for pair in pairs:
        version = session.get(rdb.ResearchStrategyVersion, _int(pair, "strategy_version_id"))
        config = session.get(rdb.ResearchConfigVersion, _int(pair, "config_version_id"))
        if version is None or config is None:
            raise ResearchError(
                "A strategy version or configuration in this scenario does not exist."
            )
        if version.strategy_id != config.strategy_id:
            raise ResearchError("Each configuration must belong to the strategy it is paired with.")
        versions.append(version)
        configs.append(config)

    intervals = {v.interval for v in versions}
    if len(intervals) != 1:
        raise ResearchError(
            "Every strategy in a scenario must use the same bar interval, because they share "
            "one clock."
        )
    exchanges = {c.config["exchange"] for c in configs}
    if len(exchanges) != 1:
        raise ResearchError("Every configuration in a scenario must use the same exchange.")
    symbols = sorted({s for c in configs for s in c.config["universe"]})
    try:
        spec = DatasetSpec(
            symbols=tuple(symbols),
            exchange=exchanges.pop(),
            interval=intervals.pop(),
            start=str(data.get("start") or ""),
            end=str(data.get("end") or ""),
        )
    except DatasetError as exc:
        raise ResearchError(str(exc)) from exc

    cost = data.get("cost_model") or {}
    preset = cost.get("preset", "india_intraday_nse")
    if preset not in PRESETS:
        raise ResearchError("Choose one of the listed cost models.")
    slippage = _number(cost.get("slippage_bps", 0), "Slippage", 0, 100)
    max_gross = _number(
        (data.get("risk_policy") or {}).get("max_gross_leverage", 1.0), "Exposure limit", 0.01, 10
    )
    shared_capital = data.get("shared_capital")
    if mode == "SHARED" and shared_capital is not None:
        shared_capital = _number(shared_capital, "Shared capital", 1, 1e12)
    else:
        shared_capital = None

    scenario = rdb.ResearchScenario(
        experiment_id=experiment_id,
        name=name[:200],
        mode=mode,
        dataset_spec=spec.to_dict(),
        cost_model={"preset": preset, "slippage_bps": slippage},
        execution_model={"fill": "next_open"},
        risk_policy={"max_gross_leverage": max_gross},
        shared_capital=shared_capital,
    )
    session.add(scenario)
    session.flush()
    for position, (version, config) in enumerate(zip(versions, configs, strict=True)):
        session.add(
            rdb.ResearchScenarioInstance(
                scenario_id=scenario.id,
                position=position,
                strategy_version_id=version.id,
                config_version_id=config.id,
            )
        )
    session.commit()
    return {**rdb.scenario_to_dict(scenario), "instances": _instance_dicts(session, scenario.id)}


def _int(data: dict, key: str) -> int:
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ResearchError("A strategy version or configuration in this scenario is not valid.")
    return value


def _number(value, label: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ResearchError(f"{label} must be a number.") from None
    if not low <= number <= high:
        raise ResearchError(f"{label} must be between {low:g} and {high:g}.")
    return number


# --------------------------------------------------------------------------- #
# Runs and the job runner
#
# Each run executes in its own subprocess (research/worker.py), never in the
# app process: a backtest is minutes of CPU, and the app serves everything from
# one eventlet worker that CPU work would freeze. The parent only spawns, polls
# and reaps. Polling uses poll() and sleep(), which yield under eventlet; a
# blocking wait() would stop the whole server.
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_CONCURRENT = max(1, int(os.getenv("RESEARCH_MAX_CONCURRENT", "2")))
MAX_MEMORY_MB = max(256, int(os.getenv("RESEARCH_MAX_MEMORY_MB", "2048")))
CANCEL_GRACE_SECONDS = 5.0
POLL_SECONDS = 0.5

_lock = threading.Lock()
_children: dict[int, dict] = {}  # run_id -> {"process": Popen, "cancelled_at": float | None}
_supervising = False
_hook_registered = False
_shutting_down = False


def _limit_child() -> None:  # pragma: no cover - runs in the child before exec
    try:
        import resource

        limit = MAX_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    except Exception:
        pass


def _child_env() -> dict:
    """The worker's environment: the app's, with numeric libraries single-threaded.

    A backtest is parallel across scenarios, one process each, not within one.
    Left alone, OpenBLAS starts a thread per core in every worker, and each
    thread reserves its own memory arena, which is enough to exhaust the memory
    limit before a single bar is read.

    PyArrow's default allocator does the same thing on a larger scale: writing
    one small parquet snapshot reserved 1.1 GB of address space that was never
    used, so the memory limit (an address-space limit, the only one the kernel
    enforces reliably) failed the next library import instead of a runaway
    backtest. The system allocator reserves only what it allocates.
    """
    env = dict(os.environ)
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        env[name] = "1"
    env["ARROW_DEFAULT_MEMORY_POOL"] = "system"
    return env


#: Worker module per kind of run. Backtests share the concurrency cap; Sandbox
#: sessions do not, because a session lives for a whole trading day and a cap
#: would leave a scheduled one waiting behind it.
WORKER_MODULES = {"backtest": "research.worker", "sandbox": "research.sandbox_worker"}
#: Times a crashed Sandbox worker is restarted in one session before the session
#: is given up as failed.
SANDBOX_RESTARTS = 3


def _spawn(run_id: int, kind: str = "backtest", env: dict | None = None) -> None:
    log_dir = PROJECT_ROOT / os.getenv("LOG_DIR", "log") / "research"
    log_dir.mkdir(parents=True, exist_ok=True)
    child_env = _child_env()
    child_env.update(env or {})
    with open(log_dir / f"run_{run_id}.log", "ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", WORKER_MODULES[kind], str(run_id)],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            env=child_env,
            preexec_fn=_limit_child if os.name == "posix" else None,
        )
    previous = _children.get(run_id, {})
    _children[run_id] = {
        "process": process,
        "cancelled_at": None,
        "kind": kind,
        "env": env or {},
        "restarts": previous.get("restarts", 0),
    }
    logger.info("Research: started %s run %s as pid %s", kind, run_id, process.pid)


def spawn_sandbox_session(run_id: int, env: dict) -> bool:
    """Start the worker for a Sandbox session. False if one is already running here."""
    with _lock:
        child = _children.get(run_id)
        if child is not None and child["process"].poll() is None:
            return False
        _spawn(run_id, "sandbox", env)
        _ensure_supervisor_locked()
    return True


def _dispatch_locked(session) -> None:
    free = MAX_CONCURRENT - sum(
        1 for c in _children.values() if c.get("kind", "backtest") == "backtest"
    )
    if free <= 0:
        return
    queued = (
        session.query(rdb.ResearchRun.id)
        .filter_by(status=rdb.QUEUED, environment=rdb.BACKTEST)
        .order_by(rdb.ResearchRun.id)
        .limit(MAX_CONCURRENT)
        .all()
    )
    for (run_id,) in queued:
        if free <= 0:
            break
        if run_id in _children:
            continue
        try:
            _spawn(run_id)
            free -= 1
        except Exception:
            logger.exception("Research: could not start run %s", run_id)
            _settle(
                session,
                run_id,
                "The backtest could not be started. The server log has the details.",
            )


def _settle(session, run_id: int, message: str) -> None:
    """Mark a run FAILED if its process ended without the worker settling it."""
    session.query(rdb.ResearchRun).filter(
        rdb.ResearchRun.id == run_id, rdb.ResearchRun.status.in_(rdb.ACTIVE_STATUSES)
    ).update(
        {
            rdb.ResearchRun.status: rdb.FAILED,
            rdb.ResearchRun.message: message,
            rdb.ResearchRun.finished_at: rdb.utc_now(),
        },
        synchronize_session=False,
    )
    session.commit()


def _reap_locked(session) -> None:
    now = time.monotonic()
    for run_id in sorted(_children):
        child = _children[run_id]
        process = child["process"]
        code = process.poll()
        if code is None:
            cancelled = child["cancelled_at"]
            if cancelled is not None and now - cancelled > CANCEL_GRACE_SECONDS:
                process.kill()
            continue
        del _children[run_id]
        if code == 0:
            continue
        if child.get("kind") == "sandbox":
            _restart_or_settle_session(session, run_id, child, code)
        else:
            _settle(session, run_id, _exit_message(code))


def _restart_or_settle_session(session, run_id: int, child: dict, code: int) -> None:
    """A Sandbox worker died. Restart it so it resumes, a few times, then give up.

    The new worker resumes from the ledger and reconciles every order with the
    sandbox, so a restart costs no duplicate orders; giving up marks the
    session FAILED and leaves its sandbox positions for the operator to check.
    """
    run = session.get(rdb.ResearchRun, run_id)
    session.refresh(run)
    if run is None or run.status not in rdb.ACTIVE_STATUSES or _shutting_down:
        return
    if child.get("restarts", 0) < SANDBOX_RESTARTS:
        _spawn(run_id, "sandbox", child.get("env"))
        _children[run_id]["restarts"] = child.get("restarts", 0) + 1
        logger.warning("Research: Sandbox session %s worker exited %s; restarted", run_id, code)
        return
    _settle(
        session,
        run_id,
        "The Sandbox session stopped and could not be restarted. Its positions may still be "
        "open in the sandbox; check the sandbox positions for its symbols. The session log has "
        "the details.",
    )


def _exit_message(code: int) -> str:
    """What to tell the trader about a worker that ended without settling its run.

    Only a SIGKILL is attributed to memory: it is what the kernel's out-of-memory
    killer sends, and nothing in this module sends it except a cancellation,
    which has already settled the run as CANCELLED before the kill.
    """
    if _shutting_down:
        return "Stopped because the server was shutting down. Run it again."
    if code == -9:
        return (
            "The system stopped the backtest, most likely because it ran out of memory. "
            "Try a shorter window or fewer symbols."
        )
    return "The backtest stopped unexpectedly. The run log has the details."


def _supervise() -> None:
    global _supervising
    from utils.db_sessions import session_cleanup

    while True:
        with session_cleanup():
            session = rdb.db_session
            with _lock:
                try:
                    _reap_locked(session)
                    _dispatch_locked(session)
                except Exception:
                    session.rollback()
                    logger.exception("Research: the run supervisor hit an error")
                if not _children:
                    _supervising = False
                    return
        time.sleep(POLL_SECONDS)


def _ensure_supervisor_locked() -> None:
    """Register the shutdown hook once and start one supervisor. Caller holds _lock.

    Both flags are decided under the same lock the supervisor clears
    ``_supervising`` under, so two dispatches cannot each start a supervisor or
    register the hook twice, and the supervisor cannot exit between a dispatch
    seeing it alive and that dispatch's new child needing it.
    """
    global _supervising, _hook_registered
    if not _hook_registered:
        from utils.shutdown import register_shutdown_hook

        register_shutdown_hook(terminate_all, name="research-runs", budget_s=5.0)
        _hook_registered = True
    if _supervising:
        return
    _supervising = True
    from utils.shared_executors import get_executor

    get_executor("research", 1).submit(_supervise)


def dispatch() -> None:
    """Start queued runs up to the concurrency limit, and the supervisor if needed."""
    session = rdb.db_session
    with _lock:
        _dispatch_locked(session)
        if _children:
            _ensure_supervisor_locked()


def terminate_all() -> None:
    """Shutdown hook: stop every backtest this process started."""
    global _shutting_down
    _shutting_down = True
    with _lock:
        for child in _children.values():
            try:
                child["process"].terminate()
            except Exception:
                pass


def enqueue_run(scenario_id: int) -> tuple[dict, bool]:
    """Queue a run of a scenario, or return the completed run it would repeat.

    A scenario whose data is already pinned has a known run_hash, so a rerun of
    identical inputs returns the existing result instead of computing it twice
    (PRD section 17). A scenario still loading its data the first time is not
    queued twice: two first runs racing would both pin data and the second would
    have nowhere to put an identical result.

    Returns:
        ``(run, created)``.
    """
    from research.worker import scenario_identity

    session = rdb.db_session
    scenario = session.get(rdb.ResearchScenario, scenario_id)
    if scenario is None:
        raise ResearchError("That scenario does not exist.")
    active = (
        session.query(rdb.ResearchRun)
        .filter(
            rdb.ResearchRun.scenario_id == scenario_id,
            rdb.ResearchRun.environment == rdb.BACKTEST,
            rdb.ResearchRun.status.in_(rdb.ACTIVE_STATUSES),
        )
        .first()
    )
    if active is not None:
        return rdb.run_to_dict(active), False
    if scenario.dataset_id:
        dataset = session.get(rdb.ResearchDataset, scenario.dataset_id)
        run_hash, _ = scenario_identity(session, scenario, dataset.dataset_hash)
        done = (
            session.query(rdb.ResearchRun)
            .filter_by(run_hash=run_hash, status=rdb.COMPLETED)
            .first()
        )
        if done is not None:
            return rdb.run_to_dict(done), False
    run = rdb.ResearchRun(scenario_id=scenario_id, status=rdb.QUEUED, message="Waiting to start")
    session.add(run)
    session.commit()
    result = rdb.run_to_dict(run)
    dispatch()
    return result, True


def enqueue_experiment(experiment_id: int) -> list[dict]:
    session = rdb.db_session
    if session.get(rdb.ResearchExperiment, experiment_id) is None:
        raise ResearchError("That experiment does not exist.")
    ids = [
        s.id
        for s in session.query(rdb.ResearchScenario.id)
        .filter_by(experiment_id=experiment_id)
        .order_by(rdb.ResearchScenario.id)
    ]
    if not ids:
        raise ResearchError("Add a scenario before running the experiment.")
    return [enqueue_run(scenario_id)[0] for scenario_id in ids]


def cancel_run(run_id: int) -> dict:
    session = rdb.db_session
    existing = session.get(rdb.ResearchRun, run_id)
    if existing is not None and existing.environment == rdb.SANDBOX:
        # Cancelling would abandon whatever the session holds. A session ends
        # through its deployment, which squares off first.
        raise ResearchError(
            "A Sandbox session is stopped from its deployment, which closes its positions first."
        )
    changed = (
        session.query(rdb.ResearchRun)
        .filter(rdb.ResearchRun.id == run_id, rdb.ResearchRun.status.in_(rdb.ACTIVE_STATUSES))
        .update(
            {
                rdb.ResearchRun.status: rdb.CANCELLED,
                rdb.ResearchRun.message: "Cancelled",
                rdb.ResearchRun.finished_at: rdb.utc_now(),
            },
            synchronize_session=False,
        )
    )
    session.commit()
    row = session.get(rdb.ResearchRun, run_id)
    if row is None:
        raise ResearchError("That run does not exist.")
    if changed:
        with _lock:
            child = _children.get(run_id)
            if child is not None:
                child["cancelled_at"] = time.monotonic()
                child["process"].terminate()
    session.refresh(row)
    return rdb.run_to_dict(row)


def get_run(run_id: int) -> dict:
    session = rdb.db_session
    row = session.get(rdb.ResearchRun, run_id)
    if row is None:
        raise ResearchError("That run does not exist.")
    scenario = session.get(rdb.ResearchScenario, row.scenario_id)
    dataset = session.get(rdb.ResearchDataset, scenario.dataset_id) if scenario.dataset_id else None
    return {
        **rdb.run_to_dict(row),
        "scenario": {
            **rdb.scenario_to_dict(scenario),
            "instances": _instance_dicts(session, scenario.id),
        },
        "dataset": None
        if dataset is None
        else {
            "dataset_hash": dataset.dataset_hash,
            "bar_count": dataset.bar_count,
            "spec": dataset.spec,
        },
        "artifacts": [
            {"kind": a.kind, "sha256": a.sha256, "size_bytes": a.size_bytes}
            for a in session.query(rdb.ResearchArtifact).filter_by(run_id=run_id)
        ],
    }


TRADES_PAGE = 500


def get_run_trades(run_id: int, instance: int | None = None, offset: int = 0) -> dict:
    session = rdb.db_session
    query = session.query(rdb.LedgerTrade).filter_by(run_id=run_id)
    if instance is not None:
        query = query.filter_by(instance=instance)
    total = query.count()
    rows = query.order_by(rdb.LedgerTrade.seq).offset(max(0, offset)).limit(TRADES_PAGE).all()
    keys = (
        "seq", "instance", "symbol", "side", "qty", "entry_ts", "entry_price", "exit_ts",
        "exit_price", "gross_pnl", "fees", "slippage", "net_pnl",
    )  # fmt: skip
    return {"total": total, "items": [{k: getattr(r, k) for k in keys} for r in rows]}


EQUITY_POINTS = 1500


def get_run_equity(run_id: int) -> dict:
    """The equity curves, thinned to at most EQUITY_POINTS points for the chart.

    Thinning keeps every point's real value (it samples, it never averages), and
    always keeps the last point so the curve ends where the run ended.
    """
    import pandas as pd

    session = rdb.db_session
    artifact = session.query(rdb.ResearchArtifact).filter_by(run_id=run_id, kind="equity").first()
    if artifact is None:
        raise ResearchError("This run has no equity curve yet.")
    frame = pd.read_parquet(artifact.uri)
    if len(frame) > EQUITY_POINTS:
        step = -(-len(frame) // EQUITY_POINTS)
        keep = list(range(0, len(frame), step))
        if keep[-1] != len(frame) - 1:
            keep.append(len(frame) - 1)
        frame = frame.iloc[keep]
    return {
        "timestamps": frame["timestamp"].astype("int64").tolist(),
        "series": {c: frame[c].round(2).tolist() for c in frame.columns if c != "timestamp"},
    }

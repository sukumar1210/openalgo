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
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from database import research_db as rdb
from research import strategy as rs
from utils.env_config import env_int
from utils.logging import get_logger

logger = get_logger(__name__)


class ResearchError(ValueError):
    """A request the service refuses, with a message written for the trader."""


#: The source of a Python strategy hosted at /python and followed here
#: (services.research_hosted_service).
SCRIPT_SOURCE = "script"


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
            if spec.source == "file":
                if _file_version_known(session, spec):
                    continue
                identity = identify_file(spec.key, Path(spec.target))
            else:
                identity = rs.version_identity(spec)
        except ResearchError as exc:
            problems.append({"key": spec.key, "message": str(exc)})
            continue
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
        _record_version(session, spec, identity)
    try:
        from services import research_hosted_service as hosted

        hosted.sync_scripts(session)
    except Exception:
        session.rollback()
        logger.exception("Research: could not read the followed Python strategies")
        problems.append(
            {
                "key": "Python strategies",
                "message": "The Python strategies followed here could not be read. The server "
                "log has the details.",
            }
        )
    return problems


def _record_version(session, spec: rs.StrategySpec, identity: dict) -> int:
    """Record a strategy and this version of it unless already there. Returns its id."""
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
                    model_hash=identity["model_hash"],
                    adapter_version=identity["adapter_version"],
                    interval=identity["interval"],
                    meta=identity["meta"],
                )
            )
        strategy_id = row.id
        session.commit()
    except IntegrityError:
        # A concurrent sync recorded the same row first. That is the outcome
        # we wanted, so there is nothing to report.
        session.rollback()
        strategy_id = session.query(rdb.ResearchStrategy.id).filter_by(key=spec.key).scalar()
    return strategy_id


# --------------------------------------------------------------------------- #
# Uploaded strategy files
# --------------------------------------------------------------------------- #

#: How long checking an uploaded file may take. A strategy loads a model in
#: ``initialize``, never at import, so a file that needs longer is doing work
#: at the wrong time.
FILE_CHECK_SECONDS = 60
MAX_FILE_BYTES = 1_000_000


def _file_version_known(session, spec: rs.StrategySpec) -> bool:
    """Whether this exact file is already a recorded version: no process needed."""
    code_hash = rs.hash_sources(Path(spec.target))
    return (
        session.query(rdb.ResearchStrategyVersion.id)
        .join(
            rdb.ResearchStrategy, rdb.ResearchStrategy.id == rdb.ResearchStrategyVersion.strategy_id
        )
        .filter(
            rdb.ResearchStrategy.key == spec.key,
            rdb.ResearchStrategyVersion.code_hash == code_hash,
        )
        .first()
        is not None
    )


def identify_file(key: str, path: Path) -> dict:
    """A strategy file's version identity, read in a process of its own (``research.identify``).

    Raises:
        ResearchError: The file does not load or does not build a strategy,
            with what to fix.
    """
    import json
    import tempfile

    log_dir = PROJECT_ROOT / os.getenv("LOG_DIR", "log") / "research"
    log_dir.mkdir(parents=True, exist_ok=True)
    env = _child_env()
    env.update(_limits_env("backtest"))
    with tempfile.TemporaryDirectory(prefix="research-identify-") as tmp:
        out = Path(tmp) / "identity.json"
        with open(log_dir / "strategy_files.log", "ab") as log:
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        _LIMITS_BOOTSTRAP,
                        "research.identify",
                        key,
                        str(path),
                        str(out),
                    ],
                    cwd=str(PROJECT_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    env=env,
                    timeout=FILE_CHECK_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise ResearchError(
                    f"Loading {key}.py took longer than {FILE_CHECK_SECONDS} seconds. Load a "
                    "model or other heavy data in initialize(), not when the file is read."
                ) from exc
        try:
            result = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {
                "error": f"{key}.py could not be checked: the process reading it stopped early. "
                "It may have run out of memory; the research log has the details."
            }
    if "error" in result:
        raise ResearchError(result["error"])
    return result["identity"]


def upload_strategy_file(filename: str, content: bytes, replace: bool = False) -> dict:
    """Add or replace an uploaded strategy file, and record its version.

    The file is checked before it lands: it is written to a folder beside the
    strategy files under its final name, so its identity is the one it will
    have, read there, and only moved into place once it builds a strategy. A
    file that fails leaves nothing behind and changes nothing.
    """
    import tempfile

    key = Path(filename or "").name
    if not key.endswith(".py"):
        raise ResearchError("Upload a Python file, ending in .py.")
    key = key[: -len(".py")]
    if not rs.FILE_KEY_RE.match(key):
        raise ResearchError(
            "Name the file with lowercase letters, digits and underscores, starting with a "
            "letter, for example opening_range.py. The name becomes the strategy's key."
        )
    if not content.strip():
        raise ResearchError("The file is empty.")
    if len(content) > MAX_FILE_BYTES:
        raise ResearchError("A strategy file can be at most 1 MB.")
    existing = rs.discover().get(key)
    if existing is not None and existing.source != "file":
        raise ResearchError(f"{key} is already the name of an installed strategy. Rename the file.")
    target = rs.STRATEGY_FILES / f"{key}.py"
    if target.exists() and not replace:
        raise ResearchError(
            f"A strategy file named {key}.py is already here. Choose to replace it to record "
            "a new version; its earlier versions and their results are kept."
        )
    rs.STRATEGY_FILES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=rs.STRATEGY_FILES) as tmp:
        staged = Path(tmp) / f"{key}.py"
        staged.write_bytes(content)
        identity = identify_file(key, staged)
        os.replace(staged, target)
    strategy_id = _record_version(rdb.db_session, rs.file_spec(key, target), identity)
    logger.info(
        "Research: strategy file %s.py recorded (version %s)", key, identity["version_hash"][:12]
    )
    return {"id": strategy_id, "key": key, "version_hash": identity["version_hash"]}


def strategy_scenarios(session, strategy_id: int) -> list[int]:
    """Every scenario that uses any version of a strategy."""
    versions = [
        v.id
        for v in session.query(rdb.ResearchStrategyVersion.id).filter_by(strategy_id=strategy_id)
    ]
    if not versions:
        return []
    return sorted(
        {
            row.scenario_id
            for row in session.query(rdb.ResearchScenarioInstance.scenario_id).filter(
                rdb.ResearchScenarioInstance.strategy_version_id.in_(versions)
            )
        }
    )


def _overview(session, row, installed: bool) -> dict:
    """What the registry shows beside a strategy: where it stands and how it last did."""
    scenario_ids = strategy_scenarios(session, row.id)
    trading = deployed = 0
    last = None
    if scenario_ids:
        trading = (
            session.query(func.count(rdb.ResearchRun.id))
            .filter(
                rdb.ResearchRun.scenario_id.in_(scenario_ids),
                rdb.ResearchRun.environment != rdb.BACKTEST,
                rdb.ResearchRun.status.in_(rdb.ACTIVE_STATUSES),
            )
            .scalar()
        )
        deployed = (
            session.query(func.count(rdb.ResearchDeployment.id))
            .filter(
                rdb.ResearchDeployment.scenario_id.in_(scenario_ids),
                rdb.ResearchDeployment.status == "ACTIVE",
            )
            .scalar()
        )
        last = (
            session.query(rdb.ResearchRun)
            .filter(
                rdb.ResearchRun.scenario_id.in_(scenario_ids),
                rdb.ResearchRun.status.in_((rdb.COMPLETED, rdb.FAILED)),
            )
            .order_by(rdb.ResearchRun.finished_at.desc(), rdb.ResearchRun.id.desc())
            .first()
        )
    last_run = None
    if last is not None:
        positions = {
            inst.position
            for inst in session.query(rdb.ResearchScenarioInstance).filter_by(
                scenario_id=last.scenario_id
            )
            if session.get(rdb.ResearchStrategyVersion, inst.strategy_version_id).strategy_id
            == row.id
        }
        books = [a for a in (last.attribution or []) if a.get("instance") in positions]
        last_run = {
            "id": last.id,
            "status": last.status,
            "environment": last.environment,
            "finished_at": last.finished_at.isoformat() if last.finished_at else None,
            "net_pnl": sum(b["net_pnl"] for b in books) if books else None,
        }
    if not installed:
        status = "not_installed"
    elif trading:
        status = "trading"
    elif deployed:
        status = "deployed"
    else:
        status = "ready"
    return {"status": status, "last_run": last_run, "active_deployments": deployed}


def list_strategies() -> dict:
    from services import research_hosted_service as hosted

    problems = sync_registry()
    session = rdb.db_session
    installed = set(rs.discover()) | hosted.installed_keys(session)
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
                **_overview(session, row, row.key in installed),
            }
        )
    return {"items": items, "problems": problems}


#: Days of history a scenario made for a deployment can also be backtested over.
QUICK_WINDOW_DAYS = 30


def quick_scenario(strategy_id: int, data: dict) -> dict:
    """A one-strategy isolated scenario, started from the registry (PRD section 32).

    It uses the strategy's newest version and the chosen configuration (its
    newest when none is chosen), in the strategy's own experiment. An
    identical scenario already there is reused rather than duplicated, so the
    same button pressed twice leads to the same result.
    """
    from datetime import date, timedelta

    session = rdb.db_session
    row = session.get(rdb.ResearchStrategy, strategy_id)
    if row is None:
        raise ResearchError("That strategy does not exist.")
    version = (
        session.query(rdb.ResearchStrategyVersion)
        .filter_by(strategy_id=row.id)
        .order_by(rdb.ResearchStrategyVersion.id.desc())
        .first()
    )
    configs = session.query(rdb.ResearchConfigVersion).filter_by(strategy_id=row.id)
    if data.get("config_version_id"):
        config = configs.filter_by(id=_int(data, "config_version_id")).first()
    else:
        config = configs.order_by(rdb.ResearchConfigVersion.version_no.desc()).first()
    if version is None or config is None:
        raise ResearchError("Save a configuration for this strategy first.")
    yesterday = date.today() - timedelta(days=1)
    start = str(data.get("start") or yesterday - timedelta(days=QUICK_WINDOW_DAYS))
    end = str(data.get("end") or yesterday)
    name = f"{row.key} (config v{config.version_no}), {start} to {end}"
    title = f"{row.key}: from the registry"
    experiment = session.query(rdb.ResearchExperiment).filter_by(name=title).first()
    if experiment is None:
        experiment_id = create_experiment(
            title,
            objective=f"Backtests and deployments of {row.key} started from its registry row.",
        )["id"]
    else:
        experiment_id = experiment.id
        for scenario in session.query(rdb.ResearchScenario).filter_by(
            experiment_id=experiment_id, name=name
        ):
            pairs = [
                (i.strategy_version_id, i.config_version_id)
                for i in session.query(rdb.ResearchScenarioInstance).filter_by(
                    scenario_id=scenario.id
                )
            ]
            if pairs == [(version.id, config.id)]:
                return {
                    **rdb.scenario_to_dict(scenario),
                    "instances": _instance_dicts(session, scenario.id),
                }
    payload = {
        "name": name,
        "mode": "ISOLATED",
        "start": start,
        "end": end,
        "instances": [{"strategy_version_id": version.id, "config_version_id": config.id}],
    }
    if data.get("cost_model"):
        payload["cost_model"] = data["cost_model"]
    return create_scenario(experiment_id, payload)


def quick_backtest(strategy_id: int, data: dict, user_id: str | None = None) -> dict:
    """Backtest one strategy from the registry: a scenario, then a run of it."""
    scenario = quick_scenario(strategy_id, data)
    item, created = enqueue_run(scenario["id"], user_id)
    return {**item, "created": created}


#: Results shown per configuration version on a strategy's page.
CONFIG_RESULTS = 10


def _config_results(session, config_ids: list[int]) -> dict[int, list[dict]]:
    """How each configuration did: its own book in the latest finished run of every
    scenario and session that used it, newest first (PRD section 37)."""
    out: dict[int, list[dict]] = {}
    if not config_ids:
        return out
    uses = (
        session.query(rdb.ResearchScenarioInstance)
        .filter(rdb.ResearchScenarioInstance.config_version_id.in_(config_ids))
        .all()
    )
    for use in uses:
        scenario = session.get(rdb.ResearchScenario, use.scenario_id)
        runs = (
            session.query(rdb.ResearchRun)
            .filter(
                rdb.ResearchRun.scenario_id == use.scenario_id,
                rdb.ResearchRun.status == rdb.COMPLETED,
            )
            .order_by(rdb.ResearchRun.id.desc())
            .limit(CONFIG_RESULTS)
            .all()
        )
        for run in runs:
            book = next(
                (a for a in (run.attribution or []) if a.get("instance") == use.position), None
            )
            if book is None:
                continue
            out.setdefault(use.config_version_id, []).append(
                {
                    "run_id": run.id,
                    "scenario_id": scenario.id,
                    "scenario": scenario.name,
                    "mode": scenario.mode,
                    "environment": run.environment,
                    "trading_date": run.trading_date,
                    "net_pnl": book.get("net_pnl"),
                    "return_pct": book.get("return_pct"),
                    "max_drawdown_pct": book.get("max_drawdown_pct"),
                    "trades": book.get("trades"),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                }
            )
    for rows in out.values():
        rows.sort(key=lambda r: r["run_id"], reverse=True)
        del rows[CONFIG_RESULTS:]
    return out


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
    from services.research_live_service import approved_versions

    results = _config_results(session, [c.id for c in configs])
    live = approved_versions(v.id for v in versions)
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
        "versions": [
            {
                **rdb.version_to_dict(v),
                "live_approved": v.id in live,
                "properties": rdb.version_properties(session, v),
            }
            for v in versions
        ],
        "configs": [
            {
                **rdb.config_to_dict(c),
                "used_by": used_by.get(c.id, 0),
                "results": results.get(c.id, []),
            }
            for c in configs
        ],
    }


#: Most free-form properties a version may carry, and how long each may be.
MAX_PROPERTIES, MAX_PROPERTY_NAME, MAX_PROPERTY_VALUE = 30, 60, 500


def _date_or_none(value, label: str) -> str | None:
    if value in (None, ""):
        return None
    parsed = rdb._iso_date(value)
    if parsed is None or len(str(value)) != 10:
        raise ResearchError(f"{label} must be a date, as YYYY-MM-DD.")
    return parsed


def set_version_properties(user_id: str, version_id: int, data: dict) -> dict:
    """Record what is known about a strategy version: its training period and details.

    Descriptive only: a version's code and model never change, and nothing set
    here changes how it runs (PRD section 5). The training period is what
    marks a backtest over the same dates as in-sample.

    Args:
        data: ``training_start`` and ``training_end`` (YYYY-MM-DD, both or
            neither) and ``properties``, a list of ``{"name", "value"}``.
    """
    session = rdb.db_session
    version = session.get(rdb.ResearchStrategyVersion, version_id)
    if version is None:
        raise ResearchError("That strategy version does not exist.")
    start = _date_or_none(data.get("training_start"), "The training start")
    end = _date_or_none(data.get("training_end"), "The training end")
    if (start is None) != (end is None):
        raise ResearchError("Give the training period both a start and an end, or neither.")
    if start and start > end:
        raise ResearchError("The training period must start on or before the day it ends.")
    pairs = data.get("properties") or []
    if not isinstance(pairs, list) or len(pairs) > MAX_PROPERTIES:
        raise ResearchError(f"A version can carry up to {MAX_PROPERTIES} properties.")
    properties: dict[str, str] = {}
    for pair in pairs:
        name = str((pair or {}).get("name") or "").strip()
        value = str((pair or {}).get("value") or "").strip()
        if not name and not value:
            continue
        if not name or len(name) > MAX_PROPERTY_NAME:
            raise ResearchError(
                f"Each property needs a name of up to {MAX_PROPERTY_NAME} characters."
            )
        if len(value) > MAX_PROPERTY_VALUE:
            raise ResearchError(
                f"The value of {name} is longer than {MAX_PROPERTY_VALUE} characters."
            )
        if name.lower() in (n.lower() for n in properties):
            raise ResearchError(f"The property {name} is listed twice.")
        properties[name] = value
    row = session.get(rdb.ResearchVersionProperties, version_id)
    if row is None:
        row = rdb.ResearchVersionProperties(strategy_version_id=version_id)
        session.add(row)
    row.training_start, row.training_end = start, end
    row.properties = properties
    row.updated_by = user_id
    row.updated_at = rdb.utc_now()
    session.commit()
    logger.info("Research: version=%s properties recorded by %s", version_id, user_id)
    return rdb.version_properties(session, version)


def trade_from(scenario) -> str | None:
    """A scenario's first traded day, when its data starts earlier as history.

    None for a scenario made before history was loaded ahead of the window: it
    trades from its data's first day.
    """
    return (scenario.execution_model or {}).get("trade_from")


def _in_sample(session, scenario) -> list[dict]:
    """Each strategy a backtest ran over dates it was trained on, with the overlap."""
    spec = scenario.dataset_spec or {}
    start = rdb._iso_date(trade_from(scenario) or spec.get("start"))
    end = rdb._iso_date(spec.get("end"))
    if not (start and end):
        return []
    out = []
    for inst in (
        session.query(rdb.ResearchScenarioInstance)
        .filter_by(scenario_id=scenario.id)
        .order_by(rdb.ResearchScenarioInstance.position)
    ):
        version = session.get(rdb.ResearchStrategyVersion, inst.strategy_version_id)
        props = rdb.version_properties(session, version)
        trained_from, trained_to = props["training_start"], props["training_end"]
        if trained_from and trained_to and trained_from <= end and start <= trained_to:
            out.append(
                {
                    "position": inst.position,
                    "strategy_key": session.get(rdb.ResearchStrategy, version.strategy_id).key,
                    "training_start": trained_from,
                    "training_end": trained_to,
                    "overlap_start": max(trained_from, start),
                    "overlap_end": min(trained_to, end),
                    "all_of_it": trained_from <= start and end <= trained_to,
                }
            )
    return out


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
    strategy = session.get(rdb.ResearchStrategy, strategy_id)
    if strategy is None:
        raise ResearchError("That strategy does not exist.")
    if strategy.source == SCRIPT_SOURCE and (
        config.stop_pct is not None or config.target_pct is not None
    ):
        raise ResearchError(
            "A Python strategy places its own exits, so a stop or target here would never be "
            "applied. Leave them empty."
        )
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
#: History loaded before a backtest's first day, in calendar days, so a strategy
#: that reads past bars before it decides (Alpha A reads 21 sessions) decides
#: from the first day instead of weeks into the window. Those bars are read,
#: never traded or counted in the result.
DEFAULT_WARMUP_DAYS = 45
WARMUP_DAYS = {"D": 400}


def create_experiment(
    name: str, description: str | None = None, objective: str | None = None
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ResearchError("Give the experiment a name.")
    row = rdb.ResearchExperiment(
        name=name[:200],
        description=(description or "").strip()[:2000] or None,
        objective=(objective or "").strip()[:2000] or None,
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
            {
                **rdb.experiment_to_dict(row),
                "scenarios": len(scenario_ids),
                "runs": statuses,
                "status": experiment_status(session, scenario_ids),
            }
        )
    return out


def experiment_status(session, scenario_ids: list[int]) -> str:
    """Derived from each scenario's latest backtest, never stored: so it cannot drift.

    ``empty`` (no scenarios), ``running`` (a run queued or running), ``failed``
    (a scenario's latest run failed), ``done`` (every scenario has a completed
    run), ``partial`` (some do) or ``not_run``.
    """
    if not scenario_ids:
        return "empty"
    latest = []
    for scenario_id in scenario_ids:
        run = (
            session.query(rdb.ResearchRun.status)
            .filter_by(scenario_id=scenario_id, environment=rdb.BACKTEST)
            .filter(rdb.ResearchRun.status != rdb.CANCELLED)
            .order_by(rdb.ResearchRun.id.desc())
            .first()
        )
        latest.append(run.status if run else None)
    if any(s in (rdb.QUEUED, rdb.RUNNING) for s in latest):
        return "running"
    if rdb.FAILED in latest:
        return "failed"
    done = sum(s == rdb.COMPLETED for s in latest)
    if done == len(latest):
        return "done"
    return "partial" if done else "not_run"


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
    return {
        **rdb.experiment_to_dict(row),
        "status": experiment_status(session, [s["id"] for s in scenarios]),
        "scenarios": scenarios,
    }


def create_scenario(experiment_id: int, data: dict) -> dict:
    """Validate and store one scenario. Scenarios are never edited afterwards.

    Everything the run will need is checked here, so a scenario that saves is
    one that can run: one bar interval across its strategies, one exchange
    across its configurations, a known cost schedule and a valid window. The
    dataset's symbols are the union of the instances' universes.
    """
    from datetime import date, timedelta

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

    scripted = any(
        session.get(rdb.ResearchStrategy, v.strategy_id).source == SCRIPT_SOURCE for v in versions
    )
    if scripted and len(versions) > 1:
        raise ResearchError(
            "A Python strategy runs alone in its scenario: it places its own orders, so it "
            "cannot share a book or a clock with another strategy."
        )
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
    interval = intervals.pop()
    start, end = str(data.get("start") or ""), str(data.get("end") or "")
    try:
        DatasetSpec(symbols=tuple(symbols), exchange="", interval=interval, start=start, end=end)
        # The bars before the first day are history the strategies read before
        # they decide; the run trades from ``start`` (execution_model.trade_from).
        history_from = date.fromisoformat(start) - timedelta(
            days=WARMUP_DAYS.get(interval, DEFAULT_WARMUP_DAYS)
        )
        spec = DatasetSpec(
            symbols=tuple(symbols),
            exchange=exchanges.pop(),
            interval=interval,
            start=history_from.isoformat(),
            end=end,
        )
    except DatasetError as exc:
        raise ResearchError(str(exc)) from exc

    cost = data.get("cost_model") or {}
    preset = cost.get("preset", "india_intraday_nse")
    if preset not in PRESETS:
        raise ResearchError("Choose one of the listed cost models.")
    slippage = _number(cost.get("slippage_bps", 0), "Slippage", 0, 100)
    policy = data.get("risk_policy") or {}
    max_gross = _number(policy.get("max_gross_leverage", 1.0), "Exposure limit", 0.01, 10)
    risk_policy = {"max_gross_leverage": max_gross}
    # Portfolio limits, in rupees, judged on each day's P&L by the shared risk core.
    for key, label in (
        ("daily_loss_limit", "Daily loss limit"),
        ("daily_profit_target", "Daily profit target"),
        ("lock_profit_at", "Lock profit from"),
        ("lock_profit_floor", "Locked profit floor"),
        ("lock_trail_step", "Lock trail step"),
    ):
        if policy.get(key) not in (None, ""):
            risk_policy[key] = _number(policy[key], label, 1, 1e12)
    if scripted and len(risk_policy) > 1:
        raise ResearchError(
            "A Python strategy places its own exits, so portfolio limits would never be applied "
            "to it. Leave them empty."
        )
    if ("lock_profit_floor" in risk_policy or "lock_trail_step" in risk_policy) and (
        "lock_profit_at" not in risk_policy
    ):
        raise ResearchError("Set the profit at which locking starts before its floor or trail.")
    if risk_policy.get("lock_profit_floor", 0) > risk_policy.get("lock_profit_at", float("inf")):
        raise ResearchError("The locked profit floor must be below the profit that starts it.")
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
        execution_model={"fill": "script_replay" if scripted else "next_open", "trade_from": start},
        risk_policy=risk_policy,
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
MAX_CONCURRENT = env_int("RESEARCH_MAX_CONCURRENT", 2, minimum=1)
MAX_MEMORY_MB = env_int("RESEARCH_MAX_MEMORY_MB", 2048, minimum=256)
#: Processor time one backtest may use before the kernel stops it, as the
#: strategy host limits a script. Sessions have none: they wait on the market
#: all day, and stopping one would leave its positions unmanaged.
MAX_CPU_SECONDS = env_int("RESEARCH_MAX_CPU_SECONDS", 3600, minimum=60)
#: Wall-clock time one backtest may run before the supervisor stops it.
JOB_TIMEOUT_SECONDS = env_int("RESEARCH_JOB_TIMEOUT_SECONDS", 7200, minimum=60)
#: Sessions (Sandbox, Shadow and Live) that may run at once, each a process all day.
MAX_SESSIONS = env_int("RESEARCH_MAX_SESSIONS", 4, minimum=1)
#: Backtests run below the app's priority: the app serves traders, a backtest can wait.
BACKTEST_NICE = 10
#: Run logs older than this are deleted, as the strategy host prunes its own.
#: A run's ledger, results and artifacts are its record and are never pruned.
LOG_RETENTION_DAYS = env_int("RESEARCH_LOG_RETENTION_DAYS", 30, minimum=1)
CANCEL_GRACE_SECONDS = 5.0
POLL_SECONDS = 0.5

_lock = threading.Lock()
_children: dict[int, dict] = {}  # run_id -> {"process": Popen, "cancelled_at": float | None}
_supervising = False
_hook_registered = False
_shutting_down = False


# Run as ``python -c <this> <worker module> <run id>``. It sets the worker's
# limits in the child after exec and then runs the module as __main__, as
# ``python -m`` would. Never preexec_fn: that runs Python between fork and exec,
# where a lock another thread held at the fork stays held forever, and both the
# gthread worker and the development server have many threads. The strategy
# host learned this first (blueprints/python_strategy.py, _RLIMIT_BOOTSTRAP).
# A limit above the hard limit is lowered to it rather than skipped.
_LIMITS_BOOTSTRAP = (
    "import os, sys, runpy\n"
    "try:\n"
    "    import resource\n"
    "    def _limit(name, value):\n"
    "        try:\n"
    "            which = getattr(resource, name)\n"
    "            _soft, hard = resource.getrlimit(which)\n"
    "            if hard != resource.RLIM_INFINITY:\n"
    "                value = min(value, hard)\n"
    "            resource.setrlimit(which, (value, value))\n"
    "        except Exception:\n"
    "            pass\n"
    "    _mb = int(os.environ.get('RESEARCH_WORKER_MEMORY_MB') or 0)\n"
    "    if _mb:\n"
    "        _limit('RLIMIT_AS', _mb * 1024 * 1024)\n"
    "    _limit('RLIMIT_NOFILE', 256)\n"
    "    _cpu = int(os.environ.get('RESEARCH_WORKER_CPU_SECONDS') or 0)\n"
    "    if _cpu:\n"
    "        _limit('RLIMIT_CPU', _cpu)\n"
    "except Exception:\n"
    "    pass\n"
    "try:\n"
    "    os.nice(int(os.environ.get('RESEARCH_WORKER_NICE') or 0))\n"
    "except Exception:\n"
    "    pass\n"
    "_module = sys.argv[1]\n"
    "sys.argv = [_module] + sys.argv[2:]\n"
    "runpy.run_module(_module, run_name='__main__', alter_sys=True)\n"
)


def _limits_env(kind: str) -> dict:
    """The limits _LIMITS_BOOTSTRAP sets for a worker of this kind."""
    backtest = kind == "backtest"
    return {
        "RESEARCH_WORKER_MEMORY_MB": str(MAX_MEMORY_MB),
        "RESEARCH_WORKER_CPU_SECONDS": str(MAX_CPU_SECONDS if backtest else 0),
        "RESEARCH_WORKER_NICE": str(BACKTEST_NICE if backtest else 0),
    }


#: Never handed to a worker, because strategy plugin code runs there (master PRD
#: section 46): the broker's credentials, the app's own keys, and anything else
#: named like a credential. Nothing on a worker's import path reads them; a
#: Sandbox session's API key is passed explicitly, after this filter.
WITHHELD_FROM_WORKERS = frozenset({"APP_KEY", "API_KEY_PEPPER", "FERNET_SALT"})
WITHHELD_WORDS = ("SECRET", "PASSWORD", "TOKEN")


def _withheld(name: str) -> bool:
    upper = name.upper()
    return (
        upper in WITHHELD_FROM_WORKERS
        or upper.startswith("BROKER_API_")
        or upper.endswith("_KEY")
        or any(word in upper for word in WITHHELD_WORDS)
    )


def _child_env() -> dict:
    """The worker's environment: the app's, minus credentials, with numeric
    libraries single-threaded.

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
    env = {name: value for name, value in os.environ.items() if not _withheld(name)}
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        env[name] = "1"
    env["ARROW_DEFAULT_MEMORY_POOL"] = "system"
    return env


#: Worker module per kind of run. Backtests share the concurrency cap; Sandbox
#: sessions do not, because a session lives for a whole trading day and a cap
#: would leave a scheduled one waiting behind it.
WORKER_MODULES = {"backtest": "research.worker", "sandbox": "research.sandbox_worker"}
#: Times a session's worker is restarted after it stops on an error. The last
#: restart only closes what the session holds; after that it is given up.
SANDBOX_RESTARTS = 3
#: Where a session given up after its restarts may have left positions open.
LEFT_OPEN = {
    rdb.SANDBOX: "Its positions may still be open in the sandbox: check the sandbox positions "
    "for its symbols.",
    rdb.SHADOW: "A Shadow session holds nothing real, so nothing is left open anywhere.",
    rdb.LIVE: "Its positions may still be open at your broker: check your broker's positions "
    "for its symbols now.",
}


_pruned_at: float | None = None


def _prune_logs(log_dir: Path) -> None:
    """Delete run logs past LOG_RETENTION_DAYS, at most once a day. Caller holds _lock.

    A log still being written was modified within the period, so it is never
    one of them.
    """
    global _pruned_at
    if _pruned_at is not None and time.monotonic() - _pruned_at < 86400:
        return
    _pruned_at = time.monotonic()
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for path in log_dir.glob("run_*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.exception("Research: could not prune the run log %s", path.name)


def _spawn(run_id: int, kind: str = "backtest", env: dict | None = None) -> None:
    log_dir = PROJECT_ROOT / os.getenv("LOG_DIR", "log") / "research"
    log_dir.mkdir(parents=True, exist_ok=True)
    _prune_logs(log_dir)
    child_env = _child_env()
    child_env.update(env or {})
    child_env.update(_limits_env(kind))
    with open(log_dir / f"run_{run_id}.log", "ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", _LIMITS_BOOTSTRAP, WORKER_MODULES[kind], str(run_id)],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            env=child_env,
        )
    previous = _children.get(run_id, {})
    _children[run_id] = {
        "process": process,
        "cancelled_at": None,
        "started_at": time.monotonic(),
        "kind": kind,
        "env": env or {},
        "restarts": previous.get("restarts", 0),
    }
    logger.info("Research: run=%s started (%s) as pid %s", run_id, kind, process.pid)


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
    _advance_downloads_locked(session)
    free = MAX_CONCURRENT - sum(
        1 for c in _children.values() if c.get("kind", "backtest") == "backtest"
    )
    if free <= 0:
        return
    queued = (
        session.query(rdb.ResearchRun.id)
        .filter_by(status=rdb.QUEUED, environment=rdb.BACKTEST, data_job=None)
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
            logger.exception("Research: run=%s could not start", run_id)
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
            elif (
                cancelled is None
                and child.get("kind", "backtest") == "backtest"
                and now - child.get("started_at", now) > JOB_TIMEOUT_SECONDS
            ):
                # Settled first, so the kill below is not read as running out
                # of memory when the process is reaped.
                _settle(
                    session,
                    run_id,
                    f"The backtest ran longer than its {JOB_TIMEOUT_SECONDS // 60}-minute limit "
                    "and was stopped. Try a shorter window or fewer symbols.",
                )
                child["cancelled_at"] = now - CANCEL_GRACE_SECONDS
                process.kill()
            continue
        del _children[run_id]
        if child.get("kind") == "sandbox":
            if code != 0:
                _restart_or_settle_session(session, run_id, child, code)
            if run_id not in _children:  # not restarted: tell its owner how it ended
                from services.research_sandbox_service import alert_session_ended

                alert_session_ended(run_id)
        elif code != 0:
            _settle(session, run_id, _exit_message(code))


def _restart_or_settle_session(session, run_id: int, child: dict, code: int) -> None:
    """A session's worker stopped without ending its session: resume, else square off.

    The new worker resumes from the ledger and reconciles every order with its
    venue, so a restart costs no duplicate orders. The last restart resumes
    only to close what the session holds. If even that worker stops, the
    session is given up FAILED, saying where its positions may still be open.
    """
    run = session.get(rdb.ResearchRun, run_id)
    if run is None:
        return
    session.refresh(run)
    if run.status not in rdb.ACTIVE_STATUSES or _shutting_down:
        return
    restarts = child.get("restarts", 0)
    if restarts < SANDBOX_RESTARTS:
        last = restarts + 1 == SANDBOX_RESTARTS
        env = {**(child.get("env") or {}), "RESEARCH_RESTARTED": "close" if last else "resume"}
        _spawn(run_id, "sandbox", env)
        _children[run_id]["restarts"] = restarts + 1
        logger.warning(
            "Research: session=%s worker exited %s; restarted to %s",
            run_id, code, "close its positions" if last else "resume",
        )  # fmt: skip
        return
    _settle(
        session,
        run_id,
        "The session stopped on repeated errors and could not close its positions itself. "
        + LEFT_OPEN.get(run.environment, LEFT_OPEN[rdb.SANDBOX])
        + " The session log has the details.",
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
    if code == -getattr(signal, "SIGXCPU", -1):
        return (
            f"The backtest used more than its {MAX_CPU_SECONDS // 60} minutes of processor "
            "time and was stopped. Try a shorter window or fewer symbols."
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
                if not _children and not _downloading(session):
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
        if _children or _downloading(session):
            _ensure_supervisor_locked()


# --------------------------------------------------------------------------- #
# Market data a backtest needs
# --------------------------------------------------------------------------- #

#: A stored range that starts or ends this many days inside a window still
#: covers it: a window may begin or end on a weekend or a holiday, which has no
#: bars, and asking the broker again for those days would never fill them.
COVERAGE_SLACK_DAYS = 5
#: Historify's download job outcomes, and whether the backtest can go ahead.
DOWNLOAD_DONE = {
    "completed": True,
    "completed_with_errors": True,
    "failed": False,
    "cancelled": False,
}


def missing_symbols(spec: dict, now: float | None = None) -> list[str]:
    """Symbols Historify does not hold over a dataset spec's whole window.

    Read from Historify's catalogue of first and last bars per symbol, at the
    interval Historify stores the spec's bars at (1m for every intraday
    interval, D for daily).
    """
    from database import historify_db
    from research.dataset import DatasetSpec

    start_ts, end_ts = DatasetSpec(**spec).epoch_window()
    end_ts = min(end_ts, now if now is not None else time.time())
    slack = COVERAGE_SLACK_DAYS * 86400
    interval = "D" if spec["interval"] == "D" else "1m"
    missing = []
    for symbol in spec["symbols"]:
        stored = historify_db.get_data_range(symbol, spec["exchange"], interval)
        if (
            not stored
            or stored["first_timestamp"] is None
            or stored["first_timestamp"] > start_ts + slack
            or stored["last_timestamp"] < end_ts - slack
        ):
            missing.append(symbol)
    return missing


def _start_download(run, scenario, user_id: str | None) -> None:
    """Fetch from the broker, through Historify, the bars a first run is missing.

    Only before a scenario's data is pinned: a pinned scenario replays its
    snapshot and never reads Historify again. Nothing to fetch, or no way to
    fetch it (no API key), leaves the run to start at once; the worker then
    names any symbol that still has no bars.
    """
    from datetime import date

    spec = scenario.dataset_spec
    try:
        missing = missing_symbols(spec)
    except Exception:
        logger.exception("Research: run=%s could not read Historify's catalogue", run.id)
        return
    if not missing:
        return
    from database.auth_db import get_api_key_for_tradingview
    from services.historify_service import create_and_start_job

    api_key = get_api_key_for_tradingview(user_id) if user_id else None
    if not api_key:
        logger.info("Research: run=%s needs bars but there is no API key to fetch them", run.id)
        return
    ok, body, _status = create_and_start_job(
        "custom",
        [{"symbol": symbol, "exchange": spec["exchange"]} for symbol in missing],
        "D" if spec["interval"] == "D" else "1m",
        spec["start"],
        min(spec["end"], date.today().isoformat()),
        api_key,
        config={"source": "research", "run_id": run.id},
    )
    if not ok:
        logger.warning("Research: run=%s could not start its download: %s", run.id, body)
        return
    run.data_job = body["job_id"]
    run.message = (
        f"Downloading market data from your broker for {len(missing)} of "
        f"{len(spec['symbols'])} symbols. It starts when the download finishes."
    )
    logger.info("Research: run=%s waits for Historify job %s", run.id, run.data_job)


def _downloading(session) -> bool:
    return (
        session.query(rdb.ResearchRun.id)
        .filter(
            rdb.ResearchRun.status == rdb.QUEUED,
            rdb.ResearchRun.environment == rdb.BACKTEST,
            rdb.ResearchRun.data_job.isnot(None),
        )
        .first()
        is not None
    )


def _advance_downloads_locked(session) -> None:
    """Release each backtest whose download has finished; fail one whose download did not.

    Caller holds _lock. Every write is a compare-and-set on the run still
    waiting for that job, so a run the operator cancelled meanwhile stays
    cancelled.
    """
    from database import historify_db

    waiting = (
        session.query(rdb.ResearchRun.id, rdb.ResearchRun.data_job, rdb.ResearchRun.message)
        .filter(
            rdb.ResearchRun.status == rdb.QUEUED,
            rdb.ResearchRun.environment == rdb.BACKTEST,
            rdb.ResearchRun.data_job.isnot(None),
        )
        .all()
    )
    for run_id, job_id, message in waiting:
        job = historify_db.get_download_job(job_id)
        status = (job or {}).get("status")
        same = session.query(rdb.ResearchRun).filter_by(
            id=run_id, status=rdb.QUEUED, data_job=job_id
        )
        if job is None or DOWNLOAD_DONE.get(status) is False:
            reason = (job or {}).get("error_message") or (
                "the download was cancelled" if status == "cancelled" else "the download stopped"
            )
            same.update(
                {
                    rdb.ResearchRun.status: rdb.FAILED,
                    rdb.ResearchRun.data_job: None,
                    rdb.ResearchRun.finished_at: rdb.utc_now(),
                    rdb.ResearchRun.message: "The market data for this backtest could not be "
                    f"downloaded from your broker ({reason}). Check that you are logged in to "
                    "your broker, or download the bars in Historify, then run it again.",
                },
                synchronize_session=False,
            )
        elif DOWNLOAD_DONE.get(status):
            same.update(
                {rdb.ResearchRun.data_job: None, rdb.ResearchRun.message: "Waiting to start"},
                synchronize_session=False,
            )
        else:
            done = (job.get("completed_symbols") or 0) + (job.get("failed_symbols") or 0)
            progress = (
                f"Downloading market data from your broker: {done} of "
                f"{job.get('total_symbols') or 0} symbols. It starts when the download finishes."
            )
            if progress != message:
                same.update({rdb.ResearchRun.message: progress}, synchronize_session=False)
        session.commit()


def operations() -> dict:
    """The operations panel: every worker this app runs, and the limits it runs them under.

    The child table is copied under the lock and everything slower (the
    database, each process's memory from /proc) is read after it is released.
    """
    import psutil

    with _lock:
        children = sorted((run_id, dict(child)) for run_id, child in _children.items())
    now = time.monotonic()
    session = rdb.db_session
    workers = []
    for run_id, child in children:
        process = child["process"]
        try:
            memory_mb = round(psutil.Process(process.pid).memory_info().rss / 1048576, 1)
        except (psutil.Error, AttributeError, TypeError):
            memory_mb = None
        run = session.get(rdb.ResearchRun, run_id)
        workers.append(
            {
                "run_id": run_id,
                "kind": "backtest" if child.get("kind", "backtest") == "backtest" else "session",
                "environment": run.environment if run else None,
                "status": run.status if run else None,
                "pid": getattr(process, "pid", None),
                "memory_mb": memory_mb,
                "running_seconds": int(now - child["started_at"])
                if child.get("started_at")
                else None,
                "heartbeat_at": run.heartbeat_at.isoformat() if run and run.heartbeat_at else None,
                "lease_owner": run.lease_owner if run else None,
                "restarts": child.get("restarts", 0),
            }
        )
    return {
        "workers": workers,
        "limits": {
            "backtests": MAX_CONCURRENT,
            "sessions": MAX_SESSIONS,
            "memory_mb": MAX_MEMORY_MB,
            "cpu_seconds": MAX_CPU_SECONDS,
            "job_timeout_seconds": JOB_TIMEOUT_SECONDS,
        },
    }


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


def enqueue_run(scenario_id: int, user_id: str | None = None) -> tuple[dict, bool]:
    """Queue a run of a scenario, or return the completed run it would repeat.

    A scenario whose data is already pinned has a known run_hash, so a rerun of
    identical inputs returns the existing result instead of computing it twice
    (PRD section 17). A scenario still loading its data the first time is not
    queued twice: two first runs racing would both pin data and the second would
    have nowhere to put an identical result.

    A first run whose bars Historify does not hold yet fetches them from the
    broker first, as ``user_id``'s download, and starts when they are in.

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
    session.flush()
    if not scenario.dataset_id:
        _start_download(run, scenario, user_id)
    session.commit()
    result = rdb.run_to_dict(run)
    dispatch()
    return result, True


def enqueue_experiment(experiment_id: int, user_id: str | None = None) -> list[dict]:
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
    return [enqueue_run(scenario_id, user_id)[0] for scenario_id in ids]


def cancel_run(run_id: int) -> dict:
    session = rdb.db_session
    existing = session.get(rdb.ResearchRun, run_id)
    if existing is not None and existing.environment in rdb.SESSION_ENVIRONMENTS:
        # Cancelling would abandon whatever the session holds. A session ends
        # through its deployment, which squares off first.
        raise ResearchError(
            "A session is stopped from its deployment, which closes its positions first."
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
        # A backtest over dates a strategy was trained on scores it on what it
        # has already seen. Sessions trade new data, so they never are.
        "in_sample": _in_sample(session, scenario) if row.environment == rdb.BACKTEST else [],
        # How a run Research did not decide was made: a hosted day's tag and
        # marks, a script replay's fill rules. Empty for the others.
        "provenance": row.provenance or {},
        "dataset": None
        if dataset is None
        else {
            "id": dataset.id,
            "dataset_hash": dataset.dataset_hash,
            "bar_count": dataset.bar_count,
            "spec": dataset.spec,
            "source": dataset.source,
            "symbol_count": dataset.symbol_count,
            "first_ts": dataset.first_ts,
            "last_ts": dataset.last_ts,
        },
        "artifacts": [
            {"kind": a.kind, "sha256": a.sha256, "size_bytes": a.size_bytes}
            for a in session.query(rdb.ResearchArtifact).filter_by(run_id=run_id)
        ],
    }


#: The most of a run's log a page reads at once: its end, where a failure is.
LOG_TAIL_BYTES = 512 * 1024


def run_log(run_id: int) -> dict:
    """The end of a run's log, as the strategy host shows a script's.

    Every run and session writes its own log (``log/research/run_<id>.log``):
    the worker's lines, a strategy's tracebacks, and a Python strategy's own
    output in a script backtest. Logs past ``LOG_RETENTION_DAYS`` are deleted;
    a recorded Python-host day never had one.
    """
    session = rdb.db_session
    run = session.get(rdb.ResearchRun, run_id)
    if run is None:
        raise ResearchError("That run does not exist.")
    path = PROJECT_ROOT / os.getenv("LOG_DIR", "log") / "research" / f"run_{run_id}.log"
    if not path.is_file():
        if run.environment == rdb.HOSTED:
            note = "A recorded Python strategy day has no Research log: its output is in Python Strategies."
        elif run.status == rdb.QUEUED:
            note = "Nothing yet: the log starts when the run does."
        else:
            note = f"No log is kept for this run. Run logs are deleted after {LOG_RETENTION_DAYS} days."
        return {
            "content": "",
            "note": note,
            "size_bytes": 0,
            "truncated": False,
            "updated_at": None,
        }
    stat = path.stat()
    with open(path, "rb") as fh:
        if stat.st_size > LOG_TAIL_BYTES:
            fh.seek(stat.st_size - LOG_TAIL_BYTES)
            fh.readline()  # the first line is cut part way; start at the next one
        content = fh.read().decode("utf-8", errors="replace")
    return {
        "content": content,
        "note": None,
        "size_bytes": stat.st_size,
        "truncated": stat.st_size > LOG_TAIL_BYTES,
        "updated_at": datetime_iso(stat.st_mtime),
    }


def datetime_iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat()


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
    # Drawdown on every bar before thinning, or a trough between two kept
    # points would vanish from the chart.
    equity = frame["portfolio"]
    frame["drawdown_portfolio"] = (equity / equity.cummax() - 1.0).fillna(0.0) * 100
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

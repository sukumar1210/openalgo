"""
Python strategies hosted at /python, followed by Research.

A hosted script keeps running exactly as the strategy host runs it: Research
never starts, stops or edits it (the Research page starts and stops it through
the host's own endpoints, as /python does). Adding one to Research makes it a
Research strategy whose source is ``script``:

* **Versions.** Each version of its file is recorded by hash, and a copy of
  the file is kept under that hash, so a backtest of an old version still runs
  after the file is edited in /python.
* **Recording.** Its own Sandbox trading is recorded as one ``HOSTED`` run per
  trading day, built from the sandbox's trades that carry its order tag (the
  ``strategy`` name its orders send). The sandbox's own square-off orders carry
  ``AUTO_SQUARE_OFF`` instead, so a square-off of a position the script held is
  given back to it. Costs come from the strategy's registry scenario, the same
  model a backtest of it charges, so the two compare like for like.
* **Backtests.** ``research.script_replay`` replays history to the unchanged
  file.

A day is recorded once it is over for the script: any earlier day, or today
once the script has stopped and its scheduled stop time has passed. Recording
runs on the Research pages and once a day after the close, and never twice for
one day.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytz

from database import research_db as rdb
from research import strategy as rs
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: Where the strategy host keeps its scripts, one file per strategy named by its id.
SCRIPT_DIR = PROJECT_ROOT / "strategies" / "scripts"
SOURCE = "script"
#: The tag the sandbox's own square-off orders carry (sandbox/position_manager).
SQUARE_OFF_TAG = "AUTO_SQUARE_OFF"
#: Research's own Sandbox sessions tag their orders with this prefix.
RESEARCH_TAG_PREFIX = "research:"
#: When a script with no schedule is taken to be done for the day.
DEFAULT_STOP = "15:30"

_record_lock = threading.Lock()


class HostedError(ValueError):
    """A request about a hosted script that is refused, written for the trader."""


# --------------------------------------------------------------------------- #
# The strategy host's side
# --------------------------------------------------------------------------- #


def _host_configs() -> dict[str, dict]:
    """The strategy host's scripts as it holds them now, by id."""
    import blueprints.python_strategy as host

    return {sid: dict(config) for sid, config in host.snapshot_strategy_configs()}


def script_path(script_id: str) -> Path:
    return SCRIPT_DIR / f"{script_id}.py"


def version_copy(code_hash: str, file_name: str) -> Path:
    """Where the file of one script version is kept, under its own name."""
    from research.worker import data_root

    return data_root() / "scripts" / code_hash / file_name


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #


def script_identity(script_id: str, name: str) -> dict | None:
    """The current file's version identity, keeping a copy of it. None if it is gone.

    The file is copied before it is hashed, so the copy is exactly what the
    hash names even if /python saves the script at the same moment.
    """
    path = script_path(script_id)
    if not path.is_file():
        return None
    with tempfile.TemporaryDirectory(prefix="research-script-") as tmp:
        staged = Path(tmp) / path.name
        shutil.copyfile(path, staged)
        code_hash = rs.hash_sources(staged)
        keep = version_copy(code_hash, path.name)
        if not keep.exists():
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), keep)
    identity = {
        "key": script_id,
        "source": SOURCE,
        "dist_name": path.name,
        "dist_version": "",
        "code_hash": code_hash,
        "artifact_hash": None,
        "interval": "1m",
    }
    identity["version_hash"] = rs.sha256_hex(rs.canonical_json(identity))
    identity.update(model_hash=None, adapter_version=None, meta={"script_name": name})
    return identity


def sync_scripts(session) -> None:
    """Record a new version of every followed script whose file changed."""
    from services.research_service import _record_version

    for link in session.query(rdb.ResearchScriptLink).all():
        identity = script_identity(link.script_id, link.name)
        if identity is not None:
            spec = rs.StrategySpec(link.script_id, SOURCE, "", identity["dist_name"], "")
            _record_version(session, spec, identity)


def installed_keys(session) -> set[str]:
    """Keys of followed scripts whose file is still in the strategy host."""
    return {
        link.script_id
        for link in session.query(rdb.ResearchScriptLink)
        if script_path(link.script_id).is_file()
    }


# --------------------------------------------------------------------------- #
# Following a script
# --------------------------------------------------------------------------- #


def order_tags() -> list[dict]:
    """Every strategy name the sandbox's orders carry, newest first: what a script tags."""
    from sqlalchemy import func

    from database.sandbox_db import SandboxOrders, db_session

    try:
        rows = (
            db_session.query(
                SandboxOrders.strategy,
                func.count(SandboxOrders.id),
                func.max(SandboxOrders.order_timestamp),
            )
            .group_by(SandboxOrders.strategy)
            .all()
        )
        traded: dict[str, set[str]] = {}
        for tag, symbol in db_session.query(
            SandboxOrders.strategy, SandboxOrders.symbol
        ).distinct():
            traded.setdefault(tag, set()).add(symbol)
    finally:
        db_session.remove()
    tags = [
        {
            "tag": tag,
            "orders": count,
            "last_order_at": last.isoformat() if last else None,
            "symbols": sorted(traded.get(tag, ())),
        }
        for tag, count, last in rows
        if tag and tag != SQUARE_OFF_TAG and not tag.startswith(RESEARCH_TAG_PREFIX)
    ]
    return sorted(tags, key=lambda t: t["last_order_at"] or "", reverse=True)


def follow_script(user_id: str, script_id: str, data: dict) -> dict:
    """Add a hosted script to Research, or change the tag it is recorded by.

    The first time, it also saves the strategy's first configuration from the
    script's own schedule and what the trader entered, so it can be backtested
    and recorded straight away.
    """
    from services import research_service as svc

    session = rdb.db_session
    config = _host_configs().get(script_id)
    if config is None or not script_path(script_id).is_file():
        raise HostedError("That Python strategy is not in the strategy host.")
    tag = str(data.get("order_tag") or "").strip()[:200] or None
    if tag in (SQUARE_OFF_TAG,) or (tag or "").startswith(RESEARCH_TAG_PREFIX):
        raise HostedError("That name belongs to the sandbox's own orders, not to a script.")
    link = session.query(rdb.ResearchScriptLink).filter_by(script_id=script_id).one_or_none()
    if link is not None:
        link.order_tag = tag
        session.commit()
        return {"strategy_id": link.strategy_id, "key": script_id, "order_tag": tag}

    if script_id in rs.discover():
        raise HostedError(f"{script_id} is already the name of a Research strategy.")
    settings = {
        "capital": data.get("capital"),
        "allocation": 1.0,
        "leverage": data.get("leverage", 1.0),
        "product": data.get("product") or "MIS",
        "entry_time": config.get("schedule_start") or "09:15",
        "exit_time": config.get("schedule_stop") or DEFAULT_STOP,
        "universe": data.get("universe") or [],
        "exchange": config.get("exchange") or "NSE",
    }
    try:
        rs.Config.from_dict(settings)
    except (TypeError, ValueError) as exc:
        raise HostedError(str(exc)) from exc
    identity = script_identity(script_id, config.get("name") or script_id)
    spec = rs.StrategySpec(script_id, SOURCE, "", identity["dist_name"], "")
    strategy_id = svc._record_version(session, spec, identity)
    session.add(
        rdb.ResearchScriptLink(
            strategy_id=strategy_id,
            script_id=script_id,
            name=config.get("name") or script_id,
            order_tag=tag,
            created_by=user_id,
        )
    )
    session.commit()
    svc.save_config(
        strategy_id, settings, note="From the Python strategy's schedule when it was added."
    )
    logger.info("Research: following the hosted script %s (tag %s)", script_id, tag)
    return {"strategy_id": strategy_id, "key": script_id, "order_tag": tag}


# --------------------------------------------------------------------------- #
# Recording its Sandbox trading
# --------------------------------------------------------------------------- #


def _epoch(naive_ist: datetime) -> int:
    return int(IST.localize(naive_ist).timestamp())


def _day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def tagged_fills(tag: str, day: date) -> list:
    """The day's fills that belong to the script: its own, then the square-offs of what it held.

    The sandbox squares off its net position in a symbol, so a square-off is
    given to the script only up to what the script held, in the direction
    that closes it.
    """
    from database.sandbox_db import SandboxTrades, db_session
    from research.fills import Fill

    start, end = _day_window(day)
    try:
        rows = (
            db_session.query(SandboxTrades)
            .filter(
                SandboxTrades.strategy.in_((tag, SQUARE_OFF_TAG)),
                SandboxTrades.trade_timestamp >= start,
                SandboxTrades.trade_timestamp < end,
            )
            .order_by(SandboxTrades.trade_timestamp, SandboxTrades.id)
            .all()
        )
        trades = [
            (r.strategy, r.symbol, r.action, int(r.quantity), float(r.price), r.trade_timestamp)
            for r in rows
        ]
    finally:
        db_session.remove()
    held: dict[str, int] = {}
    fills = []
    for strategy, symbol, action, quantity, price, at in trades:
        qty = quantity if action.upper() == "BUY" else -quantity
        reason = "signal"
        if strategy == SQUARE_OFF_TAG:
            position = held.get(symbol, 0)
            if not position or (position > 0) == (qty > 0):
                continue
            qty = max(qty, -position) if qty < 0 else min(qty, -position)
            reason = "square_off"
        held[symbol] = held.get(symbol, 0) + qty
        fills.append(Fill(_epoch(at), symbol, qty, price, reason))
    return fills


def traded_days(tag: str) -> list[date]:
    """Every IST date the sandbox has a trade with this tag, oldest first."""
    from sqlalchemy import func

    from database.sandbox_db import SandboxTrades, db_session

    try:
        rows = (
            db_session.query(func.date(SandboxTrades.trade_timestamp))
            .filter(SandboxTrades.strategy == tag)
            .distinct()
            .all()
        )
    finally:
        db_session.remove()
    return sorted(date.fromisoformat(str(r[0])) for r in rows if r[0])


def _day_over(config: dict | None, day: date, now: datetime) -> bool:
    """Whether the script is done with ``day``: no more trades of its can arrive."""
    if day < now.date():
        return True
    if day > now.date() or (config or {}).get("is_running"):
        return False
    stop = (config or {}).get("schedule_stop") or DEFAULT_STOP
    hours, minutes = (int(x) for x in stop.split(":"))
    return now.time() >= time(hours, minutes)


def _recorded_days(session, strategy_id: int) -> set[str]:
    from services.research_service import strategy_scenarios

    scenarios = strategy_scenarios(session, strategy_id)
    if not scenarios:
        return set()
    return {
        row.trading_date
        for row in session.query(rdb.ResearchRun.trading_date).filter(
            rdb.ResearchRun.scenario_id.in_(scenarios),
            rdb.ResearchRun.environment == rdb.HOSTED,
        )
    }


def _frames_for(fills, exchange: str, day: date):
    """Stored 1m bars for the traded symbols that day, else bars made of the fills."""
    from research.dataset import DatasetError, DatasetSpec, load
    from research.fills import fill_frames

    frames = fill_frames(fills)
    try:
        stored = load(DatasetSpec(tuple(frames), exchange, "1m", day.isoformat(), day.isoformat()))
    except DatasetError:
        return frames, "fills"
    except Exception:
        logger.exception("Research: stored bars could not be read for a hosted day")
        return frames, "fills"
    return {**frames, **stored}, "historify"


def record_day(session, link, day: date) -> int | None:
    """Record one finished day of a followed script as a HOSTED run. Returns its id.

    None when there is nothing to record: no configuration yet, or no fills.
    """
    from portfolio.costs import schedule_for
    from research.fills import book_from_fills
    from research.worker import save_result
    from services.research_service import quick_scenario

    config_row = (
        session.query(rdb.ResearchConfigVersion)
        .filter_by(strategy_id=link.strategy_id)
        .order_by(rdb.ResearchConfigVersion.version_no.desc())
        .first()
    )
    fills = tagged_fills(link.order_tag, day)
    if config_row is None or not fills:
        return None
    scenario = quick_scenario(link.strategy_id, {"config_version_id": config_row.id})
    instance = scenario["instances"][0]
    config = rs.Config.from_dict(config_row.config)
    frames, marks = _frames_for(fills, config.exchange, day)
    cost_model = scenario["cost_model"]
    result = book_from_fills(
        frames,
        config,
        f"{link.script_id} (config v{config_row.version_no})",
        schedule_for(cost_model["preset"], cost_model.get("overrides")),
        fills,
        slippage_bps=float(cost_model.get("slippage_bps", 0.0)),
    )
    version = session.get(rdb.ResearchStrategyVersion, instance["strategy_version"]["id"])
    # Written whole in one commit: nobody ever sees it half recorded.
    run = rdb.ResearchRun(
        scenario_id=scenario["id"],
        status=rdb.COMPLETED,
        progress=1.0,
        environment=rdb.HOSTED,
        trading_date=day.isoformat(),
        started_at=rdb.utc_now(),
        finished_at=rdb.utc_now(),
    )
    session.add(run)
    session.flush()
    run.metrics, run.attribution = save_result(rdb, session, run.id, result)
    run.provenance = {
        "instances": [
            {
                "instance": 0,
                "strategy_id": link.strategy_id,
                "strategy_key": link.script_id,
                "strategy_version_id": version.id,
                "version_hash": version.version_hash,
                "config_version_id": config_row.id,
                "config_hash": config_row.config_hash,
            }
        ],
        "scenario_id": scenario["id"],
        "experiment_id": scenario["experiment_id"],
        "execution_environment": rdb.HOSTED,
        "recorded_from": f"sandbox trades tagged {link.order_tag}",
        "fills": len(fills),
        "marks": marks,
        "cost_model": cost_model,
        # Research records the version in place when it records the day. A
        # script edited earlier in the day is not told apart.
        "version_basis": "current when recorded",
    }
    session.commit()
    logger.info("Research: recorded %s on %s as run=%s", link.script_id, day, run.id)
    return run.id


def record_hosted(now: datetime | None = None) -> list[int]:
    """Record every finished, unrecorded day of every followed script. Never raises.

    One recorder at a time in this process, so a page load and the daily job
    cannot record the same day twice.
    """
    if not _record_lock.acquire(blocking=False):
        return []
    recorded: list[int] = []
    session = rdb.db_session
    try:
        now = now or datetime.now(IST).replace(tzinfo=None)
        configs = _host_configs()
        for link in session.query(rdb.ResearchScriptLink).all():
            if not link.order_tag:
                continue
            try:
                done = _recorded_days(session, link.strategy_id)
                for day in traded_days(link.order_tag):
                    if day.isoformat() in done or not _day_over(
                        configs.get(link.script_id), day, now
                    ):
                        continue
                    run_id = record_day(session, link, day)
                    if run_id is not None:
                        recorded.append(run_id)
            except Exception:
                session.rollback()
                logger.exception("Research: recording the hosted script %s failed", link.script_id)
    finally:
        _record_lock.release()
    return recorded


def hosted_overview() -> dict:
    """What the Research page shows beside each followed script, by script id."""
    session = rdb.db_session
    out = {}
    today = datetime.now(IST).date()
    for link in session.query(rdb.ResearchScriptLink).all():
        from services.research_service import strategy_scenarios

        scenarios = strategy_scenarios(session, link.strategy_id)
        last = None
        if scenarios:
            last = (
                session.query(rdb.ResearchRun)
                .filter(
                    rdb.ResearchRun.scenario_id.in_(scenarios),
                    rdb.ResearchRun.environment == rdb.HOSTED,
                    rdb.ResearchRun.status == rdb.COMPLETED,
                )
                .order_by(rdb.ResearchRun.trading_date.desc(), rdb.ResearchRun.id.desc())
                .first()
            )
        out[link.script_id] = {
            "strategy_id": link.strategy_id,
            "key": link.script_id,
            "order_tag": link.order_tag,
            "trades_today": len(tagged_fills(link.order_tag, today)) if link.order_tag else 0,
            "last_recorded": None
            if last is None
            else {
                "run_id": last.id,
                "trading_date": last.trading_date,
                "net_pnl": (last.metrics or {}).get("net_pnl"),
                "trades": (last.metrics or {}).get("trades"),
            },
        }
    return out

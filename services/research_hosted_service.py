"""
Research for the Python strategies hosted at /python: an extension of them, not a
second strategy system.

A Python strategy is one script with one set of settings, the ones it already
has in Python Strategies: its schedule (start time, stop time, weekdays), its
exchange, and the environment the host runs it in. It trades in analyzer mode
against the sandbox, whose own capital and intraday leverage are its money.
Research adds three abilities to every script, and asks for no settings of its
own:

* **Backtest.** The unchanged file is replayed over stored market data one
  trading day at a time, as the host runs it (``research.script_replay``). The
  script decides which symbols it reads; the replay learns them by watching
  what it asks for, and missing bars are downloaded through Historify.
* **Sandbox days.** Each day the script traded in the Sandbox is recorded as a
  ``HOSTED`` run from the sandbox's own trades. Its orders are recognised by the
  strategy names they carry, learned from the orders placed while the script
  was running (its run logs say when): no tag is ever configured. The sandbox's
  own square-off orders are given back to it, up to what it held.
* **Results.** Both kinds of run are ordinary Research runs: the same ledger,
  costs, metrics and run page, so a backtest and the days the script actually
  traded compare like for like.

Every script is followed automatically: each version of its file is recorded
by hash and a copy kept, so the code behind any result can be read back after
the file is edited.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytz

from database import research_db as rdb
from research import strategy as rs
from portfolio.strategy_pnl import SQUARE_OFF_TAG
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: Where the strategy host keeps its scripts, one file per strategy named by its id.
SCRIPT_DIR = PROJECT_ROOT / "strategies" / "scripts"
#: Where the strategy host writes one log per run, named by its start.
LOG_DIR = PROJECT_ROOT / os.getenv("LOG_DIR", "log") / "strategies"
SOURCE = "script"
#: Research's own Sandbox sessions tag their orders with this prefix.
RESEARCH_TAG_PREFIX = "research:"
#: When a script with no schedule is taken to be done for the day.
DEFAULT_STOP = "15:30"
#: A run log's last write marks the run's end, give or take this much.
RUN_END_SLACK = timedelta(minutes=2)
#: The cost model a script's runs are charged with, by exchange.
COST_PRESET = {"BSE": "india_intraday_bse"}
DEFAULT_COST_PRESET = "india_intraday_nse"

_record_lock = threading.Lock()


class HostedError(ValueError):
    """A request about a Python strategy that is refused, written for the trader."""


# --------------------------------------------------------------------------- #
# The strategy host's side
# --------------------------------------------------------------------------- #


def _host_configs() -> dict[str, dict]:
    """The strategy host's scripts as it holds them now, by id.

    A Run's sources are left out: each is a pinned copy of a version already
    followed, and it trades its own Sandbox account, not the trader's.
    """
    import blueprints.python_strategy as host
    from utils.strategy_mode import is_source_config

    return {
        sid: dict(config)
        for sid, config in host.snapshot_strategy_configs()
        if not is_source_config(config)
    }


def script_path(script_id: str) -> Path:
    return SCRIPT_DIR / f"{script_id}.py"


def version_copy(code_hash: str, file_name: str) -> Path:
    """Where the file of one script version is kept, under its own name."""
    from research.worker import data_root

    return data_root() / "scripts" / code_hash / file_name


def sandbox_money() -> tuple[float, float]:
    """The sandbox account's starting capital and intraday leverage: a script's money."""
    from database.sandbox_db import get_config

    capital = float(get_config("starting_capital", "10000000") or 10_000_000)
    leverage = float(get_config("equity_mis_leverage", "5") or 5)
    return capital, max(leverage, 1.0)


def script_settings(config: dict):
    """A script's own schedule, exchange and starting capital: all a replay needs.

    The capital is the strategy's own (its Schedule tab, Rs 5,00,000 unless
    set); the intraday leverage is the sandbox's, the account it trades in.
    """
    from research.script_replay import ScriptSettings
    from services.strategy_performance_service import start_capital
    from services.symbol_list_service import get_list

    _account, leverage = sandbox_money()
    capital = start_capital(config)
    chosen = get_list(config.get("symbol_list"))
    return ScriptSettings(
        capital=capital,
        leverage=leverage,
        entry_time=config.get("schedule_start") or "09:15",
        exit_time=config.get("schedule_stop") or DEFAULT_STOP,
        exchange=(config.get("exchange") or "NSE").upper(),
        days=tuple(
            d.lower()[:3]
            for d in (config.get("schedule_days") or ["mon", "tue", "wed", "thu", "fri"])
        ),
        symbols=tuple(chosen["symbols"]) if chosen else (),
    )


# --------------------------------------------------------------------------- #
# Following every script
# --------------------------------------------------------------------------- #


def script_identity(script_id: str, name: str) -> dict | None:
    """The current file's version identity, keeping a copy of it. None if it is gone.

    The file is copied before it is hashed, so the copy is exactly what the
    hash names even if /python saves the script at the same moment.
    """
    from research.script_replay import launcher_release

    path = script_path(script_id)
    if not path.is_file():
        return None
    with tempfile.TemporaryDirectory(prefix="research-script-") as tmp:
        staged = Path(tmp) / path.name
        shutil.copyfile(path, staged)
        code_hash = rs.hash_sources(staged)
        # A launcher's bytes never change; the release it runs is its version.
        release = launcher_release(staged) or ""
        keep = version_copy(code_hash, path.name)
        if not keep.exists():
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), keep)
    identity = {
        "key": script_id,
        "source": SOURCE,
        "dist_name": path.name,
        "dist_version": release,
        "code_hash": code_hash,
        "artifact_hash": None,
        "interval": "1m",
    }
    identity["version_hash"] = rs.sha256_hex(rs.canonical_json(identity))
    identity.update(model_hash=None, adapter_version=None, meta={"script_name": name})
    return identity


def follow(session, script_id: str, config: dict):
    """The script's link, made the first time it is seen; its current version recorded.

    Returns the link, or None when the script's file is gone.
    """
    from services.research_service import _record_version

    name = config.get("name") or script_id
    identity = script_identity(script_id, name)
    if identity is None:
        return None
    spec = rs.StrategySpec(script_id, SOURCE, "", identity["dist_name"], "")
    strategy_id = _record_version(session, spec, identity)
    link = session.get(rdb.ResearchScriptLink, strategy_id)
    if link is None:
        link = rdb.ResearchScriptLink(
            strategy_id=strategy_id, script_id=script_id, name=name, learned_tags=[]
        )
        session.add(link)
    elif link.name != name:
        link.name = name
    session.commit()
    return link


def sync_scripts(session) -> None:
    """Follow every script in the strategy host, recording any new version of its file."""
    for script_id, config in _host_configs().items():
        follow(session, script_id, config)


def installed_keys(session) -> set[str]:
    """Keys of followed scripts whose file is still in the strategy host."""
    return {
        link.script_id
        for link in session.query(rdb.ResearchScriptLink)
        if script_path(link.script_id).is_file()
    }


def _latest_version(session, strategy_id: int):
    return (
        session.query(rdb.ResearchStrategyVersion)
        .filter_by(strategy_id=strategy_id)
        .order_by(rdb.ResearchStrategyVersion.id.desc())
        .first()
    )


def _settings_version(session, strategy_id: int, settings) -> rdb.ResearchConfigVersion:
    """The settings a run used, kept as a configuration version (one per distinct set).

    They are the script's own and the sandbox's, copied here so a past run
    still says what it ran with after either changes.
    """
    from sqlalchemy import func

    values = settings.to_dict()
    config_hash = rs.sha256_hex(rs.canonical_json(values))
    row = (
        session.query(rdb.ResearchConfigVersion)
        .filter_by(strategy_id=strategy_id, config_hash=config_hash)
        .one_or_none()
    )
    if row is None:
        number = (
            session.query(func.coalesce(func.max(rdb.ResearchConfigVersion.version_no), 0))
            .filter_by(strategy_id=strategy_id)
            .scalar()
            + 1
        )
        row = rdb.ResearchConfigVersion(
            strategy_id=strategy_id,
            version_no=number,
            config=values,
            config_hash=config_hash,
            note="The script's schedule and exchange, and the sandbox's capital and leverage.",
        )
        session.add(row)
        session.commit()
    return row


def _experiment(session, link) -> int:
    from services.research_service import create_experiment

    title = f"{link.name} (Python strategy)"
    row = session.query(rdb.ResearchExperiment).filter_by(name=title).first()
    if row is not None:
        return row.id
    return create_experiment(
        title, objective=f"Backtests and Sandbox days of the Python strategy {link.script_id}."
    )["id"]


def _scenario(session, link, version, config_row, name: str, spec: dict, model: dict):
    """A scenario of the one script, reused when an identical one exists."""
    experiment_id = _experiment(session, link)
    for scenario in session.query(rdb.ResearchScenario).filter_by(
        experiment_id=experiment_id, name=name
    ):
        pairs = [
            (i.strategy_version_id, i.config_version_id)
            for i in session.query(rdb.ResearchScenarioInstance).filter_by(scenario_id=scenario.id)
        ]
        if pairs == [(version.id, config_row.id)] and scenario.dataset_spec == spec:
            return scenario
    exchange = spec["exchange"]
    scenario = rdb.ResearchScenario(
        experiment_id=experiment_id,
        name=name[:200],
        mode="ISOLATED",
        dataset_spec=spec,
        cost_model={"preset": COST_PRESET.get(exchange, DEFAULT_COST_PRESET), "slippage_bps": 0},
        execution_model=model,
        risk_policy={"max_gross_leverage": 1.0},
    )
    session.add(scenario)
    session.flush()
    session.add(
        rdb.ResearchScenarioInstance(
            scenario_id=scenario.id,
            position=0,
            strategy_version_id=version.id,
            config_version_id=config_row.id,
        )
    )
    session.commit()
    return scenario


def known_symbols(session, link) -> list[str]:
    """Symbols the script is known to read: from its own trades, and its last backtest."""
    symbols = set(_traded_symbols(link.learned_tags or []))
    last = (
        _runs(session, link.strategy_id, rdb.BACKTEST)
        .filter(rdb.ResearchRun.status == rdb.COMPLETED)
        .order_by(rdb.ResearchRun.id.desc())
        .first()
    )
    if last is not None and last.provenance:
        dataset = session.get(rdb.ResearchDataset, last.provenance.get("dataset_id") or 0)
        if dataset is not None:
            symbols |= set(dataset.spec.get("symbols") or [])
    return sorted(symbols)


# --------------------------------------------------------------------------- #
# Backtests
# --------------------------------------------------------------------------- #


def backtest(user_id: str, script_id: str, start: str, end: str) -> dict:
    """Backtest a Python strategy, unchanged, over a window of past days.

    It runs on the script's own schedule, weekdays and exchange and the
    sandbox's money. It loads history from before ``start``, so a script that
    reads past bars first decides from the first day.
    """
    first, last = backtest_window(start, end)
    session = rdb.db_session
    config = _host_configs().get(script_id)
    if config is None:
        raise HostedError("That Python strategy is not in Python Strategies.")
    link = follow(session, script_id, config)
    if link is None:
        raise HostedError("That Python strategy's file is missing.")
    version = _latest_version(session, link.strategy_id)
    settings = script_settings(config)
    if config.get("symbol_list") and not settings.symbols:
        raise HostedError(
            "This strategy's symbol list no longer exists. Choose a list on its Schedule tab."
        )
    run, created = enqueue_backtest(
        session, user_id, link, version, settings, first, last, f"{link.name}, {start} to {end}"
    )
    return {**run, "created": created}


def backtest_window(start: str, end: str) -> tuple[date, date]:
    """The first and last day a backtest covers, refused unless they are past days in order."""
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise HostedError("Dates must look like 2026-01-31.") from exc
    if last < first:
        raise HostedError("The last day must be on or after the first day.")
    if last >= datetime.now(IST).date():
        raise HostedError("A backtest covers past days only. End it yesterday or earlier.")
    return first, last


def enqueue_backtest(session, user_id, link, version, settings, first, last, name):
    """Queue a replay of one version of a followed script on ``settings``: ``(run, created)``.

    An identical scenario and its run are reused (research_service.enqueue_run),
    so asking for the same replay twice runs it once. It loads history from
    before ``first``, so a script that reads past bars decides from the first day.
    """
    from services import research_service as svc

    config_row = _settings_version(session, link.strategy_id, settings)
    history_from = first - timedelta(days=svc.DEFAULT_WARMUP_DAYS)
    spec = {
        # Its list is known before it runs, so all of it is downloaded up front.
        "symbols": sorted(set(known_symbols(session, link)) | set(settings.symbols)),
        "exchange": settings.exchange,
        "interval": "1m",
        "start": history_from.isoformat(),
        "end": last.isoformat(),
        "source": "historify",
    }
    scenario = _scenario(
        session,
        link,
        version,
        config_row,
        name,
        spec,
        {"fill": "script_replay", "trade_from": first.isoformat()},
    )
    return svc.enqueue_run(scenario.id, user_id)


# --------------------------------------------------------------------------- #
# Recognising its orders
# --------------------------------------------------------------------------- #


def run_windows(script_id: str) -> list[tuple[datetime, datetime]]:
    """When the script ran, as naive IST times, from its run logs.

    The host names each run's log by its start; the log's last write is taken
    as its end. Logs past the host's retention are gone, so this only ever
    covers recent runs; what it teaches is kept in the link.
    """
    windows = []
    prefix = f"{script_id}_"
    for path in sorted(LOG_DIR.glob(f"{script_id}_*_IST.log")):
        stamp = path.name[len(prefix) : -len("_IST.log")]
        try:
            began = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        ended = datetime.fromtimestamp(path.stat().st_mtime, IST).replace(tzinfo=None)
        windows.append((began, max(ended + RUN_END_SLACK, began)))
    return windows


def _tags_in(windows) -> set[str]:
    from database.sandbox_db import SandboxOrders, db_session

    tags: set[str] = set()
    try:
        for began, ended in windows:
            for (tag,) in (
                db_session.query(SandboxOrders.strategy)
                .filter(
                    SandboxOrders.order_timestamp >= began,
                    SandboxOrders.order_timestamp <= ended,
                )
                .distinct()
            ):
                if tag and tag != SQUARE_OFF_TAG and not tag.startswith(RESEARCH_TAG_PREFIX):
                    tags.add(tag)
    finally:
        db_session.remove()
    return tags


def learn_tags(session, links) -> None:
    """Learn the strategy names each script's orders carry, from when it was running.

    A name seen while two scripts were both running belongs to neither, unless
    it is one of theirs by name: the STRATEGY_NAME or STRATEGY_ID the host
    gives that script. That is what lets scripts that run the same hours be
    told apart. Any other shared name is never guessed. What is learned is
    kept, so a day stays recognisable after its run log is pruned.
    """
    seen = {link.strategy_id: _tags_in(run_windows(link.script_id)) for link in links}
    claims = Counter(tag for tags in seen.values() for tag in tags)

    def named_after(tag: str):
        """The one script seeing this name whose own name or id it is, if any."""
        owners = [
            link
            for link in links
            if tag in seen[link.strategy_id]
            and tag.strip().casefold() in {link.script_id.casefold(), (link.name or "").casefold()}
        ]
        return owners[0] if len(owners) == 1 else None

    for link in links:
        mine = {
            tag for tag in seen[link.strategy_id] if claims[tag] == 1 or named_after(tag) is link
        }
        known = set(link.learned_tags or [])
        if not mine <= known:
            link.learned_tags = sorted(known | mine)
    session.commit()


# --------------------------------------------------------------------------- #
# Recording its Sandbox days
# --------------------------------------------------------------------------- #


def _epoch(naive_ist: datetime) -> int:
    return int(IST.localize(naive_ist).timestamp())


def _day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def tagged_fills(tags, day: date) -> list:
    """The day's fills that belong to the script: its own, then the square-offs of what it held.

    The sandbox squares off its net position in a symbol, so a square-off is
    given to the script only up to what the script held, in the direction
    that closes it.
    """
    from database.sandbox_db import SandboxTrades, db_session
    from portfolio.strategy_pnl import attribute
    from research.fills import Fill

    tags = [tags] if isinstance(tags, str) else list(tags)
    start, end = _day_window(day)
    try:
        rows = (
            db_session.query(SandboxTrades)
            .filter(
                SandboxTrades.strategy.in_([*tags, SQUARE_OFF_TAG]),
                SandboxTrades.trade_timestamp >= start,
                SandboxTrades.trade_timestamp < end,
            )
            .order_by(SandboxTrades.trade_timestamp, SandboxTrades.id)
            .all()
        )
        trades = [
            {
                "strategy": r.strategy,
                "symbol": r.symbol,
                "exchange": r.exchange,
                "product": r.product,
                "action": r.action,
                "quantity": r.quantity,
                "price": float(r.price),
                "timestamp": r.trade_timestamp,
            }
            for r in rows
        ]
    finally:
        db_session.remove()
    return [
        Fill(_epoch(f["timestamp"]), f["symbol"], f["qty"], f["price"], f["reason"])
        for f in attribute(trades, tags)
    ]


def _learned_order_names(script_id: str) -> list[str]:
    """The names a hosted script's orders carry in the Sandbox, learned first so a run that ended moments ago counts.

    Learned across every hosted script, as the nightly job does (record_hosted),
    never from this one alone: learn_tags can tell a name two scripts shared
    while both ran only when it sees both. Seen alone, the shared name looked
    like this script's, and was saved as such for good.
    """
    configs = _host_configs()
    if script_id not in configs:
        return []
    session = rdb.db_session
    links = [
        link
        for sid, config in configs.items()
        if (link := follow(session, sid, config)) is not None
    ]
    learn_tags(session, links)
    mine = next((link for link in links if link.script_id == script_id), None)
    return list(mine.learned_tags or []) if mine is not None else []


def open_sandbox_positions(script_id: str) -> list[dict]:
    """The script's open Sandbox positions, from every order it is known to have placed.

    Its order names are learned first, so a run that ended moments ago counts.
    A name it shared with another script while both ran is never learned,
    and positions under it are not found here.
    """
    from database.sandbox_db import SandboxTrades
    from database.sandbox_db import db_session as sandbox_session
    from portfolio.strategy_pnl import attribute, book

    tags = _learned_order_names(script_id)
    if not tags:
        return []
    try:
        rows = [
            {
                "strategy": r.strategy,
                "symbol": r.symbol,
                "exchange": r.exchange,
                "product": r.product,
                "action": r.action,
                "quantity": r.quantity,
                "price": float(r.price),
                "timestamp": r.trade_timestamp,
            }
            for r in sandbox_session.query(SandboxTrades)
            .filter(SandboxTrades.strategy.in_([*tags, SQUARE_OFF_TAG]))
            .order_by(SandboxTrades.trade_timestamp, SandboxTrades.id)
        ]
    finally:
        sandbox_session.remove()
    positions = book(attribute(rows, tags), {}, datetime.now(IST).date())["positions"]
    return [p for p in positions if p["quantity"]]


def open_sandbox_orders(script_id: str) -> list[dict]:
    """The script's resting Sandbox orders (open or trigger pending).

    Found by the same learned order names as open_sandbox_positions, with the same blind
    spot: a name it shared with another script while both ran is never learned.
    """
    from database.sandbox_db import SandboxOrders
    from database.sandbox_db import db_session as sandbox_session

    tags = _learned_order_names(script_id)
    if not tags:
        return []
    try:
        return [
            {
                "orderid": o.orderid,
                "symbol": o.symbol,
                "exchange": o.exchange,
                "status": o.order_status,
            }
            for o in sandbox_session.query(SandboxOrders).filter(
                SandboxOrders.strategy.in_(tags),
                SandboxOrders.order_status.in_(("open", "trigger pending")),
            )
        ]
    finally:
        sandbox_session.remove()


def traded_days(tags) -> list[date]:
    """Every IST date the sandbox has a trade with one of these tags, oldest first."""
    from sqlalchemy import func

    from database.sandbox_db import SandboxTrades, db_session

    tags = [tags] if isinstance(tags, str) else list(tags)
    if not tags:
        return []
    try:
        rows = (
            db_session.query(func.date(SandboxTrades.trade_timestamp))
            .filter(SandboxTrades.strategy.in_(tags))
            .distinct()
            .all()
        )
    finally:
        db_session.remove()
    return sorted(date.fromisoformat(str(r[0])) for r in rows if r[0])


def _traded_symbols(tags) -> list[str]:
    from database.sandbox_db import SandboxTrades, db_session

    if not tags:
        return []
    try:
        rows = (
            db_session.query(SandboxTrades.symbol)
            .filter(SandboxTrades.strategy.in_(list(tags)))
            .distinct()
            .all()
        )
    finally:
        db_session.remove()
    return sorted(r[0] for r in rows if r[0])


def _day_over(config: dict | None, day: date, now: datetime) -> bool:
    """Whether the script is done with ``day``: no more trades of its can arrive."""
    if day < now.date():
        return True
    if day > now.date() or (config or {}).get("is_running"):
        return False
    stop = (config or {}).get("schedule_stop") or DEFAULT_STOP
    hours, minutes = (int(x) for x in stop.split(":"))
    return now.time() >= time(hours, minutes)


def _runs(session, strategy_id: int, environment: str):
    from services.research_service import strategy_scenarios

    scenarios = strategy_scenarios(session, strategy_id) or [-1]
    return session.query(rdb.ResearchRun).filter(
        rdb.ResearchRun.scenario_id.in_(scenarios),
        rdb.ResearchRun.environment == environment,
    )


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


def _fetch_bars(fills, exchange: str, day: date) -> None:
    """Ask Historify for the day's minute bars of what was traded. Best effort.

    A day recorded without them is marked only at its fills, so its intraday
    drawdown is unknown; the day is recorded again once they are stored.
    """
    from database.auth_db import get_first_available_api_key
    from services.historify_service import create_and_start_job

    try:
        api_key = get_first_available_api_key()
        if not api_key:
            return
        ok, body, _status = create_and_start_job(
            "custom",
            [{"symbol": s, "exchange": exchange} for s in sorted({f.symbol for f in fills})],
            "1m",
            day.isoformat(),
            day.isoformat(),
            api_key,
            config={"source": "research", "hosted_day": day.isoformat()},
        )
        if not ok:
            logger.warning("Research: minute bars for a hosted day could not be fetched: %s", body)
    except Exception:
        logger.exception("Research: minute bars for a hosted day could not be fetched")


def _clear_result(session, run_id: int) -> None:
    """Remove what ``save_result`` wrote for a run, so the run can be booked again."""
    for model in (
        rdb.LedgerOrder,
        rdb.LedgerFill,
        rdb.LedgerTrade,
        rdb.LedgerPosition,
        rdb.LedgerEvent,
        rdb.ResearchArtifact,
    ):
        session.query(model).filter(model.run_id == run_id).delete(synchronize_session=False)


def record_day(session, link, config: dict, day: date, run=None) -> int | None:
    """Record one finished day of a script's Sandbox trading as a HOSTED run. Returns its id.

    None when there is nothing to record: no fills that day. With ``run``, a
    day first recorded without minute bars is booked again in place once they
    are stored, and left alone until then.
    """
    from portfolio.costs import schedule_for
    from research.fills import book_from_fills
    from research.worker import save_result

    fills = tagged_fills(link.learned_tags or [], day)
    if not fills:
        return None
    settings = script_settings(config)
    frames, marks = _frames_for(fills, settings.exchange, day)
    if run is not None and marks == "fills":
        return None
    version = _latest_version(session, link.strategy_id)
    config_row = _settings_version(session, link.strategy_id, settings)
    scenario = _scenario(
        session,
        link,
        version,
        config_row,
        f"{link.name}: Sandbox days",
        {
            "symbols": [],
            "exchange": settings.exchange,
            "interval": "1m",
            "start": "1970-01-01",
            "end": "2999-12-31",
            "source": "sandbox",
        },
        {"fill": "sandbox"},
    )
    cost_model = scenario.cost_model
    result = book_from_fills(
        frames,
        rs.Config(
            capital=settings.capital,
            leverage=settings.leverage,
            entry_time=settings.entry_time,
            exit_time=settings.exit_time,
            exchange=settings.exchange,
            universe=tuple(frames),
        ),
        f"{link.name} (Sandbox)",
        schedule_for(cost_model["preset"], cost_model.get("overrides")),
        fills,
        slippage_bps=float(cost_model.get("slippage_bps", 0.0)),
    )
    # Written whole in one commit: nobody ever sees it half recorded.
    if run is None:
        run = rdb.ResearchRun(
            scenario_id=scenario.id,
            status=rdb.COMPLETED,
            progress=1.0,
            environment=rdb.HOSTED,
            trading_date=day.isoformat(),
            started_at=rdb.utc_now(),
        )
        session.add(run)
        session.flush()
    else:
        _clear_result(session, run.id)
    run.finished_at = rdb.utc_now()
    run.metrics, run.attribution = save_result(rdb, session, run.id, result)
    if marks == "fills":
        # Marked only where it traded: what it went through in between is unknown.
        run.metrics = {**run.metrics, "max_drawdown_pct": None}
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
        "scenario_id": scenario.id,
        "experiment_id": scenario.experiment_id,
        "execution_environment": rdb.HOSTED,
        "recorded_from": "sandbox trades tagged " + ", ".join(link.learned_tags or []),
        "fills": len(fills),
        "marks": marks,
        "cost_model": cost_model,
        # Research records the version in place when it records the day. A
        # script edited earlier in the day is not told apart.
        "version_basis": "current when recorded",
    }
    session.commit()
    logger.info("Research: recorded %s on %s as run=%s (%s)", link.script_id, day, run.id, marks)
    if marks == "fills":
        _fetch_bars(fills, settings.exchange, day)
    return run.id


def record_hosted(now: datetime | None = None) -> list[int]:
    """Follow every script, learn its orders, and record its finished, unrecorded days.

    Never raises. One recorder at a time in this process, so a page load and
    the daily job cannot record the same day twice.
    """
    if not _record_lock.acquire(blocking=False):
        return []
    recorded: list[int] = []
    session = rdb.db_session
    try:
        now = now or datetime.now(IST).replace(tzinfo=None)
        configs = _host_configs()
        try:
            links = [
                link
                for sid, config in configs.items()
                if (link := follow(session, sid, config)) is not None
            ]
            learn_tags(session, links)
        except Exception:
            session.rollback()
            logger.exception("Research: following the Python strategies failed")
            return recorded
        for link in links:
            try:
                done = {
                    run.trading_date: run for run in _runs(session, link.strategy_id, rdb.HOSTED)
                }
                config = configs.get(link.script_id)
                for day in traded_days(link.learned_tags or []):
                    run = done.get(day.isoformat())
                    if run is not None and (run.provenance or {}).get("marks") != "fills":
                        continue
                    if run is None and not _day_over(config, day, now):
                        continue
                    run_id = record_day(session, link, config, day, run=run)
                    if run_id is not None:
                        recorded.append(run_id)
            except Exception:
                session.rollback()
                logger.exception(
                    "Research: recording the Python strategy %s failed", link.script_id
                )
    finally:
        _record_lock.release()
    return recorded


# --------------------------------------------------------------------------- #
# What the strategy's page shows
# --------------------------------------------------------------------------- #


def _run_row(run, scenario) -> dict:
    metrics = run.metrics or {}
    model = scenario.execution_model or {}
    return {
        "id": run.id,
        "status": run.status,
        "message": run.message,
        "progress": run.progress,
        "trading_date": run.trading_date,
        "from": model.get("trade_from") or scenario.dataset_spec.get("start"),
        "to": scenario.dataset_spec.get("end"),
        "net_pnl": metrics.get("net_pnl"),
        "return_pct": metrics.get("return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "trades": metrics.get("trades"),
        "win_rate_pct": metrics.get("win_rate"),
        "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def overview(script_id: str) -> dict:
    """Everything the strategy's Backtest, Sandbox days and Results tabs show."""
    session = rdb.db_session
    config = _host_configs().get(script_id)
    if config is None:
        raise HostedError("That Python strategy is not in Python Strategies.")
    record_hosted()
    link = follow(session, script_id, config)
    if link is None:
        raise HostedError("That Python strategy's file is missing.")
    settings = script_settings(config)

    def rows(environment):
        return [
            _run_row(run, session.get(rdb.ResearchScenario, run.scenario_id))
            for run in _runs(session, link.strategy_id, environment).order_by(
                rdb.ResearchRun.id.desc()
            )
        ]

    days = sorted(rows(rdb.HOSTED), key=lambda r: r["trading_date"] or "", reverse=True)
    pnl = [d["net_pnl"] for d in days if d["net_pnl"] is not None]
    return {
        "script_id": script_id,
        "name": link.name,
        "strategy_id": link.strategy_id,
        "settings": settings.to_dict(),
        "order_tags": link.learned_tags or [],
        "backtests": rows(rdb.BACKTEST),
        "sandbox_days": days,
        "summary": {
            "days": len(pnl),
            "net_pnl": sum(pnl) if pnl else None,
            "winning_days": sum(1 for p in pnl if p > 0),
            "trades": sum(d["trades"] or 0 for d in days),
            "best_day": max(pnl) if pnl else None,
            "worst_day": min(pnl) if pnl else None,
        },
    }

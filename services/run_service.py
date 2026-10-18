"""
Runs: Python strategies trading together in the Sandbox with their own money.

A Run is a scenario: a capital, a mode, a P&L policy, a limit, and members.
Each member is one version of a Python strategy, pinned (Research keeps every
version of a script's file), with its allocation and its configuration.

**One process per configuration.** Members whose configurations are identical
(portfolio/run_capital.fingerprint: the version, schedule, exchange, symbols
and sizing; not the capital) share one *source*. The strategy runs once, as
an entry in the strategy host trading a Sandbox account of its own at a
reference capital, and each member's account follows it
(sandbox/copy_engine.py), scaled to the member's own capital. A source is
shared across Runs too.

**Isolated or shared.** Isolated: each member has its own account, so its
money is its own. Shared: the Run has one account, the members' positions net
in it, and each member's share is read from the orders carrying its name.

**Days.** Before a day's trading every seat's capital is decided by the
policy (portfolio/run_capital.day_open), recorded as the day's rows, then
moved as transfers that carry a reference, so a restart part-way repeats
nothing. Each follow then gets that day's scale and limit. After the
square-off the day is booked from the accounts: closing value, charges, net.

**Edits** add a version that applies from the next trading day; the day that
is trading finishes on the version it started with.

Off unless Sandbox accounts are on (SANDBOX_ACCOUNTS_ENABLED). Members trade
NSE or BSE equity intraday, where the Sandbox's charges and day boundaries are
modelled; other exchanges come later.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import groupby

import pytz

from database import research_db as rdb
from portfolio import run_capital as rc
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

MODES = ("isolated", "shared")
EXCHANGES = ("NSE", "BSE")
#: The least capital a source trades: members then scale down from it, and a
#: source too small to buy one share of something a member could afford
#: would leave that member out.
REFERENCE_FLOOR = 10_000_000.0
SHARED_SEAT = "run"
#: When the day's open and close run (services/research_sandbox_service.py
#: schedules them): before the 09:15 open, and after the 15:15 square-off.
OPEN_TIME = (9, 5)
CLOSE_TIME = (15, 40)


class RunError(ValueError):
    """A Run request refused, said so a trader can fix it."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _today() -> date:
    return datetime.now(IST).date()


def _session():
    return rdb.db_session


def _is_trading_day(day: date) -> bool:
    from database.market_calendar_db import is_market_holiday

    return not is_market_holiday(day, "NSE")


def _next_trading_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    for _ in range(14):
        if _is_trading_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    return candidate


def _seat(script_id: str, label: str) -> str:
    return hashlib.sha256(f"{script_id}\n{label}".encode()).hexdigest()[:16]


def _event(run_id: int, kind: str, **detail) -> None:
    _session().add(rdb.StrategyRunEvent(run_id=run_id, kind=kind, detail=detail))


def _run(run_id: int, owner: str) -> rdb.StrategyRun:
    run = _session().get(rdb.StrategyRun, run_id)
    if run is None or run.owner != owner:
        raise RunError("That Run does not exist.")
    return run


def _versions(run_id: int) -> list:
    return (
        _session()
        .query(rdb.StrategyRunVersion)
        .filter_by(run_id=run_id)
        .order_by(rdb.StrategyRunVersion.number)
        .all()
    )


def version_in_force(run_id: int, day: date):
    """The version a Run trades on ``day``: the newest that applies by then."""
    chosen = None
    for version in _versions(run_id):
        if version.effective_from <= day.isoformat():
            chosen = version
    return chosen


def _members(version) -> list:
    return (
        _session()
        .query(rdb.StrategyRunMember)
        .filter_by(version_id=version.id)
        .order_by(rdb.StrategyRunMember.id)
        .all()
    )


# --------------------------------------------------------------------------- #
# Defining a Run
# --------------------------------------------------------------------------- #


def _definition(owner: str, name: str, capital, mode, policy, limit, members, loss_limit_pct):
    """Check a Run's definition and freeze each member's configuration."""
    from services.research_hosted_service import _host_configs, _latest_version, follow
    from services.symbol_list_service import get_list

    name = (name or "").strip()
    if not name or len(name) > 60:
        raise RunError("Give the Run a name of up to 60 characters.")
    try:
        capital = float(capital)
    except (TypeError, ValueError):
        capital = 0.0
    if capital <= 0:
        raise RunError("Give the Run some capital.")
    if mode not in MODES:
        raise RunError("Choose Isolated or Shared.")
    if policy not in rc.POLICIES:
        raise RunError("Choose what happens to each day's profit and loss.")
    if mode == "shared" and policy == "own":
        raise RunError(
            "In a shared Run the members trade one account, so their results cannot be kept "
            "apart. Choose Reset daily or Pooled, or make the Run Isolated."
        )
    if limit not in rc.LIMITS:
        raise RunError("Choose how the members' exposure limit is set.")
    if loss_limit_pct is not None:
        loss_limit_pct = float(loss_limit_pct)
        if not 0 < loss_limit_pct <= 100:
            raise RunError("A daily loss limit is a percentage between 0 and 100.")
    if not members:
        raise RunError("Add at least one strategy to the Run.")

    hosted = _host_configs()
    frozen, seats = [], set()
    session = _session()
    for raw in members:
        script_id = raw.get("script_id")
        config = hosted.get(script_id)
        if config is None:
            raise RunError("A strategy in this Run no longer exists at Python Strategies.")
        label = (raw.get("label") or config.get("name") or script_id).strip()[:60]
        seat = _seat(script_id, label)
        if seat in seats:
            raise RunError(f"{label} is in the Run twice. Give the second one another label.")
        seats.add(seat)
        exchange = (config.get("exchange") or "NSE").upper()
        if exchange not in EXCHANGES:
            raise RunError(f"{label} trades {exchange}. Runs take NSE and BSE strategies for now.")
        if not config.get("schedule_start"):
            raise RunError(f"Give {label} a schedule on its Schedule tab first.")
        kind = raw.get("allocation_kind")
        try:
            value = float(raw.get("allocation_value"))
        except (TypeError, ValueError):
            value = 0.0
        if kind not in ("fixed", "percent") or value <= 0:
            raise RunError(f"Give {label} an allocation: an amount, or a share of the Run.")
        sizing = raw.get("sizing") or "platform"
        if sizing not in rc.SIZINGS:
            raise RunError(f"Choose how {label} is sized.")
        link = follow(session, script_id, config)
        version = _latest_version(session, link.strategy_id) if link else None
        if version is None:
            raise RunError(f"{label}'s script file could not be read.")
        chosen = get_list(config.get("symbol_list"))
        settings = {
            "exchange": exchange,
            "schedule": {
                "start": config["schedule_start"],
                "stop": config.get("schedule_stop"),
                "days": [d.lower()[:3] for d in config.get("schedule_days") or []],
            },
            "symbols": list(chosen["symbols"]) if chosen else [],
            "script_name": config.get("name") or script_id,
            "file_name": version.dist_name,
            "code_hash": version.code_hash,
        }
        allocated = rc.allocation(capital, kind, value)
        dependent = bool(raw.get("capital_dependent"))
        fingerprint = rc.fingerprint(
            version.version_hash,
            exchange=exchange,
            schedule=settings["schedule"],
            symbols=settings["symbols"],
            settings={},
            sizing=sizing,
            capital_dependent=dependent,
            capital=allocated,
        )
        frozen.append(
            {
                "seat": seat,
                "label": label,
                "script_id": script_id,
                "strategy_version_id": version.id,
                "allocation_kind": kind,
                "allocation_value": value,
                "sizing": sizing,
                "capital_dependent": int(dependent),
                "settings": settings,
                "fingerprint": fingerprint,
                "order_name": f"{name} / {label}"[:100],
            }
        )
    return name, capital, loss_limit_pct, frozen


def _add_version(run, effective_from: date, capital, mode, policy, limit, loss_limit_pct, members):
    number = len(_versions(run.id)) + 1
    version = rdb.StrategyRunVersion(
        run_id=run.id,
        number=number,
        effective_from=effective_from.isoformat(),
        capital=capital,
        mode=mode,
        policy=policy,
        limit=limit,
        loss_limit_pct=loss_limit_pct,
    )
    session = _session()
    session.add(version)
    session.flush()
    for member in members:
        session.add(rdb.StrategyRunMember(version_id=version.id, **member))
    return version


def create(owner, name, *, capital, mode, policy, limit, members, loss_limit_pct=None) -> dict:
    """Define a Run. It trades once started."""
    from sandbox.accounts import enabled

    if not enabled():
        raise RunError("Runs need Sandbox accounts. Set SANDBOX_ACCOUNTS_ENABLED to true.")
    session = _session()
    name, capital, loss_limit_pct, frozen = _definition(
        owner, name, capital, mode, policy, limit, members, loss_limit_pct
    )
    if session.query(rdb.StrategyRun).filter_by(owner=owner, name=name).first():
        raise RunError(f"A Run named {name} already exists.")
    try:
        run = rdb.StrategyRun(owner=owner, name=name, status="draft")
        session.add(run)
        session.flush()
        _add_version(run, _today(), capital, mode, policy, limit, loss_limit_pct, frozen)
        _event(run.id, "created")
        session.commit()
    except Exception:
        session.rollback()
        raise
    return get(run.id, owner)


def edit(run_id, owner, *, capital, mode, policy, limit, members, loss_limit_pct=None) -> dict:
    """Change a Run. A draft changes at once; a trading Run from its next trading day."""
    session = _session()
    run = _run(run_id, owner)
    if run.status == "stopped":
        raise RunError("This Run has stopped. Start a new one instead.")
    current = version_in_force(run.id, _today()) or _versions(run.id)[-1]
    if mode != current.mode:
        raise RunError("A Run cannot change between Isolated and Shared. Start a new Run.")
    _name, capital, loss_limit_pct, frozen = _definition(
        owner, run.name, capital, mode, policy, limit, members, loss_limit_pct
    )
    effective = _today() if run.status == "draft" else _next_trading_day(_today())
    try:
        # A version not yet in force is replaced rather than stacked.
        for pending in _versions(run.id):
            if pending.effective_from >= effective.isoformat() and run.status != "draft":
                session.query(rdb.StrategyRunMember).filter_by(version_id=pending.id).delete()
                session.delete(pending)
        session.flush()
        _add_version(run, effective, capital, mode, policy, limit, loss_limit_pct, frozen)
        _event(run.id, "edited", effective_from=effective.isoformat())
        session.commit()
    except Exception:
        session.rollback()
        raise
    return get(run.id, owner)


# --------------------------------------------------------------------------- #
# Seats, sources and follows
# --------------------------------------------------------------------------- #


def _seats(version, members) -> dict[str, list]:
    """Which members trade in which seat: one each, or all in the Run's one."""
    if version.mode == "shared":
        return {SHARED_SEAT: members}
    return {m.seat: [m] for m in members}


def _account(run, seat: str):
    return _session().query(rdb.StrategyRunAccount).filter_by(run_id=run.id, seat=seat).first()


def _open_account(run, seat: str, label: str, capital: Decimal) -> str:
    from sandbox import accounts

    opened = accounts.create(
        run.owner,
        f"{run.name} / {label}",
        float(capital),
        "run" if seat == SHARED_SEAT else "member",
        run_id=run.id,
    )
    session = _session()
    session.add(rdb.StrategyRunAccount(run_id=run.id, seat=seat, principal=opened["principal"]))
    session.commit()
    return opened["principal"]


def _ensure_source(run, member, allocated: Decimal) -> rdb.StrategyRunSource:
    """The source trading this member's configuration, started if it was not."""
    import blueprints.python_strategy as host
    from database.auth_db import get_api_key_for_tradingview
    from sandbox import accounts
    from services.research_hosted_service import version_copy

    session = _session()
    source = session.query(rdb.StrategyRunSource).filter_by(fingerprint=member.fingerprint).first()
    settings = member.settings
    if source is None:
        if not get_api_key_for_tradingview(run.owner):
            raise RunError("Generate an API key first: a Run's strategies trade with it.")
        reference = max(REFERENCE_FLOOR, float(allocated))
        if member.capital_dependent:
            reference = float(allocated)
        name = f"{settings['script_name']} #{member.fingerprint[:6]}"
        opened = accounts.create(
            run.owner,
            f"Source: {name}",
            reference,
            "source",
            fingerprint=member.fingerprint,
        )
        source = rdb.StrategyRunSource(
            fingerprint=member.fingerprint,
            owner=run.owner,
            script_id=member.script_id,
            strategy_version_id=member.strategy_version_id,
            name=name,
            host_id=f"src_{member.fingerprint[:12]}",
            principal=opened["principal"],
            reference_capital=reference,
            settings=settings,
            status="active",
        )
        session.add(source)
        session.commit()
    elif source.status != "active":
        source.status = "active"
        session.commit()
    host.add_source(
        source.host_id,
        name=source.name,
        script_file=version_copy(settings["code_hash"], settings["file_name"]),
        owner=run.owner,
        exchange=settings["exchange"],
        schedule=settings["schedule"],
        symbols=settings["symbols"],
        account_token=source.principal.rsplit("~", 1)[1],
        reference_capital=source.reference_capital,
    )
    return source


def _follow_member(run, version, member, source, principal: str, capital, limit) -> None:
    from sandbox.copy_engine import set_follow

    set_follow(
        source.principal,
        principal,
        member.order_name,
        scale=Decimal(str(capital)) / Decimal(str(source.reference_capital)),
        limit=limit,
        sizing=member.sizing,
        shared=version.mode == "shared",
        run_id=run.id,
        member_id=member.id,
    )


def _stop_following(run, keep: set[tuple[str, str]] | None = None) -> None:
    """Deactivate the Run's follows, except (follower, order name) pairs in ``keep``."""
    from database.sandbox_db import SandboxFollow, db_session

    for follow in SandboxFollow.query.filter_by(run_id=run.id, active=True):
        if keep and (follow.follower, follow.order_name) in keep:
            continue
        follow.active = False
    db_session.commit()


# --------------------------------------------------------------------------- #
# Days
# --------------------------------------------------------------------------- #


def _days(run_id: int, day: date) -> list:
    return (
        _session()
        .query(rdb.StrategyRunDay)
        .filter_by(run_id=run_id, trade_date=day.isoformat())
        .all()
    )


def open_day(run_id: int, day: date) -> None:
    """Set every seat's capital for ``day`` and point its follows at today's scale.

    Idempotent: the decision is recorded once, as the day's rows, and each
    transfer carries a reference, so running it again repeats nothing.
    """
    from sandbox import accounts

    session = _session()
    run = session.get(rdb.StrategyRun, run_id)
    if run is None or run.status != "active" or not _is_trading_day(day):
        return
    version = version_in_force(run.id, day)
    members = _members(version)
    seats = _seats(version, members)
    allocated = {
        seat: sum(
            (rc.allocation(version.capital, m.allocation_kind, m.allocation_value) for m in group),
            Decimal("0"),
        )
        for seat, group in seats.items()
    }
    rows = {row.seat: row for row in _days(run.id, day)}
    if not rows:
        closing = {}
        for seat in seats:
            held = _account(run, seat)
            value = accounts.equity(held.principal) if held else None
            if value is not None:
                closing[seat] = value
        opened = rc.day_open(version.policy, allocated, closing)
        for seat, (capital, transfer) in opened.items():
            rows[seat] = rdb.StrategyRunDay(
                run_id=run.id,
                version_id=version.id,
                seat=seat,
                trade_date=day.isoformat(),
                opening_capital=float(capital),
                transfer=float(transfer) if seat in closing else 0.0,
                limit_amount=float(rc.limit_for(version.limit, capital, allocated[seat])),
            )
            session.add(rows[seat])
        _event(run.id, "day_opened", day=day.isoformat(), version=version.number)
        session.commit()

    keep = set()
    for seat, group in seats.items():
        row = rows[seat]
        held = _account(run, seat)
        label = group[0].label if seat != SHARED_SEAT else "shared"
        if held is None:
            principal = _open_account(run, seat, label, Decimal(str(row.opening_capital)))
        else:
            principal = held.principal
            if row.transfer:
                accounts.move_money(
                    principal,
                    row.transfer,
                    "transfer",
                    reason=f"{version.policy} policy at the open of {day.isoformat()}",
                    reference=f"run{run.id}:{seat}:{day.isoformat()}",
                )
        for member in group:
            share = rc.allocation(version.capital, member.allocation_kind, member.allocation_value)
            capital = Decimal(str(row.opening_capital)) * share / allocated[seat]
            source = _ensure_source(run, member, share)
            _follow_member(run, version, member, source, principal, capital, row.limit_amount)
            keep.add((principal, member.order_name))
    _stop_following(run, keep)


def _book(principal: str, day: date, opening: float) -> dict | None:
    """What an account made on ``day``: its value now against what it opened with.

    Charges are the day's charge events and gross is net before them. None
    when the account has no funds row to read.
    """
    from database.sandbox_db import SandboxAccountEvent, SandboxTrades
    from sandbox import accounts

    value = accounts.equity(principal)
    if value is None:
        return None
    charges = -sum(
        (
            Decimal(str(e.amount))
            for e in SandboxAccountEvent.query.filter_by(
                principal=principal, kind="charges", trade_date=day
            )
        ),
        Decimal("0"),
    )
    start = IST.localize(datetime.combine(day, datetime.min.time())).replace(tzinfo=None)
    trades = SandboxTrades.query.filter(
        SandboxTrades.user_id == principal,
        SandboxTrades.trade_timestamp >= start,
        SandboxTrades.trade_timestamp < start + timedelta(days=1),
    ).count()
    net = value - Decimal(str(opening))
    return {
        "closing_balance": float(value),
        "net": float(net),
        "charges": float(charges),
        "gross": float(net + charges),
        "trades": trades,
    }


def close_day(run_id: int, day: date) -> None:
    """Book ``day`` for every seat from its account. Idempotent."""
    session = _session()
    for row in _days(run_id, day):
        if row.closed_at is not None:
            continue
        held = session.query(rdb.StrategyRunAccount).filter_by(run_id=run_id, seat=row.seat).first()
        booked = _book(held.principal, day, row.opening_capital) if held else None
        if booked is None:
            continue
        for field, value in booked.items():
            setattr(row, field, value)
        row.closed_at = rdb.utc_now()
    session.commit()


def _needed_fingerprints(day: date) -> set[str]:
    """The configurations some active Run trades on ``day``."""
    needed = set()
    for run in _session().query(rdb.StrategyRun).filter_by(status="active"):
        version = version_in_force(run.id, day)
        if version is not None:
            needed |= {m.fingerprint for m in _members(version)}
    return needed


def retire_sources(day: date) -> None:
    """Stop the sources no active Run trades on ``day``; they start again if needed."""
    import blueprints.python_strategy as host

    needed = _needed_fingerprints(day)
    session = _session()
    for source in session.query(rdb.StrategyRunSource).filter_by(status="active"):
        if source.fingerprint not in needed:
            host.remove_source(source.host_id)
            source.status = "stopped"
            logger.info("Run source %s retired: no Run trades it from %s", source.name, day)
    session.commit()


def open_days(now: datetime | None = None) -> None:
    """Open today's day for every active Run (the 09:05 job, and at start-up)."""
    day = (now or datetime.now(IST)).date()
    for run in _session().query(rdb.StrategyRun).filter_by(status="active").all():
        try:
            open_day(run.id, day)
        except Exception:
            _session().rollback()
            logger.exception("Run %s: today's open failed", run.id)


def close_days(now: datetime | None = None) -> None:
    """Book today for every Run that traded it, then retire unneeded sources (15:40)."""
    day = (now or datetime.now(IST)).date()
    for (run_id,) in (
        _session()
        .query(rdb.StrategyRunDay.run_id)
        .filter_by(trade_date=day.isoformat())
        .distinct()
        .all()
    ):
        try:
            close_day(run_id, day)
        except Exception:
            _session().rollback()
            logger.exception("Run %s: today's close failed", run_id)
    retire_sources(_next_trading_day(day))


def schedule(scheduler) -> None:
    """Add the day's open and close to a running APScheduler."""
    from apscheduler.triggers.cron import CronTrigger

    from utils.db_sessions import releases_scoped_sessions

    for job_id, (hour, minute), func in (
        ("runs_open_days", OPEN_TIME, open_days),
        ("runs_close_days", CLOSE_TIME, close_days),
    ):
        scheduler.add_job(
            func=releases_scoped_sessions(func),
            trigger=CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri", timezone=IST),
            id=job_id,
            replace_existing=True,
        )


def catch_up(now: datetime | None = None) -> None:
    """After a restart: book a day that ended unbooked, open today if it is trading."""
    now = now or datetime.now(IST)
    if (now.hour, now.minute) >= CLOSE_TIME:
        close_days(now)
    elif (now.hour, now.minute) >= OPEN_TIME:
        open_days(now)


# --------------------------------------------------------------------------- #
# A Run's life
# --------------------------------------------------------------------------- #


def start(run_id: int, owner: str) -> dict:
    """Start trading a Run: from now if today is trading, else from the next open."""
    from sandbox.accounts import enabled
    from sandbox.engines import ensure_running

    if not enabled():
        raise RunError("Runs need Sandbox accounts. Set SANDBOX_ACCOUNTS_ENABLED to true.")
    session = _session()
    run = _run(run_id, owner)
    if run.status == "active":
        return get(run.id, owner)
    if run.status == "stopped":
        raise RunError("This Run has stopped. Start a new one instead.")
    run.status = "active"
    _event(run.id, "started")
    session.commit()
    ensure_running()
    now = datetime.now(IST)
    if (now.hour, now.minute) < CLOSE_TIME:
        open_day(run.id, now.date())
    return get(run.id, owner)


def pause(run_id: int, owner: str) -> dict:
    """Stop copying into the Run's accounts. What they hold stays until the square-off."""
    session = _session()
    run = _run(run_id, owner)
    if run.status != "active":
        raise RunError("Only a trading Run can be paused.")
    _stop_following(run)
    run.status = "paused"
    _event(run.id, "paused")
    session.commit()
    return get(run.id, owner)


def kill(run_id: int, owner: str) -> dict:
    """Pause the Run and close everything its accounts hold, now."""
    from sandbox.squareoff_manager import SquareOffManager

    run = _run(run_id, owner)
    if run.status == "active":
        pause(run_id, owner)
    manager = SquareOffManager()
    for held in _session().query(rdb.StrategyRunAccount).filter_by(run_id=run.id):
        manager._flatten_mis(held.principal)
    _event(run.id, "killed")
    _session().commit()
    return get(run.id, owner)


def stop(run_id: int, owner: str) -> dict:
    """End a Run for good. Its accounts keep their history; nothing new is copied."""
    session = _session()
    run = _run(run_id, owner)
    if run.status == "stopped":
        return get(run.id, owner)
    _stop_following(run)
    run.status = "stopped"
    _event(run.id, "stopped")
    session.commit()
    return get(run.id, owner)


def delete(run_id: int, owner: str) -> None:
    """Delete a Run that never traded. One that did is stopped instead, keeping its record."""
    session = _session()
    run = _run(run_id, owner)
    if run.status != "draft":
        raise RunError("A Run that has traded keeps its record. Stop it instead.")
    for version in _versions(run.id):
        session.query(rdb.StrategyRunMember).filter_by(version_id=version.id).delete()
        session.delete(version)
    session.query(rdb.StrategyRunEvent).filter_by(run_id=run.id).delete()
    session.delete(run)
    session.commit()


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _version_dict(version) -> dict:
    return {
        "number": version.number,
        "effective_from": version.effective_from,
        "capital": version.capital,
        "mode": version.mode,
        "policy": version.policy,
        "limit": version.limit,
        "loss_limit_pct": version.loss_limit_pct,
        "members": [
            {
                "id": m.id,
                "seat": m.seat,
                "label": m.label,
                "script_id": m.script_id,
                "allocation_kind": m.allocation_kind,
                "allocation_value": m.allocation_value,
                "allocated": float(
                    rc.allocation(version.capital, m.allocation_kind, m.allocation_value)
                ),
                "sizing": m.sizing,
                "capital_dependent": bool(m.capital_dependent),
                "settings": m.settings,
                "fingerprint": m.fingerprint,
                "order_name": m.order_name,
            }
            for m in _members(version)
        ],
    }


def get(run_id: int, owner: str) -> dict:
    run = _run(run_id, owner)
    today = _today()
    versions = _versions(run.id)
    current = version_in_force(run.id, today) or versions[-1]
    upcoming = [v for v in versions if v.effective_from > today.isoformat()]
    session = _session()
    days = [
        {
            "seat": d.seat,
            "trade_date": d.trade_date,
            "opening_capital": d.opening_capital,
            "transfer": d.transfer,
            "limit": d.limit_amount,
            "closing_balance": d.closing_balance,
            "gross": d.gross,
            "charges": d.charges,
            "net": d.net,
            "trades": d.trades,
            "closed": d.closed_at is not None,
        }
        for d in session.query(rdb.StrategyRunDay)
        .filter_by(run_id=run.id)
        .order_by(rdb.StrategyRunDay.trade_date, rdb.StrategyRunDay.seat)
    ]
    return {
        "id": run.id,
        "name": run.name,
        "status": run.status,
        "version": _version_dict(current),
        "upcoming": _version_dict(upcoming[-1]) if upcoming else None,
        "accounts": {
            a.seat: a.principal
            for a in session.query(rdb.StrategyRunAccount).filter_by(run_id=run.id)
        },
        "days": days,
    }


def list_runs(owner: str) -> list[dict]:
    session = _session()
    return [
        get(run.id, owner)
        for run in session.query(rdb.StrategyRun)
        .filter_by(owner=owner)
        .order_by(rdb.StrategyRun.created_at.desc())
    ]


# --------------------------------------------------------------------------- #
# P&L: every Run, one strategy in a Run, the main account, standalone strategies
# --------------------------------------------------------------------------- #


def _in_window(day: str, start: str | None, end: str | None) -> bool:
    return (start is None or day >= start) and (end is None or day <= end)


def _run_daily(run, seat: str | None, start, end) -> list[dict]:
    """A Run's days (or one seat's), summed by date; today's read live from the account."""
    session = _session()
    query = session.query(rdb.StrategyRunDay).filter_by(run_id=run.id)
    if seat is not None:
        query = query.filter_by(seat=seat)
    today = _today().isoformat()
    by_day: dict[str, dict] = {}
    for row in query.order_by(rdb.StrategyRunDay.trade_date):
        if not _in_window(row.trade_date, start, end):
            continue
        booked = None
        if row.closed_at is not None:
            booked = {
                "gross": row.gross,
                "charges": row.charges,
                "net": row.net,
                "trades": row.trades,
            }
        elif row.trade_date == today:
            held = (
                session.query(rdb.StrategyRunAccount)
                .filter_by(run_id=run.id, seat=row.seat)
                .first()
            )
            booked = _book(held.principal, _today(), row.opening_capital) if held else None
        if booked is None:
            continue  # a past day left unbooked: catch_up books it
        day = by_day.setdefault(
            row.trade_date,
            {"date": row.trade_date, "opening": 0.0, "transfer": 0.0, "pnl": 0.0,
             "charges": 0.0, "net": 0.0, "trades": 0},
        )  # fmt: skip
        day["opening"] += row.opening_capital
        day["transfer"] += row.transfer or 0.0
        day["pnl"] += booked["gross"] or 0.0
        day["charges"] += booked["charges"] or 0.0
        day["net"] += booked["net"] or 0.0
        day["trades"] += booked["trades"] or 0
    return [by_day[d] for d in sorted(by_day)]


def _seat_members(run, seat: str) -> list:
    """A seat's member row in every version that has it, oldest first."""
    return [m for version in _versions(run.id) for m in _members(version) if m.seat == seat]


def _shared_member_daily(run, seat: str, start, end) -> list[dict]:
    """One strategy's days inside a shared Run: its own orders and its square-off share."""
    from portfolio.strategy_pnl import attribute, daily_results
    from services.strategy_performance_service import sandbox_trades

    held = _account(run, SHARED_SEAT)
    if held is None:
        return []
    names = {m.order_name for m in _seat_members(run, seat)}
    results = []
    # Day by day, as Runs are intraday: what a netted square-off leaves a
    # member holding on paper must not carry into the next day's figures.
    for _day, trades in groupby(sandbox_trades(held.principal), lambda t: t["timestamp"].date()):
        results += daily_results(attribute(trades, names), _today())
    openings = {
        row.trade_date: row
        for row in _session().query(rdb.StrategyRunDay).filter_by(run_id=run.id, seat=SHARED_SEAT)
    }
    out = []
    for day in results:
        if not _in_window(day["date"], start, end) or day["date"] not in openings:
            continue
        row = openings[day["date"]]
        version = _session().get(rdb.StrategyRunVersion, row.version_id)
        group = _members(version)  # the allocations that day traded on
        total = sum(
            (rc.allocation(version.capital, m.allocation_kind, m.allocation_value) for m in group),
            Decimal("0"),
        )
        mine = next((m for m in group if m.seat == seat), None)
        share = (
            rc.allocation(version.capital, mine.allocation_kind, mine.allocation_value)
            if mine
            else Decimal("0")
        )
        out.append(
            {
                "date": day["date"],
                "opening": float(Decimal(str(row.opening_capital)) * share / total)
                if total
                else 0.0,
                "transfer": 0.0,
                "pnl": day["pnl"],
                "charges": day["charges"],
                "net": day["net"],
                "trades": day["trades"],
            }
        )
    return out


def _series(key: str, name: str, kind: str, daily: list[dict], basis: float) -> dict:
    from services.strategy_performance_service import daily_summary

    total = 0.0
    for day in daily:
        total += day["net"]
        day["cumulative"] = total
    return {
        "key": key,
        "name": name,
        "kind": kind,
        "daily": daily,
        "summary": daily_summary(daily, basis),
    }


def pnl(owner: str, *, runs=(), members=(), main=False, strategies=(), start=None, end=None):
    """Daily net P&L after charges for each thing asked for, and all of them added up.

    Args:
        runs: Run ids, each as a whole.
        members: ``(run id, seat)`` pairs: one strategy within its Run.
        main: the main Sandbox account: everything it traded as one book,
            strategies outside Runs and manual orders alike.
        strategies: keys of strategies outside Runs, as the Strategy Dashboard names them.
        start, end: inclusive YYYY-MM-DD bounds.
    """
    from portfolio.strategy_pnl import attribute, daily_results
    from services.research_hosted_service import sandbox_money
    from services.strategy_performance_service import performance, sandbox_trades

    series = []
    for run_id in runs:
        run = _run(int(run_id), owner)
        daily = _run_daily(run, None, start, end)
        basis = daily[0]["opening"] if daily else 0.0
        series.append(_series(f"run:{run.id}", run.name, "run", daily, basis))
    for run_id, seat in members:
        run = _run(int(run_id), owner)
        held = _seat_members(run, seat)  # a strategy since removed still has its days
        if not held:
            raise RunError("That strategy is not in this Run.")
        member = held[-1]
        if _versions(run.id)[-1].mode == "shared":
            daily = _shared_member_daily(run, seat, start, end)
        else:
            daily = _run_daily(run, seat, start, end)
        basis = daily[0]["opening"] if daily else 0.0
        series.append(
            _series(
                f"member:{run.id}:{seat}", f"{run.name} / {member.label}", "member", daily, basis
            )
        )
    if main:
        # The account's own book: untagged orders are in it too, and positions
        # two strategies hold in one symbol net as the account holds them.
        trades = sandbox_trades(owner)
        capital, _leverage = sandbox_money()
        daily = [
            {**d, "opening": capital, "transfer": 0.0}
            for d in daily_results(attribute(trades, {t["strategy"] for t in trades}), _today())
            if _in_window(d["date"], start, end)
        ]
        series.append(_series("main", "Main Sandbox account", "main", daily, capital))
    if strategies:
        seen = performance(owner, start, end)
        wanted = set(strategies)
        for row in seen["strategies"]:
            if row["key"] in wanted:
                basis = row["summary"].get("start_capital") or seen["capital"]
                daily = [{**d, "opening": basis, "transfer": 0.0} for d in row["daily"]]
                series.append(_series(row["key"], row["name"], row["kind"], daily, basis))

    by_day: dict[str, dict] = {}
    for one in series:
        for day in one["daily"]:
            sum_ = by_day.setdefault(
                day["date"],
                {"date": day["date"], "opening": 0.0, "transfer": 0.0, "pnl": 0.0,
                 "charges": 0.0, "net": 0.0, "trades": 0},
            )  # fmt: skip
            for field in ("opening", "transfer", "pnl", "charges", "net", "trades"):
                sum_[field] += day.get(field) or 0
    combined_daily = [by_day[d] for d in sorted(by_day)]
    basis = sum(s["daily"][0]["opening"] for s in series if s["daily"])
    return {
        "series": series,
        "combined": _series("combined", "Combined", "combined", combined_daily, basis),
    }

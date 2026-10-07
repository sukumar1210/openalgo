"""
Research Sandbox: deployments, daily sessions, and the one door to the sandbox.

A Sandbox session trades a deployed scenario against live market data with
simulated fills. The session itself runs in its own worker process
(``research/sandbox_worker.py``); this module is the app side of it.

The execution boundary (PRD sections 15 and 16)
------------------------------------------------

Every order a session sends arrives here, through ``place_sandbox_order``, and
goes to ``sandbox.order_manager.OrderManager`` directly. Nothing here calls
``place_order_with_auth`` or reads the platform's analyzer switch, so flipping
OpenAlgo between analyze and live cannot turn a Sandbox session's order into a
broker order: there is no code path from here to a broker to switch onto. The
worker process, for its part, refuses to import any order code at all, the
sandbox included; this module is the only one that touches it.

Orders are placed here, in the app process, rather than by the worker, because
the sandbox serialises its position updates with a lock that only exists inside
one process (``ExecutionEngine._position_lock``). A second process placing
sandbox orders would race the app's own on the same position rows.

Each order is tagged with its platform order id in the sandbox's ``strategy``
column. That is what makes placement idempotent (a retried order id returns the
existing sandbox order instead of creating another) and what lets a restarted
session find out whether an order it decided was ever placed.

The sandbox's MARKET fill happens at placement, from a live quote. If no quote
comes back the order stays open, and the engine that would fill it later only
runs while analyzer mode is on, so the worker cancels an order that is still
open after a short wait and records it as unfilled rather than waiting on a
fill that may never come.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime

import pytz
from sqlalchemy.exc import IntegrityError

from database import research_db as rdb
from utils.keyed_locks import KeyedLocks
from utils.logging import get_logger

logger = get_logger(__name__)

#: Sandbox actions for a signed quantity.
BUY, SELL = "BUY", "SELL"

_order_locks = KeyedLocks(name="research-sandbox-orders")


class SandboxRefused(ValueError):
    """The request is refused. ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Execution: the endpoint behind research/sandbox_worker.py
# --------------------------------------------------------------------------- #


def _authorise(api_key: str, run_id: int):
    """The session and its user, or SandboxRefused.

    The key must belong to the user who deployed the session, and the session
    must be a live Sandbox session. A finished session cannot place orders.
    """
    from database.auth_db import verify_api_key

    user_id = verify_api_key(api_key) if api_key else None
    if not user_id:
        raise SandboxRefused("The API key is not valid.", 403)
    session = rdb.db_session
    run = session.get(rdb.ResearchRun, run_id)
    if run is None or run.environment != rdb.SANDBOX or run.deployment_id is None:
        raise SandboxRefused("That is not a Sandbox session.", 404)
    deployment = session.get(rdb.ResearchDeployment, run.deployment_id)
    if deployment is None or deployment.user_id != user_id:
        raise SandboxRefused("That Sandbox session belongs to another user.", 403)
    return run, user_id


def _status(order) -> dict:
    return {
        "orderid": order.orderid,
        "platform_order_id": order.strategy,
        "order_status": order.order_status,
        "filled_quantity": int(order.filled_quantity or 0),
        "average_price": float(order.average_price) if order.average_price else 0.0,
        "rejection_reason": order.rejection_reason,
    }


def _find(user_id: str, platform_order_id: str):
    from database.sandbox_db import SandboxOrders

    # populate_existing: the sandbox's own writes (a fill, a cancel) can land
    # through a path that leaves this session's copy of the row unexpired, and
    # a status read that answers from that copy reports an order as still open.
    return (
        SandboxOrders.query.filter_by(user_id=user_id, strategy=platform_order_id)
        .order_by(SandboxOrders.id)
        .populate_existing()
        .first()
    )


def place_sandbox_order(api_key: str, run_id: int, order: dict) -> dict:
    """Place one MARKET order in the sandbox for a session, at most once.

    Args:
        api_key: The deploying user's API key.
        run_id: The Sandbox session.
        order: ``platform_order_id``, ``symbol``, ``exchange``, ``quantity``
            (signed), ``product``.

    Returns:
        The sandbox order's status, ``created`` False when it already existed.
    """
    run, user_id = _authorise(api_key, run_id)
    if run.status not in (rdb.STARTING, rdb.RUNNING, rdb.PAUSED, rdb.STOPPING):
        raise SandboxRefused("This Sandbox session is not running, so it cannot place orders.", 409)
    tag = str(order.get("platform_order_id") or "")
    if not tag.startswith(f"research:{run_id}:"):
        raise SandboxRefused("The order does not belong to this session.")
    try:
        quantity = int(order.get("quantity"))
    except (TypeError, ValueError):
        raise SandboxRefused("The order quantity is not valid.") from None
    if quantity == 0:
        raise SandboxRefused("The order quantity is not valid.")
    product = str(order.get("product") or "MIS").upper()
    if product not in ("MIS", "CNC"):
        raise SandboxRefused("The order product is not valid.")

    from sandbox.order_manager import OrderManager

    with _order_locks.try_hold(tag, timeout=10) as held:
        if not held:
            raise SandboxRefused("The order is already being placed. Try again shortly.", 409)
        existing = _find(user_id, tag)
        if existing is not None:
            return {**_status(existing), "created": False}
        ok, response, _code = OrderManager(user_id).place_order(
            {
                "symbol": str(order.get("symbol") or "").upper(),
                "exchange": str(order.get("exchange") or "").upper(),
                "action": BUY if quantity > 0 else SELL,
                "quantity": abs(quantity),
                "price": 0,
                "trigger_price": 0,
                "price_type": "MARKET",
                "product": product,
                "strategy": tag,
            }
        )
        placed = _find(user_id, tag)
    if placed is not None:
        return {**_status(placed), "created": True}
    # Refused before a row was written (margin, a closed market, an unknown
    # symbol). The sandbox's own message is already a sentence.
    return {
        "orderid": None,
        "platform_order_id": tag,
        "order_status": "rejected",
        "filled_quantity": 0,
        "average_price": 0.0,
        "rejection_reason": (response or {}).get("message") if not ok else None,
        "created": False,
    }


def sandbox_order_status(api_key: str, run_id: int, platform_order_id: str) -> dict | None:
    """The sandbox's view of a session's order, found by its tag; None if never placed."""
    _run, user_id = _authorise(api_key, run_id)
    order = _find(user_id, platform_order_id)
    return None if order is None else _status(order)


def cancel_sandbox_order(api_key: str, run_id: int, platform_order_id: str) -> dict | None:
    """Cancel a session's order if it is still open. Returns its status afterwards.

    The sandbox can refuse a cancel (its margin release arithmetic does, for
    some reducing orders) and keeps the order open. The status returned is
    always the sandbox's own, so the caller sees an order that is still open
    and keeps tracking it, rather than assuming it is gone: a later fill of a
    forgotten order would be a position with nothing managing it.
    """
    from sandbox.order_manager import OrderManager

    _run, user_id = _authorise(api_key, run_id)
    order = _find(user_id, platform_order_id)
    if order is None:
        return None
    if order.order_status in ("open", "trigger pending"):
        ok, response, _code = OrderManager(user_id).cancel_order(order.orderid)
        if not ok:
            logger.warning(
                "Research Sandbox: could not cancel %s (%s): %s",
                platform_order_id,
                order.orderid,
                (response or {}).get("message"),
            )
        order = _find(user_id, platform_order_id)
    return _status(order)


# --------------------------------------------------------------------------- #
# Deployments and sessions
#
# Modelled on the Python Strategy Host's schedule: a start and a stop time in
# IST on chosen days. The start job opens the day's session when the exchange
# trades that day (the same calendar check the host makes); the stop job asks
# the session to close its positions and finish. Starting outside the schedule
# ("Start now") opens today's session immediately.
# --------------------------------------------------------------------------- #


IST = pytz.timezone("Asia/Kolkata")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
STALE_POLICIES = ("PAUSE", "FAIL_SAFE")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
SANDBOX_INTERVALS = ("1m", "5m", "15m", "30m", "1h")


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def check_deployable(scenario) -> None:
    """Refuse, with a reason, a scenario a Sandbox session cannot run."""
    session = rdb.db_session
    spec = scenario.dataset_spec or {}
    if spec.get("interval") not in SANDBOX_INTERVALS:
        raise SandboxRefused(
            "Sandbox sessions trade intraday bars (1 minute to 1 hour). This scenario uses "
            f"{spec.get('interval')} bars."
        )
    for inst in session.query(rdb.ResearchScenarioInstance).filter_by(scenario_id=scenario.id):
        config = session.get(rdb.ResearchConfigVersion, inst.config_version_id)
        if config.config.get("product") != "MIS":
            raise SandboxRefused(
                "Sandbox sessions are intraday and close every position each day, so every "
                "strategy in the scenario needs an MIS configuration."
            )


def create_deployment(user_id: str, data: dict) -> dict:
    session = rdb.db_session
    try:
        scenario_id = int(data.get("scenario_id"))
    except (TypeError, ValueError):
        raise SandboxRefused("Choose a scenario to deploy.") from None
    scenario = session.get(rdb.ResearchScenario, scenario_id)
    if scenario is None:
        raise SandboxRefused("That scenario does not exist.", 404)
    check_deployable(scenario)
    start = str(data.get("schedule_start") or "09:10")
    stop = str(data.get("schedule_stop") or "15:25")
    if not (_TIME.match(start) and _TIME.match(stop)):
        raise SandboxRefused("Start and stop times must look like 09:10.")
    if _minutes(stop) <= _minutes(start):
        raise SandboxRefused("The stop time must be after the start time.")
    days = [
        str(d).lower() for d in (data.get("schedule_days") or ["mon", "tue", "wed", "thu", "fri"])
    ]
    if not days or set(days) - set(DAYS):
        raise SandboxRefused("Choose at least one day, as mon to sun.")
    policy = str(data.get("stale_policy") or "PAUSE").upper()
    if policy not in STALE_POLICIES:
        raise SandboxRefused(
            "When market data stops, a session either pauses or closes its positions."
        )
    name = (data.get("name") or scenario.name or "").strip()[:200] or f"Scenario {scenario.id}"
    row = rdb.ResearchDeployment(
        scenario_id=scenario.id,
        name=name,
        user_id=user_id,
        status="ACTIVE",
        schedule_start=start,
        schedule_stop=stop,
        schedule_days=days,
        stale_policy=policy,
    )
    session.add(row)
    session.commit()
    _schedule(row)
    return rdb.deployment_to_dict(row)


def _session_dict(run) -> dict:
    out = rdb.run_to_dict(run)
    out.update({"live_state": run.live_state, "provenance": run.provenance})
    return out


def list_deployments(user_id: str) -> list[dict]:
    session = rdb.db_session
    out = []
    for row in (
        session.query(rdb.ResearchDeployment)
        .filter_by(user_id=user_id)
        .order_by(rdb.ResearchDeployment.id.desc())
    ):
        latest = (
            session.query(rdb.ResearchRun)
            .filter_by(deployment_id=row.id)
            .order_by(rdb.ResearchRun.id.desc())
            .first()
        )
        out.append(
            {
                **rdb.deployment_to_dict(row),
                "latest_session": rdb.run_to_dict(latest) if latest else None,
            }
        )
    return out


def _owned_deployment(user_id: str, deployment_id: int):
    row = rdb.db_session.get(rdb.ResearchDeployment, deployment_id)
    if row is None or row.user_id != user_id:
        raise SandboxRefused("That deployment does not exist.", 404)
    return row


def get_deployment(user_id: str, deployment_id: int) -> dict:
    from services.research_service import _instance_dicts

    session = rdb.db_session
    row = _owned_deployment(user_id, deployment_id)
    scenario = session.get(rdb.ResearchScenario, row.scenario_id)
    sessions = (
        session.query(rdb.ResearchRun)
        .filter_by(deployment_id=row.id)
        .order_by(rdb.ResearchRun.id.desc())
        .all()
    )
    completed = [s for s in sessions if s.status == rdb.COMPLETED and s.metrics]
    return {
        **rdb.deployment_to_dict(row),
        "scenario": {
            **rdb.scenario_to_dict(scenario),
            "instances": _instance_dicts(session, scenario.id),
        },
        "sessions": [rdb.run_to_dict(s) for s in sessions],
        "totals": {
            "sessions": len(completed),
            "net_pnl": sum(s.metrics.get("net_pnl", 0.0) for s in completed),
            "trades": sum(s.metrics.get("trades", 0) for s in completed),
            "costs": sum(s.metrics.get("costs", 0.0) for s in completed),
        },
    }


def _active_session(deployment_id: int, trading_date: str | None = None):
    query = rdb.db_session.query(rdb.ResearchRun).filter(
        rdb.ResearchRun.deployment_id == deployment_id,
        rdb.ResearchRun.status.in_(rdb.ACTIVE_STATUSES),
    )
    if trading_date:
        query = query.filter(rdb.ResearchRun.trading_date == trading_date)
    return query.order_by(rdb.ResearchRun.id.desc()).first()


def stop_deployment(user_id: str, deployment_id: int) -> dict:
    """Stop scheduling sessions and ask today's session to close its positions."""
    row = _owned_deployment(user_id, deployment_id)
    session = rdb.db_session
    row.status = "STOPPED"
    row.stopped_at = rdb.utc_now()
    session.commit()
    _unschedule(row.id)
    active = _active_session(row.id)
    if active is not None:
        _request_stop(active.id)
    return rdb.deployment_to_dict(row)


def activate_deployment(user_id: str, deployment_id: int) -> dict:
    row = _owned_deployment(user_id, deployment_id)
    check_deployable(rdb.db_session.get(rdb.ResearchScenario, row.scenario_id))
    row.status = "ACTIVE"
    row.stopped_at = None
    rdb.db_session.commit()
    _schedule(row)
    return rdb.deployment_to_dict(row)


def _trading_today(exchange: str) -> bool:
    """Whether the exchange has a session today, from the market calendar."""
    from database.market_calendar_db import get_effective_session_window

    try:
        return bool(get_effective_session_window(datetime.now(IST).date(), exchange))
    except Exception:
        logger.exception("Research Sandbox: market calendar unavailable; assuming a trading day")
        return True


def open_session(deployment_id: int, *, scheduled: bool) -> dict | None:
    """Open today's session for a deployment and start its worker.

    One session per deployment per day, so a second call returns the session
    already open (and restarts its worker if none is running). Returns None when
    the schedule says not today.
    """
    session = rdb.db_session
    row = session.get(rdb.ResearchDeployment, deployment_id)
    if row is None or row.status != "ACTIVE":
        return None
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    scenario = session.get(rdb.ResearchScenario, row.scenario_id)
    if scheduled and DAYS[now.weekday()] not in row.schedule_days:
        return None
    if not _trading_today(scenario.dataset_spec["exchange"]):
        if scheduled:
            logger.info(
                "Research Sandbox: %s is closed today; deployment %s skipped",
                scenario.dataset_spec["exchange"],
                row.id,
            )
            return None
        raise SandboxRefused(f"{scenario.dataset_spec['exchange']} is closed today.")
    if not scheduled and now.hour * 60 + now.minute >= _minutes(row.schedule_stop):
        raise SandboxRefused("Today's session window has already ended for this deployment.")
    run = (
        session.query(rdb.ResearchRun)
        .filter_by(deployment_id=row.id, trading_date=today)
        .order_by(rdb.ResearchRun.id.desc())
        .first()
    )
    if run is None:
        run = rdb.ResearchRun(
            scenario_id=row.scenario_id,
            status=rdb.QUEUED,
            environment=rdb.SANDBOX,
            deployment_id=row.id,
            trading_date=today,
            message="Waiting to start",
        )
        session.add(run)
        try:
            session.commit()
        except IntegrityError:
            # A scheduler fire and a click raced; the other one opened it.
            session.rollback()
            run = (
                session.query(rdb.ResearchRun)
                .filter_by(deployment_id=row.id, trading_date=today)
                .one()
            )
    if run.status in rdb.ACTIVE_STATUSES:
        _start_worker(row, run)
    return _session_dict(run)


def _start_worker(deployment, run) -> None:
    from database.auth_db import get_api_key_for_tradingview
    from services.research_service import spawn_sandbox_session

    api_key = get_api_key_for_tradingview(deployment.user_id)
    if not api_key:
        _finish(
            run.id,
            rdb.FAILED,
            "Create an API key on the API Key page first. Sandbox sessions use it to reach market data.",
        )
        return
    spawn_sandbox_session(
        run.id,
        {
            "OPENALGO_API_KEY": api_key,
            "OPENALGO_HOST": os.getenv("HOST_SERVER", "http://127.0.0.1:5000"),
        },
    )


def _finish(run_id: int, status: str, message: str) -> None:
    session = rdb.db_session
    session.query(rdb.ResearchRun).filter(
        rdb.ResearchRun.id == run_id, rdb.ResearchRun.status.in_(rdb.ACTIVE_STATUSES)
    ).update(
        {
            rdb.ResearchRun.status: status,
            rdb.ResearchRun.message: message,
            rdb.ResearchRun.finished_at: rdb.utc_now(),
        },
        synchronize_session=False,
    )
    session.commit()


def _request_stop(run_id: int) -> None:
    """Ask a session to close its positions and finish. One that never started just ends."""
    session = rdb.db_session
    session.query(rdb.ResearchRun).filter(
        rdb.ResearchRun.id == run_id,
        rdb.ResearchRun.status.in_((rdb.STARTING, rdb.RUNNING, rdb.PAUSED)),
    ).update({rdb.ResearchRun.status: rdb.STOPPING}, synchronize_session=False)
    session.query(rdb.ResearchRun).filter(
        rdb.ResearchRun.id == run_id,
        rdb.ResearchRun.status == rdb.QUEUED,
        rdb.ResearchRun.lease_owner.is_(None),
    ).update(
        {
            rdb.ResearchRun.status: rdb.CANCELLED,
            rdb.ResearchRun.message: "Stopped before it started",
            rdb.ResearchRun.finished_at: rdb.utc_now(),
        },
        synchronize_session=False,
    )
    session.commit()


def _owned_session(user_id: str, run_id: int):
    run = rdb.db_session.get(rdb.ResearchRun, run_id)
    if run is None or run.environment != rdb.SANDBOX:
        raise SandboxRefused("That Sandbox session does not exist.", 404)
    _owned_deployment(user_id, run.deployment_id)
    return run


def get_session(user_id: str, run_id: int) -> dict:
    run = _owned_session(user_id, run_id)
    events = (
        rdb.db_session.query(rdb.LedgerEvent)
        .filter(rdb.LedgerEvent.run_id == run_id, rdb.LedgerEvent.kind != "MARK")
        .order_by(rdb.LedgerEvent.seq.desc())
        .limit(100)
        .all()
    )
    return {
        **_session_dict(run),
        "events": [
            {
                "seq": e.seq, "kind": e.kind, "at": e.at.isoformat() + "Z", "bar_ts": e.bar_ts,
                "instance": e.instance, "platform_order_id": e.platform_order_id, "payload": e.payload,
            }
            for e in events
        ],
    }  # fmt: skip


def control_session(user_id: str, run_id: int, action: str) -> dict:
    """Pause, resume or stop a session. Each is a compare-and-set on its status."""
    run = _owned_session(user_id, run_id)
    session = rdb.db_session
    transitions = {"pause": (rdb.RUNNING, rdb.PAUSED), "resume": (rdb.PAUSED, rdb.RUNNING)}
    if action == "stop":
        _request_stop(run.id)
    elif action in transitions:
        before, after = transitions[action]
        changed = (
            session.query(rdb.ResearchRun)
            .filter_by(id=run.id, status=before)
            .update({rdb.ResearchRun.status: after}, synchronize_session=False)
        )
        session.commit()
        if not changed:
            raise SandboxRefused(
                f"The session is not {before.lower()}, so it cannot be {action}d.", 409
            )
    else:
        raise SandboxRefused("Unknown action.")
    session.refresh(run)
    return _session_dict(run)


def start_now(user_id: str, deployment_id: int) -> dict:
    """Open today's session now, outside the schedule."""
    _owned_deployment(user_id, deployment_id)
    result = open_session(deployment_id, scheduled=False)
    if result is None:
        raise SandboxRefused("Activate the deployment before starting a session.")
    return result


def scheduled_start(deployment_id: int) -> None:
    try:
        open_session(deployment_id, scheduled=True)
    except Exception:
        logger.exception("Research Sandbox: scheduled start of deployment %s failed", deployment_id)


def scheduled_stop(deployment_id: int) -> None:
    run = _active_session(deployment_id, _today())
    if run is not None:
        _request_stop(run.id)


def recover_sessions() -> None:
    """At boot: resume today's interrupted sessions, settle older ones.

    A session from an earlier day cannot be resumed: its trading day is over.
    Its positions, if any, are still in the sandbox, so the message says where
    to look instead of claiming they were closed.
    """
    session = rdb.db_session
    today = _today()
    for run in (
        session.query(rdb.ResearchRun)
        .filter(
            rdb.ResearchRun.environment == rdb.SANDBOX,
            rdb.ResearchRun.status.in_(rdb.ACTIVE_STATUSES),
        )
        .all()
    ):
        if run.trading_date != today:
            _finish(
                run.id,
                rdb.FAILED,
                "The session was interrupted and its trading day has ended. Check the sandbox "
                "positions for its symbols.",
            )
            continue
        deployment = session.get(rdb.ResearchDeployment, run.deployment_id)
        logger.info("Research Sandbox: resuming session %s after a restart", run.id)
        _start_worker(deployment, run)


# --------------------------------------------------------------------------- #
# Scheduler: the schedule is a projection of research_deployments, rebuilt on
# every boot, the strategy module's pattern (services/strategy_module/scheduler).
# --------------------------------------------------------------------------- #


_scheduler = None
_scheduler_lock = threading.Lock()


def _job_ids(deployment_id: int) -> tuple[str, str]:
    return f"research_sandbox_start_{deployment_id}", f"research_sandbox_stop_{deployment_id}"


def _schedule(row) -> None:
    if _scheduler is None:
        return
    from apscheduler.triggers.cron import CronTrigger

    from utils.db_sessions import releases_scoped_sessions

    start_id, stop_id = _job_ids(row.id)
    days = ",".join(row.schedule_days)
    for job_id, hhmm, fn in (
        (start_id, row.schedule_start, scheduled_start),
        (stop_id, row.schedule_stop, scheduled_stop),
    ):
        hour, minute = (int(x) for x in hhmm.split(":"))
        _scheduler.add_job(
            func=releases_scoped_sessions(fn),
            args=(row.id,),
            trigger=CronTrigger(hour=hour, minute=minute, day_of_week=days, timezone=IST),
            id=job_id,
            replace_existing=True,
        )


def _unschedule(deployment_id: int) -> None:
    if _scheduler is None:
        return
    for job_id in _job_ids(deployment_id):
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)


def start_research_sandbox() -> None:
    """Boot: start the schedule, then resume whatever a restart interrupted."""
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    from utils.shutdown import register_shutdown_hook

    with _scheduler_lock:
        if _scheduler is not None:
            return
        _scheduler = BackgroundScheduler(
            timezone=IST,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        )
        _scheduler.start()
    register_shutdown_hook(
        lambda: _scheduler.shutdown(wait=False), name="research-sandbox-scheduler", budget_s=5.0
    )
    session = rdb.db_session
    for row in session.query(rdb.ResearchDeployment).filter_by(status="ACTIVE"):
        _schedule(row)
    recover_sessions()
    logger.info("Research Sandbox scheduler started")

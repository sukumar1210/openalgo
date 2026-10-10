"""
Research Live: the only way a research session places a real broker order.

Master PRD Phase 9 (sections 25, 31, 46, 56). A LIVE session is the same
session, engine, books and ledger as a Sandbox one; its execution adapter calls
``/api/v1/researchlive/*``, served here, in the app process. Orders reach the
broker through ``services.strategy_module.order_dispatch``, OpenAlgo's existing
live pipe for strategies: broker authorisation resolved fresh for every order,
``force_live=True`` so the platform's analyzer switch can neither divert a live
session's exit into the sandbox nor turn anything else live, and the Action
Center bypassed because an exit that waits for a click is not an exit. Nothing
here is a second broker abstraction.

What Live needs, every time
---------------------------

To deploy (``check_live_deployable``): the server allows Live
(``RESEARCH_LIVE_ENABLED``, off by default like ``MCP_HTTP_ENABLED``, so a
fresh install cannot trade live from Research at all); every strategy version
in the scenario approved for live by the operator (per version, as the strategy
module's ``live_enabled`` is per strategy, because a research version never
changes); the scenario has completed a Sandbox session; a capital limit the
scenario fits within; and a typed confirmation (checked by the deployment
service).

For every order that adds to a position: the server still allows Live, the kill
switch is off, every version is still approved, and the session's gross
exposure after the order stays within its deployment's capital limit. An order
that only reduces a position is always sent: refusing an exit would leave the
position open, which is the one outcome every rule here exists to prevent.

Fills
-----

Fills arrive the way the strategy module hears them
(``services/strategy_module/order_events.py``): one subscription to the
``OrderUpdateEvent`` that ``services/order_update_service.py`` publishes, so
nothing polls the broker in a loop. A session asking for an order's status
reads the door's own row. Only a row that has heard nothing for
``RECONCILE_SECONDS`` is checked with the broker, and then no more often than
that: targeted reconciliation, the safety net for a broker with no
order-update stream, on the strategy module's five-second cadence.

Order identity
--------------

Every order is written to ``research_live_orders`` as PLACING, under a lock on
its platform order id, before the broker is called, and completed after. A
retried request finds the row and returns it, so an order is sent at most once.
A row still PLACING long after its request is one a crash interrupted: its
outcome is reported as unknown, and the session stops trading that symbol
rather than guess (``research.sandbox_worker``).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from database import research_db as rdb
from utils.db_sessions import releases_scoped_sessions
from utils.keyed_locks import KeyedLocks
from utils.logging import get_logger

logger = get_logger(__name__)

_order_locks = KeyedLocks(name="research-live-orders")

KILL_SWITCH = "live_kill"
#: A request still PLACING after this long was cut off by a crash.
PLACING_TIMEOUT_SECONDS = 120
#: An open order that has heard no update for this long is checked with the
#: broker, and then no more often than this.
RECONCILE_SECONDS = 5
# Broker vocabularies differ; these match the strategy module's.
_FILLED = frozenset({"complete", "completed", "filled", "executed", "traded"})
_DEAD = frozenset({"rejected", "cancelled", "canceled"})
_CANCELLED = frozenset({"cancelled", "canceled"})
#: research_live_orders status -> the status a session reads.
SESSION_STATUS = {
    "PLACING": "open",
    "OPEN": "open",
    "COMPLETE": "complete",
    "REJECTED": "rejected",
    "CANCELLED": "cancelled",
    "UNKNOWN": "unknown",
}
FINAL = ("COMPLETE", "REJECTED", "CANCELLED", "UNKNOWN")


class LiveRefused(ValueError):
    """Live trading refused this, for the reason in the message (written for the trader)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def live_enabled() -> bool:
    """Whether this server lets Research trade live at all. Off unless opted in."""
    return os.getenv("RESEARCH_LIVE_ENABLED", "False").lower() == "true"


OFF_MESSAGE = (
    "Live trading from Research is off on this server. To allow it, set "
    "RESEARCH_LIVE_ENABLED = 'True' in .env, then restart OpenAlgo."
)


# --------------------------------------------------------------------------- #
# Kill switch and approvals: operator decisions, stored, never inferred
# --------------------------------------------------------------------------- #


def kill_switch() -> dict:
    row = rdb.db_session.get(rdb.ResearchControl, KILL_SWITCH)
    value = dict(row.value or {}) if row else {}
    return {
        "engaged": bool(value.get("engaged")),
        "reason": value.get("reason"),
        "by": row.updated_by if row else None,
        "at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


def set_kill_switch(user_id: str, engaged: bool, reason: str | None = None) -> dict:
    """Engage or release the kill switch. Engaging stops every live session now.

    Each session also reads the switch on every pass of its loop, so it stops
    even if this request to stop it were lost. While engaged, the Live door sends
    only orders that reduce a position, and no live session opens.
    """
    from services.research_sandbox_service import _request_stop

    session = rdb.db_session
    row = session.get(rdb.ResearchControl, KILL_SWITCH)
    if row is None:
        row = rdb.ResearchControl(name=KILL_SWITCH)
        session.add(row)
    row.value = {"engaged": bool(engaged), "reason": (reason or "").strip()[:500] or None}
    row.updated_by = user_id
    row.updated_at = rdb.utc_now()
    session.commit()
    if engaged:
        for run in session.query(rdb.ResearchRun).filter(
            rdb.ResearchRun.environment == rdb.LIVE,
            rdb.ResearchRun.status.in_(rdb.ACTIVE_STATUSES),
        ):
            _request_stop(run.id)
        logger.warning("Research Live kill switch engaged by %s", user_id)
    else:
        logger.info("Research Live kill switch released by %s", user_id)
    from services.research_sandbox_service import alert

    alert(
        user_id,
        "Live kill switch on: every live research session is closing its positions."
        if engaged
        else "Live kill switch off: live research sessions may open again.",
    )
    return kill_switch()


def approved_versions(version_ids) -> set[int]:
    ids = set(version_ids)
    if not ids:
        return set()
    return {
        row.strategy_version_id
        for row in rdb.db_session.query(rdb.ResearchLiveApproval).filter(
            rdb.ResearchLiveApproval.strategy_version_id.in_(ids),
            rdb.ResearchLiveApproval.revoked_at.is_(None),
        )
    }


def set_live_approval(user_id: str, version_id: int, approved: bool) -> dict:
    """Allow, or stop allowing, one strategy version to trade live."""
    session = rdb.db_session
    if session.get(rdb.ResearchStrategyVersion, version_id) is None:
        raise LiveRefused("That strategy version does not exist.", 404)
    row = session.get(rdb.ResearchLiveApproval, version_id)
    if approved:
        if row is None:
            row = rdb.ResearchLiveApproval(strategy_version_id=version_id, approved_by=user_id)
            session.add(row)
        row.approved_by = user_id
        row.approved_at = rdb.utc_now()
        row.revoked_at = None
    elif row is not None and row.revoked_at is None:
        row.revoked_at = rdb.utc_now()
    session.commit()
    logger.warning(
        "Research Live: strategy version %s %s by %s",
        version_id, "allowed to trade live" if approved else "no longer allowed to trade live",
        user_id,
    )  # fmt: skip
    return {"strategy_version_id": version_id, "live_approved": bool(approved)}


def _scenario_versions(scenario_id: int) -> list[int]:
    return [
        inst.strategy_version_id
        for inst in rdb.db_session.query(rdb.ResearchScenarioInstance).filter_by(
            scenario_id=scenario_id
        )
    ]


def _unapproved(scenario_id: int) -> list[str]:
    versions = _scenario_versions(scenario_id)
    allowed = approved_versions(versions)
    names = []
    for version_id in versions:
        if version_id not in allowed:
            version = rdb.db_session.get(rdb.ResearchStrategyVersion, version_id)
            strategy = rdb.db_session.get(rdb.ResearchStrategy, version.strategy_id)
            names.append(f"{strategy.key} {version.dist_version or version.version_hash[:10]}")
    return names


def _buying_power(scenario) -> float:
    """The most a scenario's strategies can hold at once.

    Isolated: every strategy's own sized capital. Shared: the same, capped by
    the portfolio's exposure limit (shared capital times its leverage limit),
    which the engine enforces whatever the strategies ask for.
    """
    from research.strategy import Config

    configs = [
        Config.from_dict(rdb.db_session.get(rdb.ResearchConfigVersion, i.config_version_id).config)
        for i in rdb.db_session.query(rdb.ResearchScenarioInstance).filter_by(
            scenario_id=scenario.id
        )
    ]
    asked = sum(c.sizing_capital for c in configs)
    if scenario.mode != "SHARED":
        return asked
    capital = scenario.shared_capital or sum(c.capital for c in configs)
    return min(asked, capital * float(scenario.risk_policy.get("max_gross_leverage", 1.0)))


def allotted() -> float:
    """The capital limits of every active live deployment, added up."""
    query = rdb.db_session.query(rdb.ResearchDeployment).filter(
        rdb.ResearchDeployment.environment == rdb.LIVE,
        rdb.ResearchDeployment.status == "ACTIVE",
    )
    return sum(row.capital_limit or 0.0 for row in query)


def check_live_deployable(scenario, capital_limit) -> float:
    """Refuse, naming what is missing, a scenario that may not trade live. Returns the limit."""
    if not live_enabled():
        raise LiveRefused(OFF_MESSAGE)
    missing = _unapproved(scenario.id)
    if missing:
        raise LiveRefused(
            "Allow these strategy versions to trade live on their strategy pages first: "
            + ", ".join(missing)
            + "."
        )
    sandboxed = (
        rdb.db_session.query(rdb.ResearchRun.id)
        .filter_by(scenario_id=scenario.id, environment=rdb.SANDBOX, status=rdb.COMPLETED)
        .first()
    )
    if sandboxed is None:
        raise LiveRefused(
            "Run this scenario in the Sandbox for at least one full trading day before it "
            "trades live."
        )
    try:
        limit = float(capital_limit)
    except (TypeError, ValueError):
        raise LiveRefused("Set the most capital this deployment may use, in rupees.") from None
    if not limit > 0:
        raise LiveRefused("Set the most capital this deployment may use, in rupees.")
    needed = _buying_power(scenario)
    if needed > limit + 1e-6:
        raise LiveRefused(
            f"This scenario's strategies can hold up to Rs {needed:,.0f}, more than the "
            f"Rs {limit:,.0f} limit. Raise the limit or deploy a scenario with less capital."
        )
    return limit


# --------------------------------------------------------------------------- #
# The door: /api/v1/researchlive, called by research/sandbox_worker.py
# --------------------------------------------------------------------------- #


def _authorise(api_key: str, run_id: int):
    """The live session, its deployment and its user, or LiveRefused."""
    from database.auth_db import verify_api_key

    user_id = verify_api_key(api_key) if api_key else None
    if not user_id:
        raise LiveRefused("The API key is not valid.", 403)
    session = rdb.db_session
    run = session.get(rdb.ResearchRun, run_id)
    if run is None or run.environment != rdb.LIVE or run.deployment_id is None:
        raise LiveRefused("That is not a live session.", 404)
    deployment = session.get(rdb.ResearchDeployment, run.deployment_id)
    if deployment is None or deployment.user_id != user_id or deployment.environment != rdb.LIVE:
        raise LiveRefused("That live session belongs to another user.", 403)
    return run, deployment, user_id


def _status(row) -> dict:
    return {
        "orderid": row.broker_order_id,
        "platform_order_id": row.platform_order_id,
        "order_status": SESSION_STATUS[row.status],
        "filled_quantity": int(row.filled_quantity or 0),
        "average_price": float(row.average_price or 0.0),
        "rejection_reason": row.message,
    }


def _rows(run_id: int):
    return rdb.db_session.query(rdb.LiveOrder).filter_by(run_id=run_id)


def _position(run_id: int, symbol: str) -> int:
    """The session's filled position on a symbol, from the door's own record."""
    total = 0
    for row in _rows(run_id).filter_by(symbol=symbol):
        if row.filled_quantity:
            total += row.filled_quantity if row.quantity > 0 else -row.filled_quantity
    return total


def _gross_after(run_id: int, symbol: str, quantity: int, price: float) -> float:
    """The session's gross exposure if this order filled, at the latest prices it knows."""
    held: dict[str, int] = {}
    prices: dict[str, float] = {symbol: price}
    for row in _rows(run_id).order_by(rdb.LiveOrder.created_at):
        if row.filled_quantity:
            signed = row.filled_quantity if row.quantity > 0 else -row.filled_quantity
            held[row.symbol] = held.get(row.symbol, 0) + signed
            if row.symbol != symbol and row.average_price:
                prices[row.symbol] = row.average_price
    held[symbol] = held.get(symbol, 0) + quantity
    return sum(abs(q) * prices.get(s, 0.0) for s, q in held.items())


def place_live_order(api_key: str, run_id: int, order: dict) -> dict:
    """Place one MARKET order with the broker for a live session, at most once.

    Args:
        order: ``platform_order_id``, ``symbol``, ``exchange``, ``quantity``
            (signed), ``product`` and ``reference_price`` (the decision price).

    Returns:
        The order's status as the session reads it; ``created`` False when it
        had already been placed.
    """
    from services.strategy_module.order_dispatch import build_order, dispatch_order

    run, deployment, _user = _authorise(api_key, run_id)
    if run.status not in (rdb.STARTING, rdb.RUNNING, rdb.PAUSED, rdb.STOPPING):
        raise LiveRefused("This live session is not running, so it cannot place orders.", 409)
    tag = str(order.get("platform_order_id") or "")
    if not tag.startswith(f"research:{run_id}:"):
        raise LiveRefused("The order does not belong to this session.")
    try:
        quantity = int(order["quantity"])
        symbol = str(order["symbol"]).upper()
        exchange = str(order["exchange"]).upper()
        price = float(order.get("reference_price") or 0.0)
    except (KeyError, TypeError, ValueError):
        raise LiveRefused("The order is incomplete.") from None
    product = str(order.get("product") or "MIS").upper()
    if quantity == 0 or product != "MIS":
        raise LiveRefused("A live research order is an intraday (MIS) order for some shares.")

    session = rdb.db_session
    with _order_locks.try_hold(tag, timeout=10) as held:
        if not held:
            raise LiveRefused("The order is already being placed. Try again shortly.", 409)
        existing = session.get(rdb.LiveOrder, (run_id, tag))
        if existing is not None:
            return {**_status(existing), "created": False}
        position = _position(run_id, symbol)
        # Only an order on the other side of the position, no larger than it,
        # purely reduces it. One that flips the position opens a new one.
        reducing = (
            position != 0 and (quantity > 0) != (position > 0) and abs(quantity) <= abs(position)
        )
        if not reducing:
            try:
                _check_adding(run, deployment, symbol, quantity, price)
            except LiveRefused as exc:
                from services.research_sandbox_service import _named, alert

                alert(
                    deployment.user_id,
                    f"Live order refused: {_named(deployment)}, "
                    f"{'buy' if quantity > 0 else 'sell'} {abs(quantity)} {symbol}. {exc}",
                )
                raise
        row = rdb.LiveOrder(
            run_id=run_id,
            platform_order_id=tag,
            symbol=symbol,
            exchange=exchange,
            quantity=quantity,
            product=product,
            reference_price=price,
            status="PLACING",
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return {**_status(session.get(rdb.LiveOrder, (run_id, tag))), "created": False}
        result = dispatch_order(
            mode="live",
            api_key=api_key,
            order=build_order(
                symbol=symbol,
                exchange=exchange,
                action="BUY" if quantity > 0 else "SELL",
                quantity=abs(quantity),
                product=product,
                strategy_name=f"research {run_id}",
            ),
        )
        row.broker_order_id = result.broker_order_id
        if result.ok and not result.broker_order_id:
            # Accepted with no id to follow it by: nothing can say how it ends.
            row.status = "UNKNOWN"
            row.message = "The broker accepted the order without an order id to follow it by."
        else:
            row.status = "OPEN" if result.ok else "REJECTED"
            row.message = None if result.ok else result.error
        row.updated_at = rdb.utc_now()
        session.commit()
        logger.info(
            "session=%s order=%s live %s %s %s: %s",
            run_id, tag, symbol, quantity, product, row.status,
        )  # fmt: skip
        return {**_status(row), "created": True}


def _check_adding(run, deployment, symbol: str, quantity: int, price: float) -> None:
    """Every rule an order that adds to a position must pass, at the moment it is sent."""
    if not live_enabled():
        raise LiveRefused(OFF_MESSAGE)
    if kill_switch()["engaged"]:
        raise LiveRefused("The kill switch is on: only orders that close positions are sent.", 409)
    missing = _unapproved(run.scenario_id)
    if missing:
        raise LiveRefused(
            "These strategy versions are no longer allowed to trade live: "
            + ", ".join(missing)
            + ".",
            409,
        )
    if price <= 0:
        raise LiveRefused("The order has no reference price, so its size cannot be checked.")
    limit = deployment.capital_limit or 0.0
    gross = _gross_after(run.id, symbol, quantity, price)
    if gross > limit + 1e-6:
        raise LiveRefused(
            f"This order would take the deployment to Rs {gross:,.0f} in positions, over its "
            f"Rs {limit:,.0f} limit."
        )


def live_order_status(api_key: str, run_id: int, platform_order_id: str) -> dict | None:
    """A live session's order as the broker now reports it, or None if never placed."""
    _authorise(api_key, run_id)
    row = rdb.db_session.get(rdb.LiveOrder, (run_id, platform_order_id))
    if row is None:
        return None
    _refresh(api_key, row)
    return _status(row)


def cancel_live_order(api_key: str, run_id: int, platform_order_id: str) -> dict | None:
    """Cancel a live session's order if the broker still has it open."""
    from services.strategy_module.order_dispatch import cancel_order

    _authorise(api_key, run_id)
    row = rdb.db_session.get(rdb.LiveOrder, (run_id, platform_order_id))
    if row is None:
        return None
    if row.status == "OPEN" and row.broker_order_id:
        result = cancel_order(mode="live", api_key=api_key, broker_order_id=row.broker_order_id)
        if not result.ok:
            logger.warning("session=%s order=%s cancel refused: %s", run_id, platform_order_id,
                           result.error)  # fmt: skip
    # The session acts on the cancel's outcome next, so read it now.
    _refresh(api_key, row, now=True)
    return _status(row)


def _normalise(status) -> str:
    return str(status or "").strip().lower().replace("_", " ")


def _price(value) -> float | None:
    """A usable fill price, or None: a broker that sends "0" has not said."""
    from services.risk.models import is_price

    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if is_price(price) else None


def _update(row, values: dict) -> bool:
    """Write ``values`` only if the row still reads as it did. Returns whether it did.

    Two writers can hold one order: updates run on the event bus's critical
    lane, several at once under the gthread worker, while a session's status
    read may be checking the broker. The status and filled quantity a writer
    read are its claim, as ``state.claim_leg_exit`` claims a leg, so a stale
    "open" can never land on top of the fill that came after it.
    """
    session = rdb.db_session
    changed = (
        session.query(rdb.LiveOrder)
        .filter_by(
            run_id=row.run_id,
            platform_order_id=row.platform_order_id,
            status=row.status,
            filled_quantity=row.filled_quantity,
        )
        .update({**values, "updated_at": rdb.utc_now()}, synchronize_session=False)
    )
    session.commit()
    return bool(changed)


def _fold(row, state: str, filled=None, average=None, reason=None) -> bool:
    """Fold one broker fact into a row. A final row never changes.

    ``filled`` is the broker's cumulative filled quantity, or None when it did
    not say; it only ever raises the row's. Anything neither filled nor dead is
    still working at the broker, where only a larger fill is news.
    """
    if row.status in FINAL:
        return False
    whole = abs(row.quantity)
    known = row.filled_quantity
    if filled is not None:
        known = max(known, min(abs(int(float(filled or 0))), whole))  # brokers send "40.0"
    price = _price(average)
    if state in _FILLED:
        if price is None:
            logger.warning(
                "Research Live order %s filled with no usable price; recording %s",
                row.broker_order_id, "the partial fills' average" if row.average_price else
                "the decision price",
            )  # fmt: skip
        values = {
            "status": "COMPLETE",
            "filled_quantity": whole,
            "average_price": price or row.average_price or row.reference_price,
        }
    elif state in _DEAD:
        values = {
            "status": "CANCELLED" if state in _CANCELLED else "REJECTED",
            "filled_quantity": known,
            "average_price": price or row.average_price,
            "message": row.message
            or reason
            or ("Cancelled at the broker" if state in _CANCELLED else "Refused by the broker"),
        }
    elif known > row.filled_quantity:
        values = {
            "status": "OPEN",
            "filled_quantity": known,
            "average_price": price or row.average_price,
        }
    else:
        return False
    return _update(row, values)


_lock = threading.Lock()
_started = False


def start() -> bool:
    """Subscribe to order updates. Idempotent; called once at boot.

    The critical lane, as the strategy module's: this is how a live session
    learns an order filled, and a fill it never hears about is a position
    nothing is managing.
    """
    from utils.event_bus import bus

    global _started
    with _lock:
        if _started:
            return False
        bus.subscribe(
            "order.update", _on_order_update, name="ResearchLiveOrderUpdates", critical=True
        )
        _started = True
        logger.info("Research Live subscribed to order updates")
        return True


@releases_scoped_sessions
def _on_order_update(event) -> None:
    """Apply a broker's order update to the live order it belongs to, if any.

    Cheap for every other surface's orders: one indexed lookup. A Sandbox
    update (mode "analyze") is never a Live session's.
    """
    order_id = str(getattr(event, "orderid", "") or "")
    if not order_id or getattr(event, "mode", "") != "live":
        return
    try:
        row = (
            rdb.db_session.query(rdb.LiveOrder)
            .filter(
                rdb.LiveOrder.broker_order_id == order_id,
                rdb.LiveOrder.status.notin_(FINAL),
            )
            .order_by(rdb.LiveOrder.created_at.desc())
            .first()
        )
        if row is not None:
            _fold(
                row,
                _normalise(event.order_status),
                event.filled_quantity,
                event.average_price,
                event.rejection_reason or None,
            )
    except Exception:
        logger.exception("Research Live could not apply an order update for %s", order_id)


def _refresh(api_key: str, row, now: bool = False) -> None:
    """Check a row with the broker when its updates have gone quiet, or ``now``.

    A row still PLACING long after its request was cut off by a crash: its
    outcome is unknown, and is said so rather than guessed.

    ponytail: an update that lands before the broker answers the placement has
    no row to match yet and waits for this check; replay it as the strategy
    module's ``order_events.replay_for`` does if five seconds ever matters.
    """
    from services.strategy_module.order_dispatch import fetch_order_status

    if row.status in FINAL:
        return
    at = rdb.utc_now()
    if row.status == "PLACING":
        if at - row.created_at > timedelta(seconds=PLACING_TIMEOUT_SECONDS):
            _update(
                row,
                {
                    "status": "UNKNOWN",
                    "message": "The request to the broker was cut off before its answer "
                    "was recorded.",
                },
            )
        return
    quiet = timedelta(seconds=RECONCILE_SECONDS)
    if not row.broker_order_id:
        return
    if not now and (
        at - row.updated_at < quiet or (row.checked_at and at - row.checked_at < quiet)
    ):
        return
    row.checked_at = at
    rdb.db_session.commit()
    result = fetch_order_status(mode="live", api_key=api_key, broker_order_id=row.broker_order_id)
    if not result.ok or not result.order:
        return
    state = _normalise(result.order.get("order_status"))
    filled, average = None, result.order.get("average_price")
    if state in _DEAD:
        # OpenAlgo's order status has no filled quantity; the tradebook does.
        # Unreadable, the order stays open here and is checked again: a part
        # fill guessed as none would leave a real position unmanaged.
        traded = _filled_from_trades(api_key, row.broker_order_id)
        if traded is None:
            return
        filled, average = traded
    _fold(row, state, filled, average, result.order.get("rejection_reason"))


def _filled_from_trades(api_key: str, broker_order_id: str) -> tuple[int, float] | None:
    """What part of a cancelled order filled before it ended, from the tradebook.

    None when the tradebook could not be read, which is not the same as nothing.
    """
    from services.strategy_module.order_dispatch import resolve_live_auth
    from services.tradebook_service import get_tradebook

    auth_token, broker, error = resolve_live_auth(api_key)
    if error:
        return None
    try:
        ok, response, _status_code = get_tradebook(auth_token=auth_token, broker=broker)
    except Exception:
        logger.exception("Research Live: reading the tradebook failed")
        return None
    if not ok:
        return None
    quantity, value = 0, 0.0
    for trade in (response or {}).get("data") or []:
        if str(trade.get("orderid")) != str(broker_order_id):
            continue
        qty = abs(int(float(trade.get("quantity") or 0)))
        quantity += qty
        value += qty * float(trade.get("average_price") or 0.0)
    return quantity, (value / quantity if quantity else 0.0)


# --------------------------------------------------------------------------- #
# Reconciliation and the overview
# --------------------------------------------------------------------------- #


def _today_live_runs(user_id: str):
    from services.research_sandbox_service import _today

    return [
        run
        for run in rdb.db_session.query(rdb.ResearchRun).filter(
            rdb.ResearchRun.environment == rdb.LIVE, rdb.ResearchRun.trading_date == _today()
        )
        if (deployment := rdb.db_session.get(rdb.ResearchDeployment, run.deployment_id))
        and deployment.user_id == user_id
    ]


def reconcile_live(user_id: str) -> dict:
    """Today's live sessions against the broker's own record.

    Every platform order with its broker status, and the positions the
    platform expects on each symbol beside the broker's intraday (MIS)
    position. Other trading on the same account (another strategy, a manual
    order) also shows up in the broker's numbers, so a difference is something
    to look at, not proof of an error.
    """
    from database.auth_db import get_api_key_for_tradingview
    from services.positionbook_service import get_positionbook
    from services.strategy_module.order_dispatch import resolve_live_auth

    runs = _today_live_runs(user_id)
    expected: dict[str, int] = {}
    orders = []
    for run in runs:
        for row in _rows(run.id):
            signed = row.filled_quantity if row.quantity > 0 else -row.filled_quantity
            if row.filled_quantity:
                expected[row.symbol] = expected.get(row.symbol, 0) + signed
            if row.status not in ("COMPLETE", "REJECTED", "CANCELLED"):
                orders.append(
                    {
                        "session_id": run.id,
                        "platform_order_id": row.platform_order_id,
                        "broker_order_id": row.broker_order_id,
                        "symbol": row.symbol,
                        "quantity": row.quantity,
                        "status": row.status,
                    }
                )
    broker: dict[str, int] = {}
    error = None
    api_key = get_api_key_for_tradingview(user_id) if runs else None
    if runs:
        auth_token, broker_name, error = resolve_live_auth(api_key) if api_key else (
            None, None, "Create an API key on the API Key page first."
        )  # fmt: skip
        if not error:
            ok, response, _code = get_positionbook(auth_token=auth_token, broker=broker_name)
            if ok:
                for position in (response or {}).get("data") or []:
                    if str(position.get("product", "")).upper() != "MIS":
                        continue
                    symbol = str(position.get("symbol"))
                    broker[symbol] = broker.get(symbol, 0) + int(
                        float(position.get("quantity") or 0)
                    )
            else:
                error = "The broker's positions could not be read."
    symbols = sorted(set(expected) | {s for s in broker if s in expected})
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "error": error,
        "positions": [
            {
                "symbol": s,
                "platform": expected.get(s, 0),
                "broker": broker.get(s) if not error else None,
                "difference": None if error else broker.get(s, 0) - expected.get(s, 0),
            }
            for s in symbols
        ],
        "unsettled_orders": orders,
    }


def live_overview(user_id: str) -> dict:
    """What the Research page shows about Live: whether it is on, how much is allotted,
    the kill switch, and today's live sessions."""
    return {
        "enabled": live_enabled(),
        "allotted": allotted(),
        "kill_switch": kill_switch(),
        "sessions": [rdb.run_to_dict(run) for run in _today_live_runs(user_id)],
    }

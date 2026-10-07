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

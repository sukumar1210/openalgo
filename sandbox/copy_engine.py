"""
Copying a source's position into the Sandbox accounts that follow it.

A Run trades each distinct strategy configuration once, as a *source*, in a
Sandbox account of its own at a reference capital (sandbox/accounts.py). Each
Run member's account *follows* that source: after every fill in the source's
account, each follower gets one MARKET order for the difference between the
position it should hold and the one it holds. The order is placed with the
source's own fill price as its quote, so it fills at once at that price,
through the same checks as any order: margin, the MIS window, the account's
loss limit. Charges are the follower's own, on its own quantity.

**What a follower should hold** is portfolio/run_capital.copy_target: the
source's position scaled to the follower's capital and rounded toward zero,
or the source's quantity for a self-sized strategy, never past the
follower's exposure limit. A follower whose share rounds to zero holds
nothing. Targets, not deltas, are copied, so a copy that was refused is
caught up by the next fill.

**Decided once.** Each (source trade, follow) is claimed by inserting its
row in ``sandbox_copies``, unique on the pair, before any order is placed;
a second caller finds the claim and does nothing. The follower's position
lock is held from reading what it holds until its order is placed.

**Not copied:** the source's own square-off. Every account is squared off on
its own, so copying the source's square-off as well would close a follower
twice and leave it reversed.

**A shared Run** keeps all its members in one account. Each follow then has
its own order name, and what a member holds is read from the trades carrying
that name (portfolio/strategy_pnl.attribute, square-off shares included).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from decimal import Decimal

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# One shared worker, so copies run in the order the fills came, and off the
# thread that filled the source: that can be the market-data thread, which
# delivers every tick and must never wait on another order's position lock.
_COPIES = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sandbox-copy")


def _follows(source: str) -> list:
    from database.sandbox_db import SandboxFollow

    return (
        SandboxFollow.query.filter_by(source=source, active=True).order_by(SandboxFollow.id).all()
    )


def _session_start() -> datetime:
    return IST.localize(datetime.combine(datetime.now(IST).date(), time(0, 0))).replace(tzinfo=None)


def _position(user_id: str, symbol: str, exchange: str, product: str) -> int:
    from database.sandbox_db import SandboxPositions

    row = (
        SandboxPositions.query.filter_by(
            user_id=user_id, symbol=symbol, exchange=exchange, product=product
        )
        .populate_existing()
        .first()
    )
    return int(row.quantity) if row else 0


def _named_position(follow, symbol: str, exchange: str, product: str) -> int:
    """A shared account's member's position: its own orders and its square-off share."""
    from database.sandbox_db import SandboxTrades
    from portfolio.strategy_pnl import SQUARE_OFF_TAG, attribute

    trades = [
        {
            "tradeid": t.tradeid,
            "strategy": t.strategy or "",
            "symbol": t.symbol,
            "exchange": t.exchange,
            "product": t.product,
            "action": t.action,
            "quantity": t.quantity,
            "price": float(t.price),
            "timestamp": t.trade_timestamp,
        }
        for t in SandboxTrades.query.filter(
            SandboxTrades.user_id == follow.follower,
            SandboxTrades.symbol == symbol,
            SandboxTrades.exchange == exchange,
            SandboxTrades.product == product,
            SandboxTrades.strategy.in_([follow.order_name, SQUARE_OFF_TAG]),
            SandboxTrades.trade_timestamp >= _session_start(),
        ).order_by(SandboxTrades.trade_timestamp, SandboxTrades.id)
    ]
    return sum(int(f["qty"]) for f in attribute(trades, [follow.order_name]))


def _held(follow, symbol, exchange, product) -> int:
    if follow.shared:
        return _named_position(follow, symbol, exchange, product)
    return _position(follow.follower, symbol, exchange, product)


def _exposure_elsewhere(follow, symbol, exchange) -> Decimal:
    """What a follower holds in other instruments, at cost."""
    from database.sandbox_db import SandboxPositions

    total = Decimal("0")
    for row in SandboxPositions.query.filter(
        SandboxPositions.user_id == follow.follower, SandboxPositions.quantity != 0
    ).populate_existing():
        if (row.symbol, row.exchange) == (symbol, exchange):
            continue
        total += abs(Decimal(row.quantity)) * Decimal(str(row.average_price or 0))
    return total


def _claim(tradeid: str, follow_id: int) -> bool:
    """Claim one (source trade, follow); False when another caller already has."""
    from sqlalchemy.exc import IntegrityError

    from database.sandbox_db import SandboxCopy, db_session

    try:
        db_session.add(SandboxCopy(source_tradeid=tradeid, follow_id=follow_id, status="claimed"))
        db_session.commit()
        return True
    except IntegrityError:
        db_session.rollback()
        return False


def _settle(tradeid: str, follow_id: int, **values) -> None:
    from database.sandbox_db import SandboxCopy, db_session

    SandboxCopy.query.filter_by(source_tradeid=tradeid, follow_id=follow_id).update(values)
    db_session.commit()


def after_fill(order, tradeid: str, price) -> None:
    """Queue the copies of a fill in a source's account; returns at once.

    Called by the execution engine once a fill and its position are committed.
    Never raises: a copy that cannot be made costs that copy, never the
    source's fill.
    """
    from portfolio.strategy_pnl import SQUARE_OFF_TAG
    from sandbox.accounts import is_source

    try:
        if (order.strategy or "") == SQUARE_OFF_TAG or not is_source(order.user_id):
            return
        _COPIES.submit(
            _copy_all,
            order.user_id,
            order.symbol,
            order.exchange,
            order.product,
            tradeid,
            Decimal(str(price)),
        )
    except Exception:
        logger.exception(f"Copies of trade {tradeid} were not queued")


def wait_idle(timeout: float = 30.0) -> None:
    """Wait until every copy queued so far is done (the worker runs them in order)."""
    _COPIES.submit(lambda: None).result(timeout=timeout)


def _copy_all(source, symbol, exchange, product, tradeid: str, price: Decimal) -> None:
    from database.sandbox_db import db_session
    from utils.keyed_locks import LockBusy

    try:
        follows = _follows(source)
        # Read now, not at the fill: a later fill already queued then finds
        # its follower at target, and the two converge on one order.
        source_quantity = _position(source, symbol, exchange, product)
        for follow in follows:
            try:
                _copy(follow, symbol, exchange, product, tradeid, price, source_quantity)
            except LockBusy:
                logger.warning(f"Copy of {tradeid} to {follow.follower} skipped: position busy")
            except Exception:
                logger.exception(f"Copy of {tradeid} to {follow.follower} failed")
    finally:
        db_session.remove()


def _copy(follow, symbol, exchange, product, tradeid: str, price: Decimal, source_quantity: int):
    from portfolio.run_capital import copy_target
    from sandbox.order_manager import OrderManager
    from sandbox.position_locks import position_lock

    with position_lock(follow.follower, exchange, symbol, product):
        held = _held(follow, symbol, exchange, product)
        target = copy_target(
            source_quantity,
            scale=Decimal(str(follow.scale)),
            sizing=follow.sizing,
            price=price,
            limit=Decimal(str(follow.limit_amount)),
            exposure_elsewhere=_exposure_elsewhere(follow, symbol, exchange),
        )
        delta = target - held
        if delta == 0 or not _claim(tradeid, follow.id):
            return
        ok, response, _code = OrderManager(follow.follower).place_order(
            {
                "symbol": symbol,
                "exchange": exchange,
                "action": "BUY" if delta > 0 else "SELL",
                "quantity": abs(delta),
                "price_type": "MARKET",
                "product": product,
                "strategy": follow.order_name,
            },
            prefetched_quote={"ltp": float(price)},
        )
    if ok:
        _settle(tradeid, follow.id, status="placed", orderid=response.get("orderid"),
                quantity=delta)  # fmt: skip
    else:
        reason = (response or {}).get("message", "refused")
        _settle(tradeid, follow.id, status="refused", quantity=delta, reason=str(reason)[:200])
        logger.info(f"Copy of {tradeid} to {follow.follower} refused: {reason}")


def set_follow(
    source: str,
    follower: str,
    order_name: str,
    *,
    scale,
    limit,
    sizing: str = "platform",
    shared: bool = False,
    active: bool = True,
    run_id: int | None = None,
    member_id: int | None = None,
):
    """Create or update how ``follower`` follows ``source`` under ``order_name``.

    A Run calls this at each day's open with that day's scale and limit.
    Returns the row.
    """
    from database.sandbox_db import SandboxFollow, db_session

    row = SandboxFollow.query.filter_by(
        source=source, follower=follower, order_name=order_name
    ).first()
    if row is None:
        row = SandboxFollow(source=source, follower=follower, order_name=order_name)
        db_session.add(row)
    row.scale = Decimal(str(scale))
    row.limit_amount = Decimal(str(limit)).quantize(Decimal("0.01"))
    row.sizing = sizing
    row.shared = shared
    row.active = active
    row.run_id = run_id
    row.member_id = member_id
    db_session.commit()
    return row

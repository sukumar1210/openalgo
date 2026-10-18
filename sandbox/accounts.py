"""
Sandbox accounts: money of its own inside one user's Sandbox.

The Sandbox keys every row by a ``user_id`` string: funds, orders, trades,
positions, holdings, GTTs and daily P&L. An account is a registered principal,
``<owner>~<token>``, used as that ``user_id``. Everything the Sandbox does
(margin, fills, the 15:15 square-off, T+1, the loss limit) then applies to it
on its own, sharing nothing with the owner's main Sandbox or with another
account. It is what lets two scenarios trade side by side without one's loss
spending the other's money.

**Trust comes from the registry, never from the string.** An account key
(``utils.strategy_mode.sandbox_key_for(key, token)``) can only be made from
the owner's real API key, and resolves to a principal only while this
registry holds that principal active (``principal_for``). FundManager refuses
to open funds for an unregistered principal of an account's shape. So nothing
reaches the Sandbox tables as an account without having been registered here.

**The one exception is ``owner_of``,** which reads the owner off the
principal's shape (``<owner>~`` and 8 hex digits) without a database read: the
WebSocket engine calls it under a real lock, where no I/O is allowed. That is
sound because only this module creates principals of that shape. A username
that itself ends in ``~`` and 8 hex digits is not supported.

**Money moved by anything but trading is an event.** Opening an account,
deposits, withdrawals, transfers and per-fill charges each leave a row in
``sandbox_account_events`` and change the funds row in the same commit, so
the balance is always the starting capital plus trading plus events.

Off unless SANDBOX_ACCOUNTS_ENABLED is true. Off, no account key resolves and
the Sandbox behaves exactly as before.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import date, datetime
from decimal import Decimal

import pytz

from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache

logger = get_logger(__name__)

SEPARATOR = "~"
_SHAPE = re.compile(r"(.+)~([0-9a-f]{8})")
#: What an account was opened for (Phase 2's Runs use these).
KINDS = ("run", "member", "source")

#: An account's charge setting and status, read on every one of its fills.
_info = LockedTTLCache(maxsize=512, ttl=60)


class AccountError(ValueError):
    """An account request refused, said so a trader can act on it."""


def enabled() -> bool:
    return os.getenv("SANDBOX_ACCOUNTS_ENABLED", "false").strip().lower() == "true"


def owner_of(principal: str) -> str:
    """The user an account belongs to; a main account's own principal.

    Pure: no database read, safe under a real lock (see the module docstring).
    """
    match = _SHAPE.fullmatch(principal or "")
    return match.group(1) if match else principal


def is_account(principal: str) -> bool:
    """Whether ``principal`` has an account's shape (registered or not)."""
    return _SHAPE.fullmatch(principal or "") is not None


def _row(principal: str):
    from database.sandbox_db import SandboxAccount

    return SandboxAccount.query.filter_by(principal=principal).first()


def _as_dict(row) -> dict:
    return {
        "principal": row.principal,
        "owner": row.owner,
        "token": row.principal.rsplit(SEPARATOR, 1)[1],
        "kind": row.kind,
        "label": row.label,
        "starting_capital": float(row.starting_capital),
        "charges": bool(row.charges),
        "daily_loss_limit": (
            float(row.daily_loss_limit) if row.daily_loss_limit is not None else None
        ),
        "run_id": row.run_id,
        "member_id": row.member_id,
        "fingerprint": row.fingerprint,
        "status": row.status,
    }


def get(principal: str) -> dict | None:
    """The account's registration, active or closed; None for anything else."""
    if not is_account(principal):
        return None
    row = _row(principal)
    return _as_dict(row) if row is not None else None


def _cached(principal: str) -> dict | None:
    hit = _info.get(principal)
    if hit is None:
        hit = get(principal) or {}
        _info[principal] = hit
    return hit or None


def charges_on(principal: str) -> bool:
    """Whether fills in this account are charged. Never for a main account."""
    if not is_account(principal):
        return False
    account = _cached(principal)
    return bool(account and account["charges"])


def is_source(principal: str) -> bool:
    """Whether fills in this account are copied to its followers (sandbox/copy_engine.py)."""
    if not is_account(principal):
        return False
    account = _cached(principal)
    return bool(account and account["kind"] == "source" and account["status"] == "active")


def starting_capital(principal: str) -> Decimal | None:
    """An account's starting capital; None for a main account or an unknown one."""
    account = _cached(principal) if is_account(principal) else None
    return Decimal(str(account["starting_capital"])) if account else None


def daily_loss_limit(principal: str) -> Decimal | None:
    """The most an account may lose today, as its Run set it; None when nothing did."""
    account = _cached(principal) if is_account(principal) else None
    limit = account["daily_loss_limit"] if account else None
    return Decimal(str(limit)) if limit is not None else None


def set_daily_loss_limit(principal: str, amount) -> None:
    """Set the most ``principal`` may lose in one session, in rupees; None removes it.

    The Sandbox's loss-limit check enforces it (sandbox/squareoff_manager.py),
    beside the Sandbox's own limit: whichever is reached first.
    """
    from database.sandbox_db import db_session

    value = None if amount is None else Decimal(str(amount)).quantize(Decimal("0.01"))
    if value is not None and value <= 0:
        raise AccountError("A daily loss limit is an amount above zero.")
    row = _row(principal) if is_account(principal) else None
    if row is None:
        raise AccountError("That Sandbox account does not exist.")
    if row.daily_loss_limit != value:
        row.daily_loss_limit = value
        db_session.commit()
    _info.pop(principal, None)


def any_loss_limited() -> bool:
    """Whether an open account has a loss limit of its own, so the check has work."""
    if not enabled():
        return False
    from database.sandbox_db import SandboxAccount

    return (
        SandboxAccount.query.filter(
            SandboxAccount.status == "active", SandboxAccount.daily_loss_limit.isnot(None)
        ).first()
        is not None
    )


def principal_for(owner: str, token: str) -> str | None:
    """The active account ``owner`` holds under ``token``, or None."""
    if not enabled():
        return None
    principal = f"{owner}{SEPARATOR}{token}"
    account = _cached(principal)
    if account is None or account["owner"] != owner or account["status"] != "active":
        return None
    return principal


def any_active() -> bool:
    """Whether any account is open, so the Sandbox's engines are needed."""
    if not enabled():
        return False
    from database.sandbox_db import SandboxAccount

    return SandboxAccount.query.filter_by(status="active").first() is not None


def _today() -> date:
    return datetime.now(pytz.timezone("Asia/Kolkata")).date()


def _event(principal, kind, amount, *, counterparty=None, reason=None, reference=None):
    from database.sandbox_db import SandboxAccountEvent

    return SandboxAccountEvent(
        principal=principal,
        kind=kind,
        amount=Decimal(str(amount)),
        counterparty=counterparty,
        reason=reason,
        reference=reference,
        trade_date=_today(),
    )


def create(
    owner: str,
    label: str,
    capital: float,
    kind: str,
    *,
    charges: bool = True,
    run_id: int | None = None,
    member_id: int | None = None,
    fingerprint: str | None = None,
) -> dict:
    """Open an account for ``owner`` with ``capital``, its funds and its first event.

    Returns:
        The registration, with its ``principal`` and ``token``.
    """
    from database.sandbox_db import SandboxAccount, SandboxFunds, db_session

    if not enabled():
        raise AccountError("Sandbox accounts are off. Set SANDBOX_ACCOUNTS_ENABLED to true.")
    if kind not in KINDS:
        raise AccountError(f"An account is opened for one of: {', '.join(KINDS)}.")
    amount = Decimal(str(capital)).quantize(Decimal("0.01"))
    if amount <= 0:
        raise AccountError("Give the account some capital.")
    if is_account(owner):
        raise AccountError("An account cannot own another account.")
    principal = f"{owner}{SEPARATOR}{secrets.token_hex(4)}"
    if len(principal) > 50:
        raise AccountError("This username is too long to hold Sandbox accounts.")
    try:
        db_session.add(
            SandboxAccount(
                principal=principal,
                owner=owner,
                kind=kind,
                label=(label or "")[:100],
                starting_capital=amount,
                charges=charges,
                run_id=run_id,
                member_id=member_id,
                fingerprint=fingerprint,
                status="active",
            )
        )
        db_session.add(
            SandboxFunds(
                user_id=principal,
                total_capital=amount,
                available_balance=amount,
                used_margin=Decimal("0.00"),
                realized_pnl=Decimal("0.00"),
                today_realized_pnl=Decimal("0.00"),
                unrealized_pnl=Decimal("0.00"),
                total_pnl=Decimal("0.00"),
                last_reset_date=datetime.now(pytz.timezone("Asia/Kolkata")),
                reset_count=0,
            )
        )
        db_session.add(_event(principal, "open", amount, reason=label))
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    _info.pop(principal, None)
    logger.info("Sandbox account %s opened for %s with %s", principal, owner, amount)
    return get(principal)


def _holds_anything(principal: str) -> bool:
    from database.sandbox_db import SandboxGTT, SandboxOrders, SandboxPositions
    from sandbox.order_manager import _PENDING_STATUSES

    return (
        SandboxPositions.query.filter(
            SandboxPositions.user_id == principal, SandboxPositions.quantity != 0
        ).first()
        is not None
        or SandboxOrders.query.filter(
            SandboxOrders.user_id == principal,
            SandboxOrders.order_status.in_(_PENDING_STATUSES),
        ).first()
        is not None
        or SandboxGTT.query.filter_by(user_id=principal, gtt_status="active").first() is not None
    )


def close(principal: str) -> None:
    """Close an account. Refused while it holds a position, a resting order or a GTT."""
    from database.sandbox_db import db_session

    row = _row(principal)
    if row is None or row.status != "active":
        raise AccountError("That Sandbox account is not open.")
    if _holds_anything(principal):
        raise AccountError(
            "This account still holds a position, a resting order or a GTT. "
            "Close them first; the account stays open until then."
        )
    row.status = "closed"
    row.closed_at = datetime.now(pytz.timezone("Asia/Kolkata"))
    db_session.commit()
    _info.pop(principal, None)


def move_money(
    principal: str, amount: float, kind: str, *, reason: str, counterparty=None, reference=None
) -> bool:
    """Add ``amount`` to an account's cash (negative takes it out), as one event.

    The capital and the available balance move together, so a deposit is
    tradable at once and a withdrawal is refused beyond what is free. With a
    ``reference``, the move happens once: a second call with the same one
    finds its event and does nothing.

    Returns:
        False when ``reference`` had already moved the money.
    """
    from database.sandbox_db import SandboxAccountEvent, db_session
    from sandbox.fund_manager import apply_funds_change

    if reference is not None and (
        SandboxAccountEvent.query.filter_by(principal=principal, reference=reference).first()
        is not None
    ):
        return False
    value = Decimal(str(amount)).quantize(Decimal("0.01"))

    def compute(values):
        if values["available_balance"] + value < 0:
            return False, "Not that much is free in this account.", {}
        return (
            True,
            "",
            {
                "total_capital": values["total_capital"] + value,
                "available_balance": values["available_balance"] + value,
            },
        )

    account = get(principal)
    if account is None or account["status"] != "active":
        raise AccountError("That Sandbox account is not open.")
    try:
        ok, message = apply_funds_change(principal, compute)
        if not ok:
            raise AccountError(message)
        db_session.add(
            _event(
                principal,
                kind,
                value,
                reason=reason,
                counterparty=counterparty,
                reference=reference,
            )
        )
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    return True


def equity(principal: str) -> Decimal | None:
    """What an account is worth now: its cash, the margin it has out, and its open P&L."""
    from sandbox.fund_manager import read_funds_snapshot

    snapshot = read_funds_snapshot(principal)
    if snapshot is None:
        return None
    values = snapshot.values
    return (
        Decimal(str(values["available_balance"] or 0))
        + Decimal(str(values["used_margin"] or 0))
        + Decimal(str(values["unrealized_pnl"] or 0))
    )


def stage_fill_charges(principal: str, fill: dict, reference: str) -> Decimal | None:
    """Stage a fill's charges in the caller's transaction; None when none apply.

    Debits the account's cash and realized P&L, today's included, so the day's
    loss limit sees them, and records the event against ``reference`` (the
    trade id). Nothing is committed: the fill's own commit carries both.
    """
    from database.sandbox_db import db_session
    from portfolio.strategy_pnl import fill_charges
    from sandbox.fund_manager import apply_funds_change

    if not charges_on(principal):
        return None
    amount = fill_charges(fill)
    if not amount:
        return None
    value = Decimal(str(amount)).quantize(Decimal("0.01"))

    def compute(values):
        realized = values["realized_pnl"] - value
        return (
            True,
            "",
            {
                "available_balance": values["available_balance"] - value,
                "realized_pnl": realized,
                "today_realized_pnl": (values["today_realized_pnl"] or Decimal("0.00")) - value,
                "total_pnl": realized + values["unrealized_pnl"],
            },
        )

    ok, message = apply_funds_change(principal, compute)
    if not ok:
        logger.error("Charges of %s on %s were not taken: %s", value, reference, message)
        return None
    db_session.add(_event(principal, "charges", -value, reference=reference))
    return value

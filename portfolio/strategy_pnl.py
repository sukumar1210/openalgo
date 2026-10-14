"""
One strategy's share of the sandbox account, from the trades its orders carry.

The sandbox keeps one account: positions are netted per symbol and product,
and the daily snapshots are the account's. A strategy's own P&L can still be
read exactly from the trades, because every order carries the ``strategy`` name
it was sent with. Two things make that more than a filter:

* **The square-off.** At the intraday square-off time the sandbox closes the
  account's net MIS position with an order tagged ``AUTO_SQUARE_OFF``. A
  strategy holding part of that position was closed by it, so a square-off is
  given to the strategy only up to what it held, in the direction that closes
  it. Leaving it out would show a closed intraday position as still open.
* **Average cost, per symbol and product.** Realized P&L is the closing
  quantity times the move from the strategy's own average price, as the
  sandbox computes the account's.

Pure functions over plain rows: no database, no clock, no broker. The Sandbox
P&L page and Research's record of a Python strategy's Sandbox days both read
a strategy's fills through ``attribute``, so the two always agree.

**Charges and a strategy's own capital.** ``fill_charges`` costs a fill with
the schedule Research's Sandbox days use (portfolio/costs.py: NSE or BSE
equity, intraday unless the product is CNC); a fill on another exchange has no
schedule and is counted before charges, and ``daily_results`` says so.
``capital_on`` is a strategy's rolling capital: its starting capital plus its
own P&L after charges for every day before the one asked about, so a day's
result is the strategy's own to use from the next day.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from functools import cache

#: The tag the sandbox's own square-off orders carry (sandbox/position_manager).
SQUARE_OFF_TAG = "AUTO_SQUARE_OFF"


def attribute(trades: Iterable[Mapping], tags) -> list[dict]:
    """The fills that belong to a strategy: its own, then the square-offs of what it held.

    Args:
        trades: Trade rows in time order, each with ``strategy``, ``symbol``,
            ``exchange``, ``product``, ``action``, ``quantity``, ``price`` and
            ``timestamp``. Rows with other tags are ignored.
        tags: The strategy name, or names, its orders carry.

    Returns:
        One dict per fill: the row's fields, ``qty`` signed (+ bought), and
        ``reason``: ``signal`` for its own orders, ``square_off`` for the
        sandbox's.
    """
    tags = {tags} if isinstance(tags, str) else set(tags)
    held: dict[tuple, int] = {}
    fills = []
    for row in trades:
        strategy = row.get("strategy") or ""
        if strategy not in tags and strategy != SQUARE_OFF_TAG:
            continue
        quantity = int(row["quantity"])
        qty = quantity if str(row["action"]).upper() == "BUY" else -quantity
        key = (row["symbol"], row.get("exchange"), row.get("product"))
        reason = "signal"
        if strategy == SQUARE_OFF_TAG:
            position = held.get(key, 0)
            if not position or (position > 0) == (qty > 0):
                continue
            qty = max(qty, -position) if qty < 0 else min(qty, -position)
            reason = "square_off"
        held[key] = held.get(key, 0) + qty
        fills.append({**row, "qty": qty, "reason": reason})
    return fills


def book(fills: Iterable[Mapping], ltp: Mapping[tuple, float], today: date) -> dict:
    """A strategy's positions, daily realized P&L and totals, from its fills.

    Args:
        fills: From ``attribute``, in time order.
        ltp: The latest price per ``(symbol, exchange, product)``, to mark what
            is still open. A key without one is marked at its average price.
        today: The IST date "today" means.

    Returns:
        ``{"positions": [...], "daily": [...], "summary": {...}}``: positions by
        symbol and product (open ones first), realized P&L per date (newest
        first), and today's and all-time totals.
    """
    positions: dict[tuple, dict] = {}
    daily: dict[str, dict] = {}
    for fill in fills:
        key = (fill["symbol"], fill.get("exchange"), fill.get("product"))
        pos = positions.setdefault(
            key,
            {
                "symbol": key[0],
                "exchange": key[1],
                "product": key[2],
                "quantity": 0,
                "average_price": 0.0,
                "today_realized_pnl": 0.0,
                "all_time_realized_pnl": 0.0,
                "updated_at": None,
            },
        )
        qty, price = int(fill["qty"]), float(fill["price"])
        before = pos["quantity"]
        realized = 0.0
        if before and (before > 0) != (qty > 0):
            closed = min(abs(qty), abs(before))
            realized = closed * (price - pos["average_price"]) * (1 if before > 0 else -1)
        after = before + qty
        if after == 0:
            pos["average_price"] = 0.0
        elif not before or (before > 0) != (after > 0):
            pos["average_price"] = price
        elif abs(after) > abs(before):
            pos["average_price"] = (pos["average_price"] * abs(before) + price * abs(qty)) / abs(
                after
            )
        pos["quantity"] = after
        stamp = fill["timestamp"]
        day = stamp.date() if hasattr(stamp, "date") else date.fromisoformat(str(stamp)[:10])
        pos["all_time_realized_pnl"] += realized
        if day == today:
            pos["today_realized_pnl"] += realized
        pos["updated_at"] = stamp
        entry = daily.setdefault(
            day.isoformat(), {"date": day.isoformat(), "realized_pnl": 0.0, "trades": 0}
        )
        entry["realized_pnl"] += realized
        entry["trades"] += 1

    unrealized = 0.0
    rows = []
    for key, pos in positions.items():
        mark = ltp.get(key) or pos["average_price"]
        open_pnl = pos["quantity"] * (mark - pos["average_price"]) if pos["quantity"] else 0.0
        unrealized += open_pnl
        rows.append(
            {
                **pos,
                "ltp": float(mark or 0.0),
                "unrealized_pnl": open_pnl,
                "status": "Open" if pos["quantity"] else "Closed",
            }
        )
    rows.sort(key=lambda r: (r["status"] != "Open", str(r["updated_at"])), reverse=False)
    today_realized = sum(r["today_realized_pnl"] for r in rows)
    all_time = sum(r["all_time_realized_pnl"] for r in rows)
    return {
        "positions": rows,
        "daily": sorted(daily.values(), key=lambda d: d["date"], reverse=True),
        "summary": {
            "today_realized_pnl": today_realized,
            "all_time_realized_pnl": all_time,
            "positions_unrealized_pnl": unrealized,
            "total_unrealized_pnl": unrealized,
            "today_total_mtm": today_realized + unrealized,
            "total_pnl": all_time + unrealized,
        },
    }


@cache
def _schedule(exchange: str, product: str):
    from portfolio.costs import schedule_for

    if exchange not in ("NSE", "BSE"):
        return None
    kind = "delivery" if product == "CNC" else "intraday"
    return schedule_for(f"india_{kind}_{exchange.lower()}")


def fill_charges(fill: Mapping) -> float | None:
    """Brokerage, taxes and fees on one fill, as a Sandbox day is charged; None when unmodelled."""
    schedule = _schedule(str(fill.get("exchange") or ""), str(fill.get("product") or ""))
    if schedule is None:
        return None
    notional = abs(int(fill["qty"])) * float(fill["price"])
    buy, sell = (notional, 0.0) if fill["qty"] > 0 else (0.0, notional)
    breakdown = schedule.breakdown(buy, sell, orders=1)
    return breakdown["total"] - breakdown["slippage"]


def daily_results(fills: list[Mapping], today: date) -> list[dict]:
    """Each trading day's realized P&L before and after charges, oldest first.

    ``unmodelled`` counts the day's fills no schedule covers; their charges are
    left out, so that day's ``net`` is before their charges.
    """
    by_day = {d["date"]: d for d in book(fills, {}, today)["daily"]}
    charged: dict[str, list] = {}
    for fill in fills:
        stamp = fill["timestamp"]
        day = stamp.date() if hasattr(stamp, "date") else date.fromisoformat(str(stamp)[:10])
        charged.setdefault(day.isoformat(), []).append(fill_charges(fill))
    out = []
    for day in sorted(by_day):
        costs = charged.get(day, [])
        charges = sum(c for c in costs if c is not None)
        out.append(
            {
                "date": day,
                "pnl": by_day[day]["realized_pnl"],
                "charges": charges,
                "net": by_day[day]["realized_pnl"] - charges,
                "trades": by_day[day]["trades"],
                "unmodelled": sum(1 for c in costs if c is None),
            }
        )
    return out


def capital_on(start_capital: float, daily: list[Mapping], day: date) -> float:
    """A strategy's capital at the start of ``day``: its own net P&L of earlier days added."""
    return start_capital + sum(d["net"] for d in daily if d["date"] < day.isoformat())

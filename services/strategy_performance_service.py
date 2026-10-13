"""
Every strategy's Sandbox trading, one strategy at a time or as one account.

Every Sandbox order carries the name of the strategy that sent it, so the
sandbox's trades are already a ledger for all of them. A strategy is the set
of names its orders carry:

* a Python strategy: the names learned from when it ran
  (services.research_hosted_service) and its own name, which it is handed as
  STRATEGY_NAME;
* a strategy-module strategy: its name, which the module puts on every order;
* any other name: a row of its own, so nothing traded is left out.

A strategy's fills, with its share of the sandbox's square-off, and its P&L
come from sandbox/strategy_pnl: the rule the Sandbox P&L filter and the
Sandbox days tab use, so the three never disagree. Combined, the chosen
strategies' daily P&L is added into one curve, as the one sandbox account they
share.

Daily P&L is realized P&L by trade date (the sandbox's average-price rule,
before any charges). An intraday strategy is flat at the day's end, so for it
that is the day's whole result.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

import numpy as np
import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
#: Research's own Sandbox sessions tag their orders with this prefix; they are
#: not strategies a trader runs.
RESEARCH_TAG_PREFIX = "research:"


def sandbox_trades(user_id: str, tags=None) -> list[dict]:
    """The user's sandbox trades as plain rows, oldest first; only these names if given."""
    from database.sandbox_db import SandboxTrades, db_session
    from sandbox.strategy_pnl import SQUARE_OFF_TAG

    query = db_session.query(SandboxTrades).filter(SandboxTrades.user_id == user_id)
    if tags is not None:
        query = query.filter(SandboxTrades.strategy.in_([*tags, SQUARE_OFF_TAG]))
    return [
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
        for t in query.order_by(SandboxTrades.trade_timestamp, SandboxTrades.id).all()
    ]


def _python_strategies() -> list[tuple[str, str, set[str]]]:
    """(script id, name, order names) for each Python strategy."""
    from database import research_db as rdb
    from services.research_hosted_service import _host_configs

    learned = {
        link.script_id: set(link.learned_tags or [])
        for link in rdb.db_session.query(rdb.ResearchScriptLink).all()
    }
    return [
        (sid, config.get("name") or sid, learned.get(sid, set()) | {config.get("name") or sid})
        for sid, config in _host_configs().items()
    ]


def _module_strategies(user_id: str) -> list[tuple[int, str]]:
    from database.strategy_module_db import SmStrategy, db_session

    return [
        (row.id, row.name)
        for row in db_session.query(SmStrategy).filter(SmStrategy.user_id == user_id).all()
    ]


def strategies(user_id: str, traded: set[str]) -> list[dict]:
    """Each strategy that has traded, with the order names that are its own.

    A name is claimed once: by the Python strategy that learned it or is
    called it, else by the strategy-module strategy called it, else it is a
    row of its own.
    """
    from sandbox.strategy_pnl import SQUARE_OFF_TAG

    names = {
        t for t in traded if t and t != SQUARE_OFF_TAG and not t.startswith(RESEARCH_TAG_PREFIX)
    }
    rows, claimed = [], set()
    owners: list[tuple[str, str, str, set[str]]] = []
    try:
        owners += [
            ("python", f"python:{sid}", name, tags) for sid, name, tags in _python_strategies()
        ]
    except Exception:
        logger.exception("Strategy dashboard: Python strategies could not be read")
    try:
        owners += [
            ("module", f"module:{sid}", name, {name}) for sid, name in _module_strategies(user_id)
        ]
    except Exception:
        logger.exception("Strategy dashboard: strategy-module strategies could not be read")
    for kind, key, name, tags in owners:
        mine = (tags & names) - claimed
        if mine:
            claimed |= mine
            rows.append({"key": key, "kind": kind, "name": name, "tags": sorted(mine)})
    rows += [
        {"key": f"name:{tag}", "kind": "other", "name": tag, "tags": [tag]}
        for tag in sorted(names - claimed)
    ]
    return rows


def order_name_owners(user_id: str, traded: set[str]) -> dict[str, dict]:
    """Each order name's strategy, for pages that list order names."""
    return {tag: row for row in strategies(user_id, traded) for tag in row["tags"]}


def daily_summary(daily: list[dict], capital: float) -> dict:
    """Totals and drawdown of a daily P&L series (oldest first)."""
    from research.metrics import max_drawdown

    pnl = np.array([d["pnl"] for d in daily], dtype=float)
    cumulative = np.concatenate(([0.0], np.cumsum(pnl)))
    peaks = np.maximum.accumulate(cumulative)
    return {
        "total_pnl": float(cumulative[-1]),
        "days": len(daily),
        "winning_days": int((pnl > 0).sum()),
        "best_day": float(pnl.max()) if len(pnl) else None,
        "worst_day": float(pnl.min()) if len(pnl) else None,
        "max_drawdown": float((cumulative - peaks).min()),
        "max_drawdown_pct": max_drawdown(capital + cumulative) * 100 if capital else None,
        "trades": int(sum(d["trades"] for d in daily)),
        "last_day": daily[-1]["date"] if daily else None,
    }


def _series(daily: list[dict]) -> list[dict]:
    total, out = 0.0, []
    for d in daily:
        total += d["pnl"]
        out.append({**d, "cumulative": total})
    return out


def performance(user_id: str, start: str | None = None, end: str | None = None, combine=()) -> dict:
    """Every strategy's daily P&L and totals in a window, and the chosen ones combined."""
    from sandbox.strategy_pnl import attribute, book
    from services.research_hosted_service import sandbox_money

    trades = sandbox_trades(user_id)
    capital, _leverage = sandbox_money()
    today = datetime.now(IST).date()
    first = date.fromisoformat(start) if start else None
    last = date.fromisoformat(end) if end else None
    out = []
    for row in strategies(user_id, {t["strategy"] for t in trades}):
        result = book(attribute(trades, row["tags"]), {}, today)
        daily = sorted(
            (
                {"date": d["date"], "pnl": d["realized_pnl"], "trades": d["trades"]}
                for d in result["daily"]
                if (first is None or d["date"] >= first.isoformat())
                and (last is None or d["date"] <= last.isoformat())
            ),
            key=lambda d: d["date"],
        )
        held = [p for p in result["positions"] if p["quantity"]]
        out.append(
            {
                **row,
                "summary": {**daily_summary(daily, capital), "open_positions": len(held)},
                "daily": _series(daily),
            }
        )
    chosen = [row for row in out if row["key"] in set(combine)]
    combined = None
    if chosen:
        by_day: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0})
        for row in chosen:
            for d in row["daily"]:
                by_day[d["date"]]["pnl"] += d["pnl"]
                by_day[d["date"]]["trades"] += d["trades"]
        daily = [{"date": day, **by_day[day]} for day in sorted(by_day)]
        combined = {
            "keys": [row["key"] for row in chosen],
            "summary": daily_summary(daily, capital),
            "daily": _series(daily),
            "contributions": [
                {"key": row["key"], "name": row["name"], "total_pnl": row["summary"]["total_pnl"]}
                for row in chosen
            ],
        }
    return {"capital": capital, "strategies": out, "combined": combined}


def ledger(user_id: str, key: str) -> list[dict]:
    """One strategy's fills, newest first: its own orders and its share of the square-off."""
    from sandbox.strategy_pnl import attribute

    trades = sandbox_trades(user_id)
    row = next(
        (r for r in strategies(user_id, {t["strategy"] for t in trades}) if r["key"] == key), None
    )
    if row is None:
        return []
    return [
        {
            "time": f["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if f["timestamp"] else "",
            "order_name": f["strategy"],
            "symbol": f["symbol"],
            "exchange": f["exchange"],
            "product": f["product"],
            "action": "BUY" if f["qty"] > 0 else "SELL",
            "quantity": abs(f["qty"]),
            "price": f["price"],
            "square_off": f["reason"] == "square_off",
        }
        for f in reversed(attribute(trades, row["tags"]))
    ]

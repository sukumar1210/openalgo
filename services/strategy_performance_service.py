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
come from portfolio/strategy_pnl: the rule the Sandbox P&L filter and the
Sandbox days tab use, so the three never disagree. Combined, the chosen
strategies' daily P&L is added into one curve, as the one sandbox account they
share.

Daily P&L is realized P&L by trade date (the sandbox's average-price rule),
shown before and after the charges a Sandbox day is costed with. An intraday
strategy is flat at the day's end, so for it that is the day's whole result.

**A Python strategy's own capital.** Each declares a starting capital on its
Schedule tab (Rs 5,00,000 unless set). Its capital on a day is that plus its
own P&L after charges of every earlier day (portfolio/strategy_pnl.capital_on):
today's result is its own, to use from tomorrow. The host hands it to the
script as OPENALGO_STRATEGY_CAPITAL; drawdown and return are measured on it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
#: A Python strategy's starting capital unless its Schedule tab sets one: what
#: Alpha A and RES-1 sized every position with before capital was per strategy.
DEFAULT_CAPITAL = 500_000.0
#: Research's own Sandbox sessions tag their orders with this prefix; they are
#: not strategies a trader runs.
RESEARCH_TAG_PREFIX = "research:"


def sandbox_trades(user_id: str, tags=None) -> list[dict]:
    """The user's sandbox trades as plain rows, oldest first; only these names if given."""
    from database.sandbox_db import SandboxTrades, db_session
    from portfolio.strategy_pnl import SQUARE_OFF_TAG

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


def start_capital(config: dict) -> float:
    """A Python strategy's declared starting capital."""
    return float(config.get("capital") or DEFAULT_CAPITAL)


def _python_strategies() -> list[tuple[str, str, set[str], float]]:
    """(script id, name, order names, starting capital) for each Python strategy."""
    from database import research_db as rdb
    from services.research_hosted_service import _host_configs

    learned = {
        link.script_id: set(link.learned_tags or [])
        for link in rdb.db_session.query(rdb.ResearchScriptLink).all()
    }
    return [
        (
            sid,
            config.get("name") or sid,
            learned.get(sid, set()) | {config.get("name") or sid},
            start_capital(config),
        )
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
    from portfolio.strategy_pnl import SQUARE_OFF_TAG

    names = {
        t for t in traded if t and t != SQUARE_OFF_TAG and not t.startswith(RESEARCH_TAG_PREFIX)
    }
    rows, claimed = [], set()
    owners: list[tuple[str, str, str, set[str], float | None]] = []
    try:
        owners += [
            ("python", f"python:{sid}", name, tags, capital)
            for sid, name, tags, capital in _python_strategies()
        ]
    except Exception:
        logger.exception("Strategy dashboard: Python strategies could not be read")
    try:
        owners += [
            ("module", f"module:{sid}", name, {name}, None)
            for sid, name in _module_strategies(user_id)
        ]
    except Exception:
        logger.exception("Strategy dashboard: strategy-module strategies could not be read")
    for kind, key, name, tags, capital in owners:
        mine = (tags & names) - claimed
        if mine:
            claimed |= mine
            rows.append(
                {
                    "key": key,
                    "kind": kind,
                    "name": name,
                    "tags": sorted(mine),
                    "start_capital": capital,
                }
            )
    rows += [
        {"key": f"name:{tag}", "kind": "other", "name": tag, "tags": [tag], "start_capital": None}
        for tag in sorted(names - claimed)
    ]
    return rows


def order_name_owners(user_id: str, traded: set[str]) -> dict[str, dict]:
    """Each order name's strategy, for pages that list order names."""
    return {tag: row for row in strategies(user_id, traded) for tag in row["tags"]}


def daily_summary(daily: list[dict], capital: float) -> dict:
    """Totals and drawdown of a daily series (oldest first), after charges."""
    from research.metrics import max_drawdown

    net = np.array([d["net"] for d in daily], dtype=float)
    cumulative = np.concatenate(([0.0], np.cumsum(net)))
    peaks = np.maximum.accumulate(cumulative)
    return {
        "total_pnl": float(cumulative[-1]),
        "gross_pnl": float(sum(d["pnl"] for d in daily)),
        "charges": float(sum(d["charges"] for d in daily)),
        "unmodelled_fills": int(sum(d.get("unmodelled", 0) for d in daily)),
        "days": len(daily),
        "winning_days": int((net > 0).sum()),
        "best_day": float(net.max()) if len(net) else None,
        "worst_day": float(net.min()) if len(net) else None,
        "max_drawdown": float((cumulative - peaks).min()),
        "max_drawdown_pct": max_drawdown(capital + cumulative) * 100 if capital else None,
        "return_pct": float(cumulative[-1]) / capital * 100 if capital else None,
        "trades": int(sum(d["trades"] for d in daily)),
        "last_day": daily[-1]["date"] if daily else None,
    }


def _series(daily: list[dict]) -> list[dict]:
    total, out = 0.0, []
    for d in daily:
        total += d["net"]
        out.append({**d, "cumulative": total})
    return out


def performance(user_id: str, start: str | None = None, end: str | None = None, combine=()) -> dict:
    """Every strategy's daily P&L and totals in a window, and the chosen ones combined."""
    from portfolio.strategy_pnl import attribute, book, capital_on, daily_results
    from services.research_hosted_service import sandbox_money

    trades = sandbox_trades(user_id)
    account, _leverage = sandbox_money()
    today = datetime.now(IST).date()
    first = date.fromisoformat(start) if start else None
    last = date.fromisoformat(end) if end else None
    out = []
    for row in strategies(user_id, {t["strategy"] for t in trades}):
        fills = attribute(trades, row["tags"])
        every_day = daily_results(fills, today)
        daily = [
            d
            for d in every_day
            if (first is None or d["date"] >= first.isoformat())
            and (last is None or d["date"] <= last.isoformat())
        ]
        own = row["start_capital"]
        # Measured on the capital the strategy had when the window opened.
        basis = capital_on(own, every_day, first or date.min) if own else account
        held = [p for p in book(fills, {}, today)["positions"] if p["quantity"]]
        summary = {**daily_summary(daily, basis), "open_positions": len(held)}
        if own:
            summary["start_capital"] = own
            summary["capital"] = capital_on(own, every_day, today + timedelta(days=1))
            summary["capital_today"] = capital_on(own, every_day, today)
        out.append({**row, "summary": summary, "daily": _series(daily)})
    chosen = [row for row in out if row["key"] in set(combine)]
    combined = None
    if chosen:
        by_day: dict[str, dict] = defaultdict(
            lambda: {"pnl": 0.0, "charges": 0.0, "net": 0.0, "trades": 0, "unmodelled": 0}
        )
        for row in chosen:
            for d in row["daily"]:
                for field in ("pnl", "charges", "net", "trades", "unmodelled"):
                    by_day[d["date"]][field] += d[field]
        daily = [{"date": day, **by_day[day]} for day in sorted(by_day)]
        owned = [row["summary"]["start_capital"] for row in chosen if row["start_capital"]]
        combined = {
            "keys": [row["key"] for row in chosen],
            "summary": daily_summary(daily, sum(owned) if owned else account),
            "daily": _series(daily),
            "contributions": [
                {"key": row["key"], "name": row["name"], "total_pnl": row["summary"]["total_pnl"]}
                for row in chosen
            ],
        }
    return {"capital": account, "strategies": out, "combined": combined}


def strategy_capital(user_id: str, script_id: str, config: dict, day: date) -> float:
    """A Python strategy's capital at the start of ``day``: what its run is handed."""
    from portfolio.strategy_pnl import attribute, capital_on, daily_results

    trades = sandbox_trades(user_id)
    row = next(
        (
            r
            for r in strategies(user_id, {t["strategy"] for t in trades})
            if r["key"] == f"python:{script_id}"
        ),
        None,
    )
    own = start_capital(config)
    if row is None:  # it has not traded yet
        return own
    return capital_on(own, daily_results(attribute(trades, row["tags"]), day), day)


def ledger(user_id: str, key: str) -> list[dict]:
    """One strategy's fills, newest first: its own orders and its share of the square-off."""
    from portfolio.strategy_pnl import attribute

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

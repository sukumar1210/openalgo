"""
A book built from fills that happened elsewhere.

Research did not decide these orders: a Python strategy hosted at /python did,
either on live market data in the Sandbox (recorded afterwards from the
sandbox's trades) or in a backtest that replayed history to it
(``research.script_replay``). There is nothing for the engine to ask. What
Research still owns is the accounting: costs from the scenario's cost model,
round trips, positions at each day's close and equity marked bar by bar.

Driving the engine's own book with these fills, instead of a second ledger
written beside it, keeps that accounting identical to every other run, so a
hosted day, a script backtest and a strategy backtest compare like for like and
the run pages read them all the same way.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from portfolio.costs import CostSchedule
from research.dataset import Frames, normalise
from research.engine import Engine, Instance, Result
from research.strategy import Config


@dataclass(frozen=True)
class Fill:
    """One execution: ``qty`` is signed, positive bought. ``ts`` is epoch seconds."""

    ts: int
    symbol: str
    qty: int
    price: float
    reason: str = "signal"


class _Placed:
    """Stands in for a strategy whose orders were placed by someone else."""

    interval = "1m"

    def decide(self, ctx):
        return None


def fill_frames(fills: Sequence[Fill]) -> Frames:
    """Bars made of the fills alone, for days with no stored market data.

    One bar per symbol per fill minute at the fill's price, so positions are
    marked at the last price they traded at and nowhere else.
    """
    rows: dict[str, dict[int, float]] = {}
    for fill in fills:
        rows.setdefault(fill.symbol, {})[fill.ts - fill.ts % 60] = fill.price
    frames: Frames = {}
    for symbol, bars in rows.items():
        stamps = sorted(bars)
        prices = [bars[t] for t in stamps]
        frames[symbol] = normalise(
            pd.DataFrame(
                {
                    "timestamp": stamps,
                    "open": prices,
                    "high": prices,
                    "low": prices,
                    "close": prices,
                    "volume": [0.0] * len(stamps),
                }
            )
        )
    return frames


def book_from_fills(
    frames: Frames,
    config: Config,
    label: str,
    costs: CostSchedule,
    fills: Sequence[Fill],
    slippage_bps: float = 0.0,
    trade_from: int | None = None,
) -> Result:
    """The engine's result for one book that traded exactly these fills.

    Args:
        frames: Bars to mark positions on. Every traded symbol must be there;
            ``fill_frames`` supplies a symbol that has no stored bars.
        config: The book's configuration; its capital is the starting equity.
        label: How the book is named on the run's pages.
        costs: The scenario's cost schedule, charged on every fill.
        fills: What happened, in any order.
        slippage_bps: The scenario's slippage, charged as a cost.
        trade_from: Bars before this epoch second were history the script
            read; the result starts at it.
    """
    fills = sorted(fills, key=lambda f: (f.ts, f.symbol))
    config = replace(config, universe=tuple(sorted(frames)))
    engine = Engine(
        "ISOLATED",
        [Instance(0, label, _Placed(), config)],
        frames,
        costs,
        slippage_bps=slippage_bps,
        trade_from=trade_from,
    )
    first = engine.first_traded_bar()
    clock = engine.clock[first:]
    bars = len(clock)
    book_equity = np.zeros((1, bars))
    port_equity = np.zeros((1, bars))
    book_gross = np.zeros((1, bars))
    port_gross = np.zeros((1, bars))
    port_net = np.zeros((1, bars))
    pending = 0
    for n in range(bars):
        i = first + n
        upto = clock[n + 1] if n + 1 < bars else math.inf
        while pending < len(fills) and fills[pending].ts < upto:
            fill = fills[pending]
            pending += 1
            if fill.qty:
                entries = {0: [int(fill.qty), fill.reason]}
                engine._fill(i, 0, fill.symbol, entries, float(fill.price), ts=fill.ts)
        marks = {s: engine._mark(s, i) for s in engine.symbols}
        book_equity[:, n], port_equity[:, n] = engine.equity_at(marks)
        book_gross[:, n], port_gross[:, n], port_net[:, n] = engine.exposure_at(marks)
        if n + 1 == bars or engine.day[i + 1] != engine.day[i]:
            engine.snapshot_positions(i)
    return Result(
        timestamps=clock,
        book_equity=book_equity,
        portfolio_equity=port_equity,
        orders=engine.orders,
        fills=engine.fills,
        trades=engine.trades,
        books=engine.books,
        portfolio_capital=engine.portfolio_capital,
        conflicts=0,
        events=engine.events,
        positions=engine.positions,
        book_gross=book_gross,
        portfolio_gross=port_gross,
        portfolio_net=port_net,
        buying_power=engine.buying_power,
    )

"""
FirstBarLeader: the built-in reference strategy.

It exists so the platform can be tested end to end without any external
package, and it is shaped like the strategies this fork is for: one
cross-sectional decision per day over a universe, a single position, long or
short, intraday. At the first decision of each day it ranks the universe by the
return of the day's first bar, takes the symbol that moved most, and holds it in
the direction it moved until the configured exit.

It is a reference, not a research result. Nothing about it is claimed to be
profitable; it is deterministic, cheap, and exercises every path the engine
has (entries, both sides, the exit time, stops and targets when configured).
"""

from __future__ import annotations

import math

from research.strategy import Context, ist_day


class FirstBarLeader:
    interval = "5m"
    artifact_hash = None

    def __init__(self) -> None:
        self._decided_day: int | None = None

    def decide(self, ctx: Context) -> dict[str, float] | None:
        if self._decided_day == ctx.day:
            return None
        best_symbol, best_return = None, 0.0
        for symbol in ctx.symbols:
            bars = ctx.bars(symbol)
            day_start = len(bars) - 1
            while day_start > 0 and _same_day(bars.timestamp[day_start - 1], ctx.day):
                day_start -= 1
            if not len(bars) or not _same_day(bars.timestamp[day_start], ctx.day):
                continue
            first_open = bars.open[day_start]
            first_close = bars.close[day_start]
            if not (math.isfinite(first_open) and math.isfinite(first_close)) or first_open <= 0:
                continue
            move = first_close / first_open - 1.0
            if abs(move) > abs(best_return):
                best_symbol, best_return = symbol, move
        self._decided_day = ctx.day
        if best_symbol is None:
            return {}
        return {best_symbol: 1.0 if best_return > 0 else -1.0}


def _same_day(timestamp: float, day: int) -> bool:
    return ist_day(int(timestamp)) == day

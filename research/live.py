"""
The live side of the engine: one bar at a time, fills from outside.

``LiveEngine`` is the backtest ``Engine`` with two things changed and nothing
else. The clock: a live session does not hold the next bar, so where a bar sits
in the day is derived from the interval and the session close instead of read
from the data. And execution: orders the engine decides are handed out as
``OrderIntent`` objects for the Sandbox adapter to place, and come back as fills
at whatever price the sandbox gave. Books, decisions, stops and targets, the
exposure limit, netting and cost allocation are the backtest's own code, which
is the point of PRD section 42: a strategy keeps its portfolio semantics when
the execution environment changes underneath it.

``TickBarBuilder`` turns WebSocket quote ticks into bars on the session's
interval. Ticks are bucketed by the time the session received them, not by the
exchange time they carry: an illiquid symbol's last-trade time can be minutes
old, and bucketing by it would put a tick that arrived now into a bar that has
already closed. Bars built from ticks will not match a broker's own bars
exactly; that is the documented cost of the WebSocket transport.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from portfolio.costs import CostSchedule
from research.engine import Engine, EngineError, Instance
from research.strategy import IST_OFFSET_SECONDS, ist_day, ist_minute

COLUMNS = ("open", "high", "low", "close", "volume")

#: NSE and BSE cash close at 15:30; the last bar of a day is the one whose
#: successor would start at or after this minute.
SESSION_CLOSE_MINUTE = 15 * 60 + 30


@dataclass
class OrderIntent:
    """A net order a portfolio wants, before it reaches the sandbox.

    ``seq`` is the engine's sequence number reserved for this order, so the
    platform order id exists before submission and the eventual ledger order
    row carries the same number. ``entries`` is every book's share of it.
    """

    seq: int
    portfolio: int
    symbol: str
    net: int
    reference_price: float
    bar_index: int
    entries: dict[int, list] = field(default_factory=dict)

    def platform_order_id(self, run_id: int) -> str:
        return f"research:{run_id}:{self.seq}"


class LiveEngine(Engine):
    def __init__(
        self,
        mode: str,
        instances: Sequence[Instance],
        symbols: Sequence[str],
        costs: CostSchedule,
        interval_minutes: int,
        shared_capital: float | None = None,
        max_gross_leverage: float = 1.0,
        session_close_minute: int = SESSION_CLOSE_MINUTE,
    ) -> None:
        empty = pd.DataFrame({"timestamp": np.array([], dtype="int64"), **{c: [] for c in COLUMNS}})
        # Slippage is not modelled live: the sandbox's fill price already is
        # whatever the market gave, and charging a modelled slippage on top
        # would count it twice. Observed slippage is recorded separately.
        super().__init__(
            mode,
            instances,
            dict.fromkeys(symbols, empty),
            costs,
            slippage_bps=0.0,
            shared_capital=shared_capital,
            max_gross_leverage=max_gross_leverage,
        )
        self.interval_minutes = int(interval_minutes)
        self.session_close_minute = session_close_minute
        self._n = 0
        self._capacity = 0
        self._last_close: dict[str, float] = dict.fromkeys(self.symbols, math.nan)
        # (portfolio, symbol) -> {book: delta} for orders placed and not yet settled.
        self._inflight: dict[int, OrderIntent] = {}
        self._grow(512)

    # ------------------------------------------------------------------ bars

    def _grow(self, capacity: int) -> None:
        def widen(old: np.ndarray, fill) -> np.ndarray:
            arr = np.full(capacity, fill, dtype=old.dtype if old.size else type(fill))
            arr[: self._n] = old[: self._n]
            return arr

        self._clock_buf = widen(getattr(self, "_clock_buf", np.array([], dtype=np.int64)), 0)
        self._day_buf = widen(getattr(self, "_day_buf", np.array([], dtype=np.int64)), 0)
        self._minute_buf = widen(getattr(self, "_minute_buf", np.array([], dtype=np.int64)), 0)
        bufs = getattr(self, "_bufs", {s: {} for s in self.symbols})
        for symbol in self.symbols:
            for name in (*COLUMNS, "mark"):
                bufs[symbol][name] = widen(
                    bufs[symbol].get(name, np.array([], dtype=float)), np.nan
                )
        self._bufs = bufs
        self._capacity = capacity

    def _publish_views(self) -> None:
        """Expose the first n rows as read-only views, the shape Context reads."""
        n = self._n
        self.clock = self._clock_buf[:n]
        self.clock.setflags(write=False)
        self.day = self._day_buf[:n]
        self.minute = self._minute_buf[:n]
        for symbol in self.symbols:
            cols = {"timestamp": self.clock}
            for name in COLUMNS:
                view = self._bufs[symbol][name][:n]
                view.setflags(write=False)
                cols[name] = view
            self.columns[symbol] = cols
            self.marks[symbol] = self._bufs[symbol]["mark"][:n]

    def append_bar(self, ts: int, bars: Mapping[str, Sequence[float]]) -> int:
        """Add one bar for every symbol (missing symbols are NaN). Returns its index."""
        if self._n and ts <= int(self._clock_buf[self._n - 1]):
            raise EngineError("Bars must arrive in time order.")
        if self._n == self._capacity:
            self._grow(self._capacity * 2)
        i = self._n
        self._clock_buf[i] = ts
        self._day_buf[i] = ist_day(ts)
        self._minute_buf[i] = ist_minute(ts)
        for symbol in self.symbols:
            values = bars.get(symbol)
            buf = self._bufs[symbol]
            if values is not None:
                for name, value in zip(COLUMNS, values, strict=True):
                    buf[name][i] = float(value)
                if math.isfinite(buf["close"][i]):
                    self._last_close[symbol] = float(buf["close"][i])
            buf["mark"][i] = self._last_close[symbol]
        self._n += 1
        self._publish_views()
        return i

    def last_index(self) -> int:
        return self._n - 1

    def _flags(self, i: int) -> tuple[bool, bool, int | None]:
        next_minute = int(self.minute[i]) + self.interval_minutes
        last_of_day = next_minute >= self.session_close_minute
        return False, last_of_day, None if last_of_day else next_minute

    # ------------------------------------------------------------------ orders

    def _pending_qty(self, book, b: int, symbol: str) -> int:
        queued = super()._pending_qty(book, b, symbol)
        inflight = sum(
            intent.entries[b][0]
            for intent in self._inflight.values()
            if intent.portfolio == book.portfolio
            and intent.symbol == symbol
            and b in intent.entries
        )
        return queued + inflight

    def close_bar(self, i: int) -> list[OrderIntent]:
        """Decide at the close of bar ``i`` and hand back the orders to place.

        Fully crossed intents (net zero) need no market order and are settled
        here at the bar's close, exactly as a backtest settles them at its fill
        price. Everything else becomes an in-flight ``OrderIntent``.
        """
        marks = {s: self._mark(s, i) for s in self.symbols}
        try:
            self.step_decisions(i, marks)
        except Exception:
            # Half a bar's decisions are never sent: the session squares off instead.
            self._pending.clear()
            raise
        return self._intents_from_pending(i)

    def fill_ts(self, i: int) -> int:
        """When an order decided at the close of bar ``i`` fills: the next bar's start.

        The same instant a backtest fills it, at the next bar's open.
        """
        return int(self.clock[i]) + self.interval_minutes * 60

    def _intents_from_pending(self, i: int) -> list[OrderIntent]:
        marks = {s: self._mark(s, i) for s in self.symbols}
        intents = []
        for key in sorted(self._pending):
            entries = self._pending.pop(key)
            if not entries:
                continue
            portfolio, symbol = key
            net = sum(delta for delta, _ in entries.values())
            price = float(marks[symbol])
            if not net:
                if price > 0:
                    self._fill(i, portfolio, symbol, entries, price, ts=self.fill_ts(i))
                continue
            intent = OrderIntent(self._next_seq(), portfolio, symbol, net, price, i, entries)
            self._inflight[intent.seq] = intent
            intents.append(intent)
        return intents

    def square_off(self, i: int, reason: str = "square_off") -> list[OrderIntent]:
        """Orders that flatten every book, without asking any strategy.

        Used when a session is stopped, or fails safe on stale data, before its
        configured exit time. Positions already being closed by an order in
        flight are not ordered again.
        """
        for b, book in enumerate(self.books):
            held = {s: q + self._pending_qty(book, b, s) for s, q in book.qty.items()}
            targets = {s: 0 for s, q in held.items() if q}
            self._queue(book, b, i, targets, dict.fromkeys(targets, reason))
        return self._intents_from_pending(i)

    def warm(self, i: int) -> None:
        """Let each strategy see bar ``i`` without trading on its answer.

        After a restart the books come back from the ledger, but a strategy's
        own state (what it decided today, its running indicators) lives in the
        object. Replaying the session's bars through ``decide`` rebuilds it; the
        answers are discarded because the positions they led to already exist.
        """
        from research.strategy import Context

        _last_of_data, last_of_day, next_minute = self._flags(i)
        marks = {s: self._mark(s, i) for s in self.symbols}
        for b, book in enumerate(self.books):
            config = book.instance.config
            if last_of_day or self.minute[i] < config.entry_minute:
                continue
            if config.exit_minute is not None and next_minute >= config.exit_minute:
                continue
            self._ask(
                book,
                Context(
                    self.columns,
                    i,
                    int(self.clock[i]),
                    int(self.day[i]),
                    int(self.minute[i]),
                    {s: q + self._pending_qty(book, b, s) for s, q in book.qty.items()},
                    book.equity(marks),
                    config,
                ),
            )

    def apply_fill(self, intent: OrderIntent, price: float) -> None:
        """Settle an in-flight order at the sandbox's fill price."""
        self._inflight.pop(intent.seq, None)
        self._fill(
            intent.bar_index,
            intent.portfolio,
            intent.symbol,
            intent.entries,
            float(price),
            order_seq=intent.seq,
            ts=self.fill_ts(intent.bar_index),
        )

    def drop(self, intent: OrderIntent) -> None:
        """Forget an order the sandbox rejected or did not fill. The books are unchanged."""
        self._inflight.pop(intent.seq, None)

    def inflight(self) -> list[OrderIntent]:
        return [self._inflight[k] for k in sorted(self._inflight)]

    def reserve_seq(self) -> int:
        """A sequence number for a ledger event, from the same counter as orders."""
        return self._next_seq()

    def restore(self, fills: Sequence[Mapping], last_seq: int) -> None:
        """Rebuild every book from a session's recorded fills, in their order.

        Fills carry everything a book needs (quantity, price and its share of
        costs), so replaying them reproduces cash, positions, average prices and
        open round trips exactly, without asking the strategy to decide again.
        """
        by_position = {book.instance.position: book for book in self.books}
        for fill in sorted(fills, key=lambda f: f["seq"]):
            book = by_position[fill["instance"]]
            self._apply(
                book,
                fill["symbol"],
                fill["qty"],
                fill["price"],
                fill["fees"],
                fill["slippage"],
                fill["reason"],
                fill["ts"],
                fill["order_seq"],
            )
        # The replay re-emitted these rows; they are already in the ledger.
        self.fills.clear()
        self.trades.clear()
        self.orders.clear()
        self._seq = max(self._seq, last_seq)


@dataclass
class _Building:
    open: float
    high: float
    low: float
    close: float
    volume: float


class TickBarBuilder:
    """Builds bars on an interval from quote ticks, per symbol.

    ``on_tick`` is called from the feed's dispatcher thread and ``closed`` from
    the session loop, so both take the lock. Volume is the change in the day's
    cumulative volume between consecutive ticks.
    """

    def __init__(self, symbols: Sequence[str], interval_minutes: int, lock) -> None:
        self.symbols = tuple(sorted(symbols))
        self.seconds = int(interval_minutes) * 60
        self._lock = lock
        self._bars: dict[int, dict[str, _Building]] = {}
        self._last_volume: dict[str, float] = {}
        self.last_tick_at: dict[str, float] = {}

    def bucket(self, epoch: float) -> int:
        """Start of the bar holding ``epoch``, aligned to IST midnight."""
        ist = int(epoch) + IST_OFFSET_SECONDS
        return ist - ist % self.seconds - IST_OFFSET_SECONDS

    def on_tick(self, symbol: str, ltp, cumulative_volume, received_at: float) -> None:
        try:
            price = float(ltp)
        except (TypeError, ValueError):
            return
        if symbol not in self.symbols or not math.isfinite(price) or price <= 0:
            return
        start = self.bucket(received_at)
        with self._lock:
            volume = 0.0
            try:
                cumulative = float(cumulative_volume)
            except (TypeError, ValueError):
                cumulative = math.nan
            previous = self._last_volume.get(symbol)
            if math.isfinite(cumulative):
                if previous is not None and cumulative >= previous:
                    volume = cumulative - previous
                self._last_volume[symbol] = cumulative
            bars = self._bars.setdefault(start, {})
            bar = bars.get(symbol)
            if bar is None:
                bars[symbol] = _Building(price, price, price, price, volume)
            else:
                bar.high = max(bar.high, price)
                bar.low = min(bar.low, price)
                bar.close = price
                bar.volume += volume
            self.last_tick_at[symbol] = received_at

    def closed(self, now: float, grace_seconds: float) -> list[tuple[int, dict[str, tuple]]]:
        """Every bar whose interval ended at least ``grace_seconds`` ago, oldest first."""
        with self._lock:
            due = sorted(
                start for start in self._bars if start + self.seconds + grace_seconds <= now
            )
            out = []
            for start in due:
                bars = self._bars.pop(start)
                out.append(
                    (
                        start,
                        {s: (b.open, b.high, b.low, b.close, b.volume) for s, b in bars.items()},
                    )
                )
            return out

    def stale_symbols(self, now: float, after_seconds: float) -> list[str]:
        with self._lock:
            return [
                s for s in self.symbols if now - self.last_tick_at.get(s, -math.inf) > after_seconds
            ]

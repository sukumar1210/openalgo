"""
The backtest engine: one deterministic code path for single, isolated and shared runs.

Books and portfolios
--------------------

A **book** is one strategy instance's virtual account: its cash, its positions,
its fills and its round-trip trades. A **portfolio** is a group of books that
trade as one account. The three modes are nothing more than three groupings:

* single    one portfolio holding one book
* isolated  one portfolio per book, so each strategy has its own capital,
            positions, orders and costs, and shares only the bars and the clock
* shared    one portfolio holding every book: one pool of buying power, one
            exposure limit, and orders netted across strategies before they
            reach the market

Because the grouping is the only difference, an isolated run and a single run
of the same instance are the same computation, which is what lets a test assert
that one strategy's trades cannot change another's result.

Netting and attribution in shared mode
--------------------------------------

Every book still trades its own full intent. If A wants +100 RELIANCE and B
wants -100 at the same bar, both books are filled at the same reference price,
so each keeps the P&L of its own decision, and the portfolio sends no order at
all because the net is zero. The cash legs cancel exactly (+100p and -100p).
Costs are charged on the net order only, since that is all a broker would see,
and are allocated to the books whose intent the market order carried (those
trading in the direction of the net), pro rata by size. The last such book takes
the rounding remainder so the pieces sum to the order's cost exactly. That rule
is ``CONFLICT_POLICY = "net-v1"``; change it and bump the constant.

The exposure limit in shared mode scales every book's target by one common
factor when the netted portfolio would exceed it, so no strategy gets priority
by being listed first. Each reduction is counted per book and reported.

Portfolio limits
----------------

A scenario's risk policy may set a daily loss limit, a daily profit target and
a lock-profit floor per portfolio. Each bar's close judges the portfolio's P&L
for the day through ``services.risk.evaluate_aggregate``, the shared risk core,
never a second evaluator. A breach closes everything the portfolio holds and
takes no new entries until the next trading day. In isolated mode every
strategy is its own portfolio, so the limit applies to each; in shared mode to
the whole.

Analytics
---------

Beside equity the engine records, per bar, every book's gross exposure and every
portfolio's gross and net exposure (of its netted positions, which is what its
buying power carries); how many order groups it netted, what crossing inside a
portfolio saved (orders, notional and costs against each book trading alone);
and, for every pair of strategies, the bars on which they held a symbol on the
same side or on opposite sides.

Timing
------

Each bar is processed in two passes. First, orders decided at the previous
close are filled at this bar's open. Then, at this bar's close, positions are
marked, stops and targets are checked through ``services.risk`` (never a second
evaluator), the exit time is applied, and strategies decide. A decision at bar
``i`` therefore fills at bar ``i + 1``'s open: no strategy ever trades at a price
it used to decide. The one exception is a position that must be closed and has
no later bar on the same day (an intraday position on the day's last bar, or any
position on the last bar of the data); that fills at the bar's close, because
the alternative is carrying an intraday position overnight.

Orders and the ledger
---------------------

A decision becomes an order the moment it is made, in every environment: its
number is reserved then (``OrderIntent``), so ``research:<run>:<seq>`` names the
same order whether a backtest, the sandbox or a broker fills it. Intents that
cross completely inside a shared portfolio send no order; they settle at the
decision bar's close, the price both strategies decided on, in a backtest and
live alike. Alongside orders, fills and trades the engine records the facts no
other row holds (``events``): each changed answer of a strategy (``SIGNAL``),
each target it led to (``TARGET_POSITION``, or ``SQUARE_OFF`` when the platform
forced it), each order (``ORDER_INTENT``); and each open position at the close
of a trading day (``positions``). Every row shares one sequence, so the tables
interleave into one ordered history.

Known simplification: a stop is detected on the close and exits at the next
open, so a gap through the stop is filled at the gapped price and an intrabar
touch that recovers by the close is not a stop at all.
# ponytail: close-based stops; add intrabar high/low stop fills when a strategy
# with tight stops needs them.

Lifecycle hooks
---------------

A strategy's optional hooks run at fixed points of the same loop:
``initialize`` before the first bar, ``on_fill`` for each fill on its book after
the fill is fully settled and before its next decision, and ``on_session_end``
after the last bar of each trading day (``research.strategy``). Every hook runs
through ``_call``, as strategy code.

Determinism: no wall clock, symbols in sorted order and books in instance order
everywhere, and the only randomness is each instance's own seeded generator.
Identical inputs give byte-identical ledgers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType

import numpy as np

from portfolio.costs import CostSchedule
from research.dataset import Frames
from research.strategy import (
    Config,
    Context,
    FillView,
    SetupContext,
    Strategy,
    StrategyRefused,
    ist_day,
    ist_minute,
    strategy_code,
)
from services.risk import (
    AggregateRisk,
    BreachReason,
    PositionRisk,
    evaluate_aggregate,
    evaluate_position,
)

MODES = ("ISOLATED", "SHARED")

#: Portfolio limits a scenario's risk policy may set (rupees), as the shared risk
#: core's ``AggregateRisk`` fields.
PORTFOLIO_LIMITS = {
    "daily_loss_limit": "combined_stoploss",
    "daily_profit_target": "combined_target",
    "lock_profit_at": "lock_profit_at",
    "lock_profit_floor": "lock_profit_floor",
    "lock_trail_step": "lock_trail_step",
}
#: The square-off reason each portfolio breach is recorded with.
LIMIT_REASONS = {
    BreachReason.COMBINED_STOP: "daily_loss",
    BreachReason.COMBINED_TARGET: "daily_target",
    BreachReason.LOCK_PROFIT: "lock_profit",
}

#: Fills smaller than this, in rupees, are treated as exact when reconciling.
EPSILON = 1e-6


class EngineError(RuntimeError):
    """The run cannot continue. The message is written for the trader."""


@dataclass
class OrderIntent:
    """A net order a portfolio decided on, before it is filled.

    ``seq`` is reserved when the order is decided, so the platform order id
    exists before submission and the ledger order row carries the same number.
    ``entries`` is every book's share of it.
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


@dataclass
class Instance:
    position: int
    label: str
    strategy: Strategy
    config: Config
    seed: int = 0


@dataclass
class _Open:
    """A round trip in progress on one symbol."""

    side: int
    entry_ts: int
    entry_value: float = 0.0
    entry_qty: int = 0
    exit_value: float = 0.0
    exit_qty: int = 0
    max_qty: int = 0
    gross: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0


@dataclass
class Book:
    instance: Instance
    portfolio: int
    capital: float
    cash: float
    qty: dict[str, int] = field(default_factory=dict)
    avg: dict[str, float] = field(default_factory=dict)
    open_trades: dict[str, _Open] = field(default_factory=dict)
    traded_value: float = 0.0
    reductions: int = 0
    rng: np.random.Generator | None = None
    #: The strategy's last recorded answer and the SIGNAL event that recorded it.
    last_signal: dict | None = None
    signal_seq: int | None = None

    def equity(self, marks: Mapping[str, float]) -> float:
        return self.cash + sum(q * marks[s] for s, q in self.qty.items() if q)


@dataclass
class Result:
    timestamps: np.ndarray
    book_equity: np.ndarray  # shape (books, bars)
    portfolio_equity: np.ndarray  # shape (portfolios, bars)
    orders: list[dict]
    fills: list[dict]
    trades: list[dict]
    books: list[Book]
    portfolio_capital: list[float]
    conflicts: int
    events: list[dict] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    book_gross: np.ndarray | None = None  # shape (books, bars)
    portfolio_gross: np.ndarray | None = None  # shape (portfolios, bars)
    portfolio_net: np.ndarray | None = None
    buying_power: list[float] = field(default_factory=list)
    order_groups: int = 0
    netting: dict = field(default_factory=dict)
    overlap: list[dict] = field(default_factory=list)
    breaches: list[dict] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        mode: str,
        instances: Sequence[Instance],
        frames: Frames,
        costs: CostSchedule,
        slippage_bps: float = 0.0,
        shared_capital: float | None = None,
        max_gross_leverage: float = 1.0,
        portfolio_limits: Mapping[str, float] | None = None,
        trade_from: int | None = None,
    ) -> None:
        if mode not in MODES:
            raise EngineError("The scenario mode must be isolated or shared.")
        if not instances:
            raise EngineError("The scenario has no strategies.")
        missing = sorted({s for i in instances for s in i.config.universe} - set(frames))
        if missing:
            raise EngineError(f"No bars were loaded for {', '.join(missing[:5])}.")
        if not math.isfinite(slippage_bps) or slippage_bps < 0:
            raise EngineError("Slippage must be zero or more.")
        self.mode = mode
        #: Bars before this epoch second are history only: a strategy reads them
        #: through ``ctx.bars``, but none is replayed, so nothing is decided,
        #: filled or counted in the result before it.
        self.trade_from = trade_from
        self.costs = costs
        self.slip = slippage_bps / 10_000.0
        self.max_gross_leverage = max_gross_leverage
        self.symbols = sorted(frames)
        self._align(frames)

        ordered = sorted(instances, key=lambda inst: inst.position)
        if mode == "SHARED":
            capital = (
                shared_capital
                if shared_capital is not None
                else sum(i.config.capital for i in ordered)
            )
            self.portfolio_capital = [float(capital)]
            self.books = [
                Book(inst, 0, inst.config.capital, inst.config.capital) for inst in ordered
            ]
        else:
            self.portfolio_capital = [float(i.config.capital) for i in ordered]
            self.books = [
                Book(inst, n, inst.config.capital, inst.config.capital)
                for n, inst in enumerate(ordered)
            ]
        for book in self.books:
            book.rng = np.random.default_rng(book.instance.seed)
        if mode == "SHARED":
            self.buying_power = [self.portfolio_capital[0] * max_gross_leverage]
        else:
            self.buying_power = [book.instance.config.sizing_capital for book in self.books]
        limits = {
            PORTFOLIO_LIMITS[k]: float(v)
            for k, v in (portfolio_limits or {}).items()
            if k in PORTFOLIO_LIMITS and v is not None
        }
        self._limits = AggregateRisk(**limits) if limits else None
        self._aggregate = [self._limits] * len(self.portfolio_capital)
        self._halted: dict[int, str] = {}
        self._day_start = list(self.portfolio_capital)
        self.breaches: list[dict] = []
        #: Where ``ctx.record`` writes: (bar time, instance, name, values). Set
        #: by the worker; None keeps records nowhere, as in a unit test.
        self.record_sink = None
        self.order_groups = 0
        self.netting = {"orders_avoided": 0, "notional_netted": 0.0, "costs_saved": 0.0}
        self.overlap = {
            (a, b): {"same": 0, "opposite": 0}
            for a in range(len(self.books))
            for b in range(a + 1, len(self.books))
        }
        self.bars_seen = 0
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self.positions: list[dict] = []
        # Fills not yet told to their strategy's on_fill: (book, fill row).
        self._unnotified: list[tuple[Book, dict]] = []
        self.conflicts = 0
        #: When False, strategies are not asked to decide; forced exits (stops,
        #: targets, the exit time) still run. A backtest never changes it; a
        #: Sandbox session clears it while paused or while its data is stale.
        self.accept_new = True
        self._seq = 0
        # (portfolio, symbol) -> {book index: [delta, reason]}: decided this bar.
        self._pending: dict[tuple[int, str], dict[int, list]] = {}
        # (portfolio, symbol, book) -> what queued that target, for its event.
        self._targets: dict[tuple[int, str, int], dict] = {}
        # seq -> OrderIntent: ordered and not yet filled.
        self._inflight: dict[int, OrderIntent] = {}

    # ------------------------------------------------------------------ data

    def _align(self, frames: Frames) -> None:
        clock = np.unique(
            np.concatenate([f["timestamp"].to_numpy("int64") for f in frames.values()])
        )
        self.clock = clock
        self.day = np.array([ist_day(t) for t in clock], dtype=np.int64)
        self.minute = np.array([ist_minute(t) for t in clock], dtype=np.int64)
        self.columns: dict[str, dict[str, np.ndarray]] = {}
        self.marks: dict[str, np.ndarray] = {}
        for symbol in self.symbols:
            frame = frames[symbol]
            index = np.searchsorted(clock, frame["timestamp"].to_numpy("int64"))
            cols = {"timestamp": clock}
            for name in ("open", "high", "low", "close", "volume"):
                arr = np.full(len(clock), np.nan)
                arr[index] = frame[name].to_numpy("float64")
                arr.setflags(write=False)
                cols[name] = arr
            self.columns[symbol] = cols
            close = cols["close"]
            filled = np.where(np.isfinite(close), close, np.nan)
            last = np.nan
            for n in range(len(filled)):
                if math.isfinite(filled[n]):
                    last = filled[n]
                else:
                    filled[n] = last
            self.marks[symbol] = filled
        clock.setflags(write=False)

    # ------------------------------------------------------------------ run

    def first_traded_bar(self) -> int:
        """The index of the first bar the run replays; earlier bars are history."""
        if self.trade_from is None:
            return 0
        return int(np.searchsorted(self.clock, self.trade_from, "left"))

    def run(self, on_progress=None) -> Result:
        first = self.first_traded_bar()
        if first >= len(self.clock):
            raise EngineError("There are no bars on or after the backtest's first day.")
        bars = len(self.clock) - first
        book_equity = np.zeros((len(self.books), bars))
        port_equity = np.zeros((len(self.portfolio_capital), bars))
        book_gross = np.zeros((len(self.books), bars))
        port_gross = np.zeros((len(self.portfolio_capital), bars))
        port_net = np.zeros((len(self.portfolio_capital), bars))
        step = max(1, bars // 20)
        self.initialize()
        for n in range(bars):
            i = first + n
            self._fill_inflight(i, "open")
            self.notify_fills()
            marks = {s: self._mark(s, i) for s in self.symbols}
            last_of_day = self._flags(i)[1]
            self.close_bar(i)
            if last_of_day:
                # Nothing may wait for a bar that belongs to another day.
                self._fill_inflight(i, "close")
                self.notify_fills()
                self.end_day(i)
            book_equity[:, n], port_equity[:, n] = self.equity_at(marks)
            book_gross[:, n], port_gross[:, n], port_net[:, n] = self.exposure_at(marks)
            self.count_overlap()
            if on_progress and (n % step == 0 or n == bars - 1):
                on_progress((n + 1) / bars)
        return Result(
            timestamps=self.clock[first:],
            book_equity=book_equity,
            portfolio_equity=port_equity,
            orders=self.orders,
            fills=self.fills,
            trades=self.trades,
            books=self.books,
            portfolio_capital=self.portfolio_capital,
            conflicts=self.conflicts,
            events=self.events,
            positions=self.positions,
            book_gross=book_gross,
            portfolio_gross=port_gross,
            portfolio_net=port_net,
            buying_power=self.buying_power,
            order_groups=self.order_groups,
            netting=self.netting,
            overlap=self.overlap_rows(),
            breaches=self.breaches,
        )

    def _flags(self, i: int) -> tuple[bool, bool, int | None]:
        """Where bar ``i`` sits: (last of the data, last of its day, next bar's minute).

        The only thing about the clock the decision logic needs to know. A
        backtest reads it from the bars it holds; a live session, which does not
        have the next bar yet, derives it from the schedule instead
        (``research.live.LiveEngine``).
        """
        last_of_data = i == len(self.clock) - 1
        last_of_day = last_of_data or self.day[i + 1] != self.day[i]
        next_minute = None if last_of_day else int(self.minute[i + 1])
        return last_of_data, last_of_day, next_minute

    def step_decisions(self, i: int, marks: Mapping[str, float]) -> bool:
        """Every book's decision at the close of bar ``i``, queued as pending orders.

        Forced exits first (end of data, intraday exit time, stops and targets),
        then the strategy inside its window, then the shared exposure limit.
        Returns whether bar ``i`` is the last of its day.
        """
        last_of_data, last_of_day, next_minute = self._flags(i)
        halted = self._portfolio_limits(i, marks)
        for b, book in enumerate(self.books):
            config = book.instance.config
            forced: dict[str, str] = {}
            if last_of_data:
                forced = dict.fromkeys(self._held(book), "end")
            elif config.product == "MIS" and (last_of_day or next_minute >= config.exit_minute):
                forced = dict.fromkeys(self._held(book), "exit_time" if not last_of_day else "eod")
            else:
                forced = self._risk_exits(book, i)
            if book.portfolio in halted:
                for symbol in self._held(book):
                    forced.setdefault(symbol, halted[book.portfolio])
            in_window = (
                self.accept_new
                and book.portfolio not in halted
                and not last_of_data
                and not last_of_day
                and self.minute[i] >= config.entry_minute
                and (config.exit_minute is None or next_minute < config.exit_minute)
            )
            targets = self._decide(book, b, i, marks, forced, in_window)
            self._queue(book, b, i, targets, forced)
        self._apply_exposure_limit(i)
        return last_of_day

    def close_bar(self, i: int) -> list[OrderIntent]:
        """Decide at the close of bar ``i`` and turn the decisions into orders."""
        marks = {s: self._mark(s, i) for s in self.symbols}
        try:
            self.step_decisions(i, marks)
        except Exception:
            # Half a bar's decisions are never ordered.
            self._pending.clear()
            self._targets.clear()
            raise
        return self._intents_from_pending(i)

    def fill_ts(self, i: int) -> int:
        """When an order decided at the close of bar ``i`` fills: the next bar's open."""
        return int(self.clock[i + 1]) if i + 1 < len(self.clock) else int(self.clock[i])

    def _event(self, kind: str, i: int, *, payload: dict, instance=None, order_seq=None) -> int:
        seq = self._next_seq()
        self.events.append(
            {
                "seq": seq,
                "kind": kind,
                "bar_ts": int(self.clock[i]),
                "instance": instance,
                "order_seq": order_seq,
                "payload": payload,
            }
        )
        return seq

    def _signal(self, book: Book, i: int, weights: Mapping) -> None:
        """Record a strategy's answer when it differs from its last one that day."""
        answer = {}
        for symbol, weight in sorted(weights.items(), key=lambda kv: str(kv[0])):
            try:
                value = float(weight)
            except (TypeError, ValueError):
                value = None
            answer[str(symbol)] = value if value is not None and math.isfinite(value) else None
        if answer != book.last_signal:
            book.last_signal = answer
            book.signal_seq = self._event(
                "SIGNAL", i, instance=book.instance.position, payload={"weights": answer}
            )

    def _intents_from_pending(self, i: int) -> list[OrderIntent]:
        """This bar's decisions as orders, each target and order recorded.

        Fully crossed intents (net zero) need no market order and settle here
        at the bar's close. Everything else becomes an ``OrderIntent`` with its
        number reserved now.
        """
        intents = []
        for key in sorted(self._pending):
            entries = self._pending.pop(key)
            if not entries:
                continue
            portfolio, symbol = key
            targets = {}
            for b in sorted(entries):
                book = self.books[b]
                delta, reason = entries[b]
                meta = self._targets.get((portfolio, symbol, b), {})
                previous = meta.get("previous", book.qty.get(symbol, 0))
                payload = {
                    "symbol": symbol,
                    "previous": previous,
                    "target": previous + delta,
                    "reason": reason,
                }
                if meta.get("signal") is not None:
                    payload["signal"] = meta["signal"]
                if meta.get("requested") not in (None, previous + delta):
                    payload["requested"] = meta["requested"]
                kind = "TARGET_POSITION" if reason == "signal" else "SQUARE_OFF"
                position = book.instance.position
                targets[str(position)] = self._event(kind, i, instance=position, payload=payload)
            net = sum(delta for delta, _ in entries.values())
            price = float(self._mark(symbol, i))
            if not net:
                if price > 0:
                    self._fill(i, portfolio, symbol, entries, price, ts=self.fill_ts(i))
                continue
            intent = OrderIntent(self._next_seq(), portfolio, symbol, net, price, i, entries)
            self._event(
                "ORDER_INTENT",
                i,
                order_seq=intent.seq,
                payload={
                    "seq": intent.seq,
                    "portfolio": portfolio,
                    "symbol": symbol,
                    "net": net,
                    "reference_price": price,
                    "entries": {str(b): list(v) for b, v in entries.items()},
                    "instances": {
                        str(self.books[b].instance.position): v[0] for b, v in entries.items()
                    },
                    "targets": targets,
                },
            )
            self._inflight[intent.seq] = intent
            intents.append(intent)
        self._targets.clear()
        return intents

    def _portfolio_limits(self, i: int, marks: Mapping[str, float]) -> dict[int, str]:
        """Each portfolio stopped for the day, with why; judged now for the rest."""
        if self._limits is None:
            return {}
        for p, capital in enumerate(self.portfolio_capital):
            if p in self._halted:
                continue
            equity = capital + sum(
                book.equity(marks) - book.capital for book in self.books if book.portfolio == p
            )
            day_pnl = equity - self._day_start[p]
            # The core sums its two P&L arguments; the day's change in equity is
            # already both, realized and unrealized.
            decision = evaluate_aggregate(self._aggregate[p], day_pnl, 0.0)
            self._aggregate[p] = replace(
                self._aggregate[p],
                peak_pnl=decision.peak_pnl,
                trough_pnl=decision.trough_pnl,
                lock_armed=decision.lock_armed,
                lock_floor=decision.lock_floor,
            )
            if decision.breached:
                reason = LIMIT_REASONS[decision.reason]
                self._halted[p] = reason
                breach = {
                    "portfolio": p,
                    "ts": int(self.clock[i]),
                    "reason": reason,
                    "day_pnl": day_pnl,
                    "detail": decision.detail,
                }
                self.breaches.append(breach)
                self._event("SESSION_EVENT", i, payload={"event": "portfolio limit", **breach})
        return dict(self._halted)

    def _reset_limits(self, i: int) -> None:
        self._halted.clear()
        self._aggregate = [self._limits] * len(self.portfolio_capital)
        _, portfolios = self.equity_at({s: self._mark(s, i) for s in self.symbols})
        self._day_start = [float(v) for v in portfolios]

    def exposure_at(self, marks: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Each book's gross exposure, and each portfolio's gross and net, at these marks.

        A portfolio's exposure is of its netted positions: two books long and
        short the same shares hold nothing the portfolio pays for.
        """
        books = np.array(
            [sum(abs(q) * marks[s] for s, q in book.qty.items() if q) for book in self.books]
        )
        gross, net = [], []
        for p in range(len(self.portfolio_capital)):
            held: dict[str, int] = {}
            for book in self.books:
                if book.portfolio == p:
                    for symbol, qty in book.qty.items():
                        if qty:
                            held[symbol] = held.get(symbol, 0) + qty
            gross.append(sum(abs(q) * marks[s] for s, q in held.items()))
            net.append(sum(q * marks[s] for s, q in held.items()))
        return books, np.array(gross), np.array(net)

    def count_overlap(self) -> None:
        """One bar's worth of each pair of strategies holding a symbol together."""
        self.bars_seen += 1
        for (a, b), counts in self.overlap.items():
            qa, qb = self.books[a].qty, self.books[b].qty
            both = [s for s, q in qa.items() if q and qb.get(s)]
            if any((qa[s] > 0) == (qb[s] > 0) for s in both):
                counts["same"] += 1
            if any((qa[s] > 0) != (qb[s] > 0) for s in both):
                counts["opposite"] += 1

    def overlap_rows(self) -> list[dict]:
        bars = max(self.bars_seen, 1)
        return [
            {
                "a": self.books[a].instance.position,
                "b": self.books[b].instance.position,
                "same_pct": counts["same"] / bars * 100,
                "opposite_pct": counts["opposite"] / bars * 100,
            }
            for (a, b), counts in self.overlap.items()
        ]

    def equity_at(self, marks: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
        """Each book's equity and each portfolio's, at these marks."""
        books = np.array([book.equity(marks) for book in self.books])
        portfolios = np.array(
            [
                capital
                + sum(
                    books[b] - self.books[b].capital
                    for b, book in enumerate(self.books)
                    if book.portfolio == p
                )
                for p, capital in enumerate(self.portfolio_capital)
            ]
        )
        return books, portfolios

    def _mark(self, symbol: str, i: int) -> float:
        value = self.marks[symbol][i]
        return float(value) if math.isfinite(value) else 0.0

    @staticmethod
    def _held(book: Book) -> list[str]:
        return sorted(s for s, q in book.qty.items() if q)

    def _risk_exits(self, book: Book, i: int) -> dict[str, str]:
        config = book.instance.config
        if config.stop_pct is None and config.target_pct is None:
            return {}
        out = {}
        for symbol in self._held(book):
            qty, avg = book.qty[symbol], book.avg[symbol]
            close = self.columns[symbol]["close"][i]
            if not math.isfinite(close):
                continue
            sign = 1 if qty > 0 else -1
            stop = avg * (1 - sign * config.stop_pct / 100) if config.stop_pct else None
            target = avg * (1 + sign * config.target_pct / 100) if config.target_pct else None
            decision = evaluate_position(
                PositionRisk(
                    identifier=symbol,
                    side="BUY" if qty > 0 else "SELL",
                    entry_price=avg,
                    quantity=abs(qty),
                    stop_price=stop,
                    initial_stop_price=stop,
                    target_price=target,
                ),
                float(close),
            )
            if decision.breached:
                out[symbol] = "stop" if decision.reason == BreachReason.STOP else "target"
        return out

    def _decide(
        self, book: Book, b: int, i: int, marks, forced, in_window: bool
    ) -> dict[str, int] | None:
        """The book's target quantities, or None for no change."""
        config = book.instance.config
        targets = None
        if in_window:
            sink, position = self.record_sink, book.instance.position
            ctx = Context(
                self.columns,
                i,
                int(self.clock[i]),
                int(self.day[i]),
                int(self.minute[i]),
                {s: q + self._pending_qty(book, b, s) for s, q in book.qty.items()},
                book.equity(marks),
                config,
                book.rng,
                None if sink is None else lambda ts, name, values: sink(ts, position, name, values),
            )
            weights = self._ask(book, ctx)
            if weights is not None:
                self._signal(book, i, weights)
                targets = self._to_quantities(book, weights, i)
        if forced:
            targets = dict(targets) if targets is not None else self._current_targets(book, b)
            for symbol in forced:
                targets[symbol] = 0
        return targets

    @staticmethod
    def _call(book: Book, hook: str, *args):
        """One of the strategy's methods, run as strategy code. None if it has no such hook."""
        method = getattr(book.instance.strategy, hook, None)
        if method is None:
            return None
        try:
            with strategy_code():
                return method(*args)
        except StrategyRefused as exc:
            raise EngineError(
                f"{book.instance.label} tried to {exc.action}. A strategy gets its market data "
                "from the platform and only the platform places its orders, so it was refused."
            ) from exc

    @classmethod
    def _ask(cls, book: Book, ctx: Context) -> Mapping[str, float] | None:
        """The strategy's ``decide``, run as strategy code."""
        return cls._call(book, "decide", ctx)

    def initialize(self) -> None:
        """Every strategy's ``initialize``, once, before its first bar."""
        for book in self.books:
            config = book.instance.config
            self._call(
                book,
                "initialize",
                SetupContext(
                    symbols=config.universe,
                    interval=book.instance.strategy.interval,
                    config=MappingProxyType(config.to_dict()),
                    seed=book.instance.seed,
                    rng=book.rng,
                ),
            )

    def notify_fills(self) -> None:
        """Every settled fill not yet told to its strategy, in the order it happened."""
        while self._unnotified:
            book, fill = self._unnotified.pop(0)
            if getattr(book.instance.strategy, "on_fill", None) is None:
                continue
            self._call(
                book,
                "on_fill",
                FillView(
                    symbol=fill["symbol"],
                    qty=fill["qty"],
                    price=fill["price"],
                    fees=fill["fees"],
                    slippage=fill["slippage"],
                    reason=fill["reason"],
                    time=fill["ts"],
                ),
            )

    def discard_notifications(self) -> None:
        self._unnotified.clear()

    def end_day(self, i: int | None = None) -> None:
        """Close a trading day: snapshot what is held, then ``on_session_end``.

        A strategy's next answer is recorded as a new SIGNAL even when it
        repeats yesterday's, because it is a new day's decision.
        """
        if i is not None:
            self.snapshot_positions(i)
            self._reset_limits(i)
        for book in self.books:
            book.last_signal = None
            self._call(book, "on_session_end")

    def snapshot_positions(self, i: int) -> None:
        """Each open position's quantity, average price, mark and unrealized P&L at bar ``i``."""
        ts = int(self.clock[i])
        for book in self.books:
            for symbol in self._held(book):
                qty, avg, mark = book.qty[symbol], book.avg[symbol], self._mark(symbol, i)
                self.positions.append(
                    {
                        "seq": self._next_seq(),
                        "ts": ts,
                        "instance": book.instance.position,
                        "symbol": symbol,
                        "qty": qty,
                        "avg_price": avg,
                        "mark": mark,
                        "unrealized_pnl": qty * (mark - avg),
                    }
                )

    def _current_targets(self, book: Book, b: int) -> dict[str, int]:
        return {s: q + self._pending_qty(book, b, s) for s, q in book.qty.items() if q}

    def _to_quantities(self, book: Book, weights: Mapping[str, float], i: int) -> dict[str, int]:
        config = book.instance.config
        allowed = set(config.universe)
        total = 0.0
        for symbol, weight in weights.items():
            if symbol not in allowed:
                raise EngineError(
                    f"{book.instance.label} tried to trade {symbol}, which is not in its universe."
                )
            if not isinstance(weight, (int, float)) or not math.isfinite(weight):
                raise EngineError(f"{book.instance.label} returned an invalid weight for {symbol}.")
            total += abs(weight)
        if total > 1 + 1e-9:
            raise EngineError(
                f"{book.instance.label} asked for {total:.0%} of its capital in one decision; "
                "the most a strategy can use is 100%."
            )
        out = {}
        for symbol in sorted(weights):
            close = self.columns[symbol]["close"][i]
            if not math.isfinite(close) or close <= 0:
                continue  # No price this bar: keep the current holding.
            out[symbol] = int(weights[symbol] * config.sizing_capital / close)
        held = {s for s, q in book.qty.items() if q}
        for symbol in held - set(weights):
            out[symbol] = 0
        return out

    def _pending_qty(self, book: Book, b: int, symbol: str) -> int:
        """What this book has decided or ordered on ``symbol`` and not yet been filled."""
        entry = self._pending.get((book.portfolio, symbol), {}).get(b)
        inflight = sum(
            intent.entries[b][0]
            for intent in self._inflight.values()
            if intent.portfolio == book.portfolio
            and intent.symbol == symbol
            and b in intent.entries
        )
        return (entry[0] if entry else 0) + inflight

    def _queue(self, book: Book, b: int, i: int, targets, forced) -> None:
        if targets is None:
            return
        for symbol in sorted(targets):
            current = book.qty.get(symbol, 0) + self._pending_qty(book, b, symbol)
            delta = targets[symbol] - current
            if delta == 0:
                continue
            reason = forced.get(symbol, "signal")
            slot = self._pending.setdefault((book.portfolio, symbol), {})
            previous = slot.get(b, [0, reason])[0]
            slot[b] = [previous + delta, reason]
            meta = self._targets.setdefault(
                (book.portfolio, symbol, b), {"previous": current - previous}
            )
            meta["requested"] = targets[symbol]
            meta["signal"] = book.signal_seq if reason == "signal" else None

    def _apply_exposure_limit(self, i: int) -> None:
        """Shared mode only: keep the netted portfolio within its buying power.

        When the portfolio's net exposure after this bar's orders would exceed
        ``shared capital * max_gross_leverage``, every book's new target is
        scaled by one common factor, so no strategy wins capital by being listed
        first. Isolated books have no portfolio limit by definition; their only
        limit is their own capital, which ``_to_quantities`` already enforces.
        """
        if self.mode != "SHARED":
            return
        pending = {s for (_, s) in self._pending}
        if not pending:
            return
        net: dict[str, int] = {}
        for b, book in enumerate(self.books):
            for symbol in set(book.qty) | pending:
                net[symbol] = (
                    net.get(symbol, 0)
                    + book.qty.get(symbol, 0)
                    + self._pending_qty(book, b, symbol)
                )
        gross = sum(abs(q) * self._mark(s, i) for s, q in net.items())
        limit = self.portfolio_capital[0] * self.max_gross_leverage
        if gross <= limit + EPSILON:
            return
        # ponytail: scales only the targets that changed this bar, so a book
        # whose held position grew in value can leave the portfolio slightly over
        # the limit until its next order. Rebalance held positions too if a
        # strategy runs long enough for that drift to matter.
        factor = limit / gross
        for symbol in sorted(pending):
            slot = self._pending.get((0, symbol), {})
            for b in sorted(slot):
                book = self.books[b]
                held = book.qty.get(symbol, 0)
                target = held + slot[b][0]
                scaled = int(target * factor)
                if scaled == target:
                    continue
                book.reductions += 1
                if scaled == held:
                    del slot[b]
                else:
                    slot[b][0] = scaled - held

    # ------------------------------------------------------------------ fills

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _fill_inflight(self, i: int, at: str) -> None:
        """The simulated broker: fill every order in flight at bar ``i``'s open or close."""
        for seq in sorted(self._inflight):
            intent = self._inflight[seq]
            price = self.columns[intent.symbol][at][i]
            if not math.isfinite(price) or price <= 0:
                if at == "close":
                    price = self.marks[intent.symbol][i]
                if not math.isfinite(price) or price <= 0:
                    continue  # ponytail: waits for the next priced bar; no expiry.
            del self._inflight[seq]
            self._fill(i, intent.portfolio, intent.symbol, intent.entries, float(price), seq)

    def _fill(
        self,
        i: int,
        portfolio: int,
        symbol: str,
        entries,
        price: float,
        order_seq: int | None = None,
        ts: int | None = None,
    ) -> None:
        """Net the books' entries, charge the net order, and fill every book.

        ``order_seq`` is the number reserved when the order was decided; a
        fully crossed intent has none. ``ts`` defaults to bar ``i``'s time.
        """
        ts = int(self.clock[i]) if ts is None else int(ts)
        reserved = order_seq
        ordered = sorted(entries.items())
        net = sum(delta for _, (delta, _) in ordered)
        if any(d > 0 for _, (d, _) in ordered) and any(d < 0 for _, (d, _) in ordered):
            self.conflicts += 1
        self.order_groups += 1
        order_seq = None
        fees = slippage = 0.0
        if net:
            notional = abs(net) * price
            buy, sell = (notional, 0.0) if net > 0 else (0.0, notional)
            breakdown = self.costs.breakdown(buy, sell, orders=1)
            fees = breakdown["total"] - breakdown["slippage"]
            slippage = notional * self.slip
            order_seq = reserved if reserved is not None else self._next_seq()
            self.orders.append(
                {
                    "seq": order_seq,
                    "ts": ts,
                    "portfolio": portfolio,
                    "symbol": symbol,
                    "qty": net,
                    "price": price,
                    "fees": fees,
                    "slippage": slippage,
                }
            )
        if len(ordered) > 1:
            self._count_netting(ordered, net, price, fees + slippage)
        carriers = [(b, d) for b, (d, _) in ordered if net and (d > 0) == (net > 0)]
        carried = sum(abs(d) for _, d in carriers)
        fee_left, slip_left = fees, slippage
        shares: dict[int, tuple[float, float]] = {}
        for n, (b, d) in enumerate(carriers):
            if n == len(carriers) - 1:
                shares[b] = (fee_left, slip_left)
            else:
                part = abs(d) / carried
                shares[b] = (fees * part, slippage * part)
                fee_left -= shares[b][0]
                slip_left -= shares[b][1]
        for b, (delta, reason) in ordered:
            fee, slip = shares.get(b, (0.0, 0.0))
            self._apply(
                self.books[b],
                symbol,
                delta,
                price,
                fee,
                slip,
                reason,
                ts,
                order_seq if b in shares else None,
            )

    def _count_netting(self, ordered, net: int, price: float, charged: float) -> None:
        """What one netted group saved against each book sending its own order."""
        alone = 0.0
        for _, (delta, _reason) in ordered:
            notional = abs(delta) * price
            buy, sell = (notional, 0.0) if delta > 0 else (0.0, notional)
            breakdown = self.costs.breakdown(buy, sell, orders=1)
            alone += breakdown["total"] - breakdown["slippage"] + notional * self.slip
        gross_qty = sum(abs(delta) for _, (delta, _reason) in ordered)
        self.netting["orders_avoided"] += len(ordered) - (1 if net else 0)
        self.netting["notional_netted"] += (gross_qty - abs(net)) * price
        self.netting["costs_saved"] += alone - charged

    def _apply(self, book, symbol, delta, price, fee, slip, reason, ts, order_seq) -> None:
        fill = {
            "seq": self._next_seq(),
            "order_seq": order_seq,
            "instance": book.instance.position,
            "ts": ts,
            "symbol": symbol,
            "qty": delta,
            "price": price,
            "fees": fee,
            "slippage": slip,
            "reason": reason,
        }
        self.fills.append(fill)
        self._unnotified.append((book, fill))
        book.cash -= delta * price + fee + slip
        book.traded_value += abs(delta) * price
        before = book.qty.get(symbol, 0)
        after = before + delta
        if before and after and (before > 0) != (after > 0):
            # A flip is a close and an open; split its costs by size.
            closing = -before
            part = abs(closing) / abs(delta)
            self._trade_leg(book, symbol, closing, price, fee * part, slip * part, ts)
            self._trade_leg(book, symbol, after, price, fee - fee * part, slip - slip * part, ts)
        else:
            self._trade_leg(book, symbol, delta, price, fee, slip, ts)

    def _trade_leg(self, book, symbol, delta, price, fee, slip, ts) -> None:
        before = book.qty.get(symbol, 0)
        after = before + delta
        trade = book.open_trades.get(symbol)
        if before == 0:
            trade = _Open(side=1 if delta > 0 else -1, entry_ts=ts)
            book.open_trades[symbol] = trade
            book.avg[symbol] = price
        trade.fees += fee
        trade.slippage += slip
        if abs(after) > abs(before):
            added = abs(delta)
            book.avg[symbol] = (book.avg[symbol] * abs(before) + price * added) / abs(after)
            trade.entry_value += price * added
            trade.entry_qty += added
            trade.max_qty = max(trade.max_qty, abs(after))
        else:
            closed = abs(delta)
            trade.gross += closed * (price - book.avg[symbol]) * trade.side
            trade.exit_value += price * closed
            trade.exit_qty += closed
        book.qty[symbol] = after
        if after == 0:
            del book.open_trades[symbol]
            book.avg.pop(symbol, None)
            self.trades.append(
                {
                    "seq": self._next_seq(),
                    "instance": book.instance.position,
                    "symbol": symbol,
                    "side": "LONG" if trade.side > 0 else "SHORT",
                    "qty": trade.max_qty,
                    "entry_ts": trade.entry_ts,
                    "entry_price": trade.entry_value / trade.entry_qty,
                    "exit_ts": ts,
                    "exit_price": trade.exit_value / trade.exit_qty,
                    "gross_pnl": trade.gross,
                    "fees": trade.fees,
                    "slippage": trade.slippage,
                    "net_pnl": trade.gross - trade.fees - trade.slippage,
                }
            )

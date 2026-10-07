"""
The live engine and the tick-to-bar builder (PRD Phase 8.2, 8.3, 8.6, and the
engine half of UAT PAPER-011).

The parity tests are the reason ``LiveEngine`` is a subclass of the backtest
engine rather than a second implementation: the same bars fed one at a time,
with each order filled at the price a backtest would have used, must produce
the same orders, fills, trades and equity. What remains different in a real
session is the market, not the code: the sandbox's fill price and bars built
from ticks.
"""

import threading

import numpy as np
import pytest
from test_research_engine import epoch, make_frames

from portfolio.costs import india_intraday
from research.engine import Engine, Instance
from research.live import LiveEngine, TickBarBuilder
from research.strategy import IST_OFFSET_SECONDS, Config


class RandomStrategy:
    interval = "5m"

    def __init__(self, seed):
        self.rng = np.random.default_rng(seed)

    def decide(self, ctx):
        r = self.rng.random()
        if r < 0.85:
            return None
        a, b = self.rng.uniform(-0.49, 0.49, 2)
        return {"AAA": float(a), "BBB": float(b)} if r < 0.97 else {}


def configs(seed):
    return [
        Config(
            capital=100_000,
            universe=("AAA", "BBB"),
            entry_time="09:15",
            exit_time="15:10",
            stop_pct=1.0 if seed % 2 else None,
        ),
        Config(
            capital=70_000,
            leverage=2.0,
            universe=("AAA", "BBB"),
            entry_time="09:30",
            exit_time="15:20" if seed % 3 == 0 else "15:10",
        ),
    ]


def drive_live(engine: LiveEngine, frames):
    """Feed bars one by one; fill each order at the next bar's open, or at the
    close when no later bar exists that day, which is where a backtest fills."""
    clock = frames["AAA"]["timestamp"].tolist()
    rows = {s: f.set_index("timestamp") for s, f in frames.items()}
    pending = []
    for ts in clock:
        bars = {
            s: tuple(rows[s].loc[ts, c] for c in ("open", "high", "low", "close", "volume"))
            for s in frames
        }
        i = engine.append_bar(ts, bars)
        for intent in pending:
            engine.apply_fill(intent, bars[intent.symbol][0])
        pending = engine.close_bar(i)
        if engine._flags(i)[1]:  # last bar of the day
            for intent in pending:
                engine.apply_fill(intent, bars[intent.symbol][3])
            pending = []


def normalised(result):
    """Orders, fills and trades with sequence numbers replaced by position.

    A live session reserves an order's number when it decides and a backtest
    numbers it when it fills, so the raw numbers differ by design. What must
    match is everything else, including which order each fill belongs to.
    """
    order_index = {o["seq"]: n for n, o in enumerate(result.orders)}
    orders = [{k: v for k, v in o.items() if k != "seq"} for o in result.orders]
    fills = [
        {
            **{k: v for k, v in f.items() if k not in ("seq", "order_seq")},
            "order": None if f["order_seq"] is None else order_index[f["order_seq"]],
        }
        for f in result.fills
    ]
    trades = [{k: v for k, v in t.items() if k != "seq"} for t in result.trades]
    return orders, fills, trades


@pytest.mark.parametrize("mode", ["ISOLATED", "SHARED"])
@pytest.mark.parametrize("seed", range(12))
def test_live_engine_matches_the_backtest_engine_on_the_same_bars(mode, seed):
    rng = np.random.default_rng(seed)
    walk_a = np.abs(100 + np.cumsum(rng.normal(0, 0.6, 225))) + 1
    walk_b = np.abs(50 + np.cumsum(rng.normal(0, 0.4, 225))) + 1
    frames = make_frames(
        {"AAA": list(walk_a), "BBB": list(walk_b)}, days=("2024-01-01", "2024-01-02", "2024-01-03")
    )
    costs = india_intraday("NSE")
    shared = 90_000 if seed % 4 == 0 else None

    backtest = Engine(
        mode,
        [
            Instance(n, f"S{n}", RandomStrategy(seed * 10 + n), c)
            for n, c in enumerate(configs(seed))
        ],
        frames,
        costs,
        shared_capital=shared,
    ).run()
    live = LiveEngine(
        mode,
        [
            Instance(n, f"S{n}", RandomStrategy(seed * 10 + n), c)
            for n, c in enumerate(configs(seed))
        ],
        ["AAA", "BBB"],
        costs,
        interval_minutes=5,
        shared_capital=shared,
    )
    drive_live(live, frames)

    assert normalised(live) == normalised(backtest)
    assert live.conflicts == backtest.conflicts
    final_books, _ = live.equity_at({s: live._mark(s, live.last_index()) for s in live.symbols})
    np.testing.assert_allclose(final_books, backtest.book_equity[:, -1], rtol=0, atol=1e-9)


def test_an_order_in_flight_is_not_ordered_twice():
    frames = make_frames({"AAA": [100.0] * 75, "BBB": [50.0] * 75})

    class Always:
        interval = "5m"

        def decide(self, ctx):
            return {"AAA": 1.0}

    live = LiveEngine(
        "ISOLATED",
        [Instance(0, "A", Always(), Config(capital=100_000, universe=("AAA", "BBB")))],
        ["AAA", "BBB"],
        india_intraday("NSE"),
        interval_minutes=5,
    )
    stamps = frames["AAA"]["timestamp"].tolist()
    first = live.close_bar(live.append_bar(stamps[0], {"AAA": (100, 100, 100, 100, 1)}))
    second = live.close_bar(live.append_bar(stamps[1], {"AAA": (100, 100, 100, 100, 1)}))

    assert [(o.symbol, o.net) for o in first] == [("AAA", 1000)]
    assert second == [], "the unfilled order must count as already on its way"
    live.drop(first[0])
    third = live.close_bar(live.append_bar(stamps[2], {"AAA": (100, 100, 100, 100, 1)}))
    assert [(o.symbol, o.net) for o in third] == [("AAA", 1000)]
    assert live.books[0].qty.get("AAA", 0) == 0, "a dropped order changes no book"


def test_restore_rebuilds_books_from_recorded_fills():
    rng = np.random.default_rng(3)
    frames = make_frames({"AAA": list(100 + np.cumsum(rng.normal(0, 0.5, 75))), "BBB": [50.0] * 75})

    def build(strategy_seed):
        return LiveEngine(
            "SHARED",
            [
                Instance(n, f"S{n}", RandomStrategy(strategy_seed + n), c)
                for n, c in enumerate(configs(1))
            ],
            ["AAA", "BBB"],
            india_intraday("NSE"),
            interval_minutes=5,
        )

    original = build(40)
    drive_live(original, frames)
    recorded = [dict(f) for f in original.fills]

    restored = build(999)  # a different strategy: restore must not need it
    restored.restore(recorded, last_seq=original._seq)

    for a, b in zip(original.books, restored.books, strict=True):
        assert a.cash == pytest.approx(b.cash, abs=1e-9)
        assert {s: q for s, q in a.qty.items() if q} == {s: q for s, q in b.qty.items() if q}
    assert restored._seq == original._seq
    assert restored.fills == [] and restored.trades == []


def _at(hhmm, second=0):
    return epoch("2024-01-01", hhmm) + second


def test_ticks_become_bars_on_the_interval():
    builder = TickBarBuilder(["AAA"], 5, threading.Lock())
    builder.on_tick("AAA", 100.0, 1_000, _at("09:15", 1))
    builder.on_tick("AAA", 103.0, 1_300, _at("09:16"))
    builder.on_tick("AAA", 99.0, 1_350, _at("09:19", 59))
    builder.on_tick("AAA", 101.0, 1_400, _at("09:20", 1))

    assert builder.closed(_at("09:20", 1), grace_seconds=2) == []
    closed = builder.closed(_at("09:20", 2), grace_seconds=2)

    assert closed == [(_at("09:15"), {"AAA": (100.0, 103.0, 99.0, 99.0, 350.0)})]
    assert builder.bucket(_at("09:24", 59)) == _at("09:20")


def test_bad_ticks_are_ignored_and_staleness_is_per_symbol():
    builder = TickBarBuilder(["AAA", "BBB"], 1, threading.Lock())
    for bad in (None, "x", 0, -5, float("nan")):
        builder.on_tick("AAA", bad, 10, _at("09:15"))
    builder.on_tick("ZZZ", 100.0, 10, _at("09:15"))
    builder.on_tick("BBB", 50.0, 10, _at("09:15", 30))

    assert builder.closed(_at("09:20"), grace_seconds=0) == [
        (_at("09:15"), {"BBB": (50, 50, 50, 50, 0)})
    ]
    assert builder.stale_symbols(_at("09:16"), after_seconds=60) == ["AAA"]


def test_bars_align_to_ist_midnight_not_utc():
    builder = TickBarBuilder(["AAA"], 15, threading.Lock())
    start = builder.bucket(_at("09:29", 30))
    assert (start + IST_OFFSET_SECONDS) % 86_400 == (9 * 60 + 15) * 60

"""
The backtest engine (PRD UAT phases 3, 4, 8 and the determinism rule).

Every test here runs the engine on synthetic bars with scripted strategies, so
each expected number is derived from the fixture rather than from a previous
run of the engine. A threshold or a snapshot of an earlier output could not tell
the right answer from a plausible wrong one; a value computed from the fixture
can.
"""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from portfolio.costs import CostSchedule, india_intraday
from research.engine import Engine, EngineError, Instance
from research.strategy import IST_OFFSET_SECONDS, Config, instance_seed

FREE = CostSchedule(name="free")
BARS_PER_DAY = 75  # 09:15 to 15:25 IST, 5-minute bars


def epoch(day: str, hhmm: str) -> int:
    local = datetime.fromisoformat(f"{day}T{hhmm}:00")
    return int((local - datetime(1970, 1, 1)).total_seconds()) - IST_OFFSET_SECONDS


def make_frames(prices: dict[str, list[float]], days=("2024-01-01",)) -> dict[str, pd.DataFrame]:
    """Bars whose open is the previous close and whose close is the listed price."""
    stamps = []
    for day in days:
        start = epoch(day, "09:15")
        stamps += [start + 300 * n for n in range(BARS_PER_DAY)]
    frames = {}
    for symbol, closes in prices.items():
        closes = list(closes) + [closes[-1]] * (len(stamps) - len(closes))
        opens = [closes[0], *closes[:-1]]
        frames[symbol] = pd.DataFrame(
            {
                "timestamp": stamps,
                "open": opens,
                "high": [max(o, c) for o, c in zip(opens, closes, strict=True)],
                "low": [min(o, c) for o, c in zip(opens, closes, strict=True)],
                "close": closes,
                "volume": [1000.0] * len(stamps),
            }
        )
    return frames


class Scripted:
    """Returns a fixed target at given bar numbers of the day, nothing otherwise."""

    interval = "5m"

    def __init__(self, plan: dict[int, dict[str, float]]):
        self.plan = plan
        self.calls = 0

    def decide(self, ctx):
        self.calls += 1
        bar = (ctx.minute - (9 * 60 + 15)) // 5
        return self.plan.get(bar)


def config(**overrides) -> Config:
    base = {
        "capital": 100_000,
        "universe": ("AAA", "BBB"),
        "entry_time": "09:15",
        "exit_time": "15:10",
    }
    base.update(overrides)
    return Config(**base)


def run(mode, instances, frames, costs=FREE, **kwargs):
    return Engine(mode, instances, frames, costs, **kwargs).run()


def reconcile(result):
    """Per book: final equity - capital must equal the sum of its trades' net P&L."""
    for b, book in enumerate(result.books):
        trades = [t for t in result.trades if t["instance"] == book.instance.position]
        net = sum(t["gross_pnl"] - t["fees"] - t["slippage"] for t in trades)
        assert result.book_equity[b, -1] - book.capital == pytest.approx(net, abs=1e-6)
        assert not book.open_trades, "every position must be closed by the end of the data"


def test_a_buy_fills_at_the_next_open_and_exits_at_the_exit_time():
    prices = {"AAA": [100.0] * 5 + [110.0] * 70, "BBB": [50.0] * 75}
    inst = Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config())

    result = run("ISOLATED", [inst], make_frames(prices))

    entry, exit_ = result.fills
    assert entry["qty"] == 1000 and entry["price"] == 100.0 and entry["reason"] == "signal"
    # The bar starting 15:10 is the first at the exit time; the order is decided
    # at the close of the 15:05 bar and fills at the 15:10 bar's open.
    assert exit_["qty"] == -1000 and exit_["reason"] == "exit_time"
    assert exit_["ts"] == epoch("2024-01-01", "15:10")
    assert result.trades[0]["gross_pnl"] == pytest.approx(1000 * 10.0)
    reconcile(result)


def test_isolated_books_cannot_affect_each_other():
    prices = {"AAA": [100.0 + n for n in range(75)], "BBB": [50.0] * 75}
    a_alone = run(
        "ISOLATED", [Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config())], make_frames(prices)
    )

    both = run(
        "ISOLATED",
        [
            Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config()),
            Instance(1, "B", Scripted({0: {"AAA": -1.0}, 3: {"BBB": 1.0}}), config(capital=50_000)),
        ],
        make_frames(prices),
    )

    np.testing.assert_array_equal(both.book_equity[0], a_alone.book_equity[0])
    a_fills = [f for f in both.fills if f["instance"] == 0]
    assert [(f["qty"], f["price"]) for f in a_fills] == [
        (f["qty"], f["price"]) for f in a_alone.fills
    ]
    # Two portfolios, each with its own capital.
    assert both.portfolio_capital == [100_000.0, 50_000.0]
    assert both.conflicts == 0
    reconcile(both)


def test_shared_mode_nets_opposite_intents_and_keeps_both_attributions():
    prices = {"AAA": [100.0] * 2 + [104.0] * 73, "BBB": [50.0] * 75}
    a = Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config())
    b = Instance(1, "B", Scripted({0: {"AAA": -1.0}}), config())

    result = run("SHARED", [a, b], make_frames(prices), costs=india_intraday("NSE"), slippage_bps=5)

    # +1000 and -1000 net to nothing: no market order, no cost, both books filled.
    assert result.orders == []
    entries = [f for f in result.fills if f["reason"] == "signal"]
    assert sorted(f["qty"] for f in entries) == [-1000, 1000]
    assert all(f["order_seq"] is None and f["fees"] == 0 and f["slippage"] == 0 for f in entries)
    assert result.conflicts == 2  # the entry and the exit both crossed internally
    pnl = {
        b.instance.label: result.book_equity[n, -1] - b.capital for n, b in enumerate(result.books)
    }
    assert pnl["A"] == pytest.approx(4000.0)
    assert pnl["B"] == pytest.approx(-4000.0)
    assert result.portfolio_equity[0, -1] == pytest.approx(200_000.0)
    reconcile(result)


def test_shared_costs_go_to_the_books_that_carried_the_net_order():
    prices = {"AAA": [100.0] * 75, "BBB": [50.0] * 75}
    a = Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config())
    b = Instance(1, "B", Scripted({0: {"AAA": -0.5}}), config())
    costs = india_intraday("NSE")

    result = run("SHARED", [a, b], make_frames(prices), costs=costs, slippage_bps=10)

    entry_order = result.orders[0]
    assert entry_order["qty"] == 500
    expected = costs.breakdown(500 * 100.0, 0.0, orders=1)
    assert entry_order["fees"] == pytest.approx(expected["total"])
    assert entry_order["slippage"] == pytest.approx(500 * 100.0 * 0.001)
    fills = {f["instance"]: f for f in result.fills if f["reason"] == "signal"}
    assert fills[0]["fees"] == pytest.approx(entry_order["fees"])
    assert fills[1]["fees"] == 0.0 and fills[1]["order_seq"] is None
    reconcile(result)


def test_shared_exposure_limit_scales_every_book_by_one_factor():
    prices = {"AAA": [100.0] * 75, "BBB": [100.0] * 75}
    a = Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config())
    b = Instance(1, "B", Scripted({0: {"BBB": 1.0}}), config())

    result = run("SHARED", [a, b], make_frames(prices), shared_capital=100_000)

    entries = sorted((f["symbol"], f["qty"]) for f in result.fills if f["reason"] == "signal")
    assert entries == [("AAA", 500), ("BBB", 500)]
    assert [book.reductions for book in result.books] == [1, 1]
    reconcile(result)


def test_identical_inputs_give_identical_ledgers():
    rng = np.random.default_rng(7)
    walk = 100 + np.cumsum(rng.normal(0, 0.5, 150))
    prices = {"AAA": list(walk), "BBB": list(walk[::-1])}
    frames = make_frames(prices, days=("2024-01-01", "2024-01-02"))

    def once():
        insts = [
            Instance(0, "A", Scripted({0: {"AAA": 1.0}, 20: {"BBB": -1.0}}), config(stop_pct=0.5)),
            Instance(1, "B", Scripted({5: {"AAA": -0.5, "BBB": 0.5}}), config()),
        ]
        return run("SHARED", insts, frames, costs=india_intraday("NSE"), slippage_bps=3)

    first, second = once(), once()
    assert first.fills == second.fills
    assert first.trades == second.trades
    assert first.orders == second.orders
    assert first.events == second.events
    assert first.positions == second.positions
    np.testing.assert_array_equal(first.book_equity, second.book_equity)
    reconcile(first)


def test_a_stop_is_decided_by_the_shared_risk_core_and_exits_next_open():
    prices = {"AAA": [100.0, 100.0, 98.0, 97.0] + [97.0] * 71, "BBB": [50.0] * 75}
    inst = Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config(stop_pct=1.5))

    result = run("ISOLATED", [inst], make_frames(prices))

    exit_ = [f for f in result.fills if f["qty"] < 0][0]
    assert exit_["reason"] == "stop"
    # Breached on the close of bar 2 (98 <= 98.5), filled at bar 3's open (98).
    assert exit_["price"] == 98.0
    reconcile(result)


def test_flipping_a_position_is_a_close_and_an_open():
    prices = {"AAA": [100.0, 100.0, 110.0, 110.0, 100.0] + [100.0] * 70, "BBB": [50.0] * 75}
    inst = Instance(0, "A", Scripted({0: {"AAA": 0.5}, 2: {"AAA": -0.5}}), config())

    result = run("ISOLATED", [inst], make_frames(prices), costs=india_intraday("NSE"))

    assert [t["side"] for t in result.trades] == ["LONG", "SHORT"]
    assert result.trades[0]["gross_pnl"] == pytest.approx(500 * 10.0)
    assert result.trades[1]["gross_pnl"] == pytest.approx(454 * 10.0)
    reconcile(result)


def test_an_intraday_position_never_survives_the_night():
    prices = {"AAA": [100.0] * 75, "BBB": [50.0] * 75}
    inst = Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config(exit_time="15:30"))

    result = run("ISOLATED", [inst], make_frames(prices, days=("2024-01-01", "2024-01-02")))

    exits = [f for f in result.fills if f["qty"] < 0]
    # The second night is also the end of the data, which has its own reason.
    assert [f["reason"] for f in exits] == ["eod", "end"]
    assert exits[0]["ts"] == epoch("2024-01-01", "15:25")


def test_a_strategy_cannot_trade_outside_its_universe_or_over_its_capital():
    prices = {"AAA": [100.0] * 75, "BBB": [50.0] * 75}
    outside = Instance(0, "A", Scripted({0: {"CCC": 1.0}}), config(universe=("AAA",)))
    with pytest.raises(EngineError, match="not in its universe"):
        run("ISOLATED", [outside], make_frames(prices))

    greedy = Instance(0, "A", Scripted({0: {"AAA": 0.8, "BBB": -0.8}}), config())
    with pytest.raises(EngineError, match="100%"):
        run("ISOLATED", [greedy], make_frames(prices))


def test_the_strategy_cannot_see_future_bars_or_write_to_them():
    seen = []

    class Peeker:
        interval = "5m"

        def decide(self, ctx):
            bars = ctx.bars("AAA")
            seen.append((len(bars), bars.timestamp[-1] == ctx.time))
            with pytest.raises(ValueError):
                bars.close[-1] = 0.0
            return None

    prices = {"AAA": [100.0] * 75, "BBB": [50.0] * 75}
    run("ISOLATED", [Instance(0, "A", Peeker(), config())], make_frames(prices))

    assert seen[0] == (1, True)
    assert all(ok for _, ok in seen)
    assert [n for n, _ in seen] == list(range(1, len(seen) + 1))


class Recorder:
    """Goes half long AAA at 09:15 each day; writes down every hook call."""

    interval = "5m"

    def __init__(self):
        self.log = []

    def initialize(self, setup):
        self.log.append(("initialize", setup.seed))

    def decide(self, ctx):
        self.log.append(("decide", ctx.time, ctx.position("AAA")))
        return {"AAA": 0.5} if ctx.minute == 9 * 60 + 15 else None

    def on_fill(self, fill):
        self.log.append(("fill", fill.time, fill.qty, fill.reason))

    def on_session_end(self):
        self.log.append(("end",))


def test_hooks_run_at_fixed_points_of_each_day():
    days = ("2024-01-01", "2024-01-02")
    frames = make_frames({"AAA": [100.0] * 75, "BBB": [50.0] * 75}, days=days)
    rec = Recorder()

    run("ISOLATED", [Instance(0, "R", rec, config(), seed=42)], frames)

    kinds = [e[0] for e in rec.log]
    assert rec.log[0] == ("initialize", 42) and kinds.count("initialize") == 1
    assert kinds.count("end") == 2, "once per trading day"
    entry = kinds.index("fill")
    assert rec.log[entry] == ("fill", epoch(days[0], "09:20"), 500, "signal")
    assert rec.log[entry + 1] == ("decide", epoch(days[0], "09:20"), 500), "told before it decides"
    first_end = kinds.index("end")
    assert rec.log[first_end - 1] == ("fill", epoch(days[0], "15:10"), -500, "exit_time")


def test_each_instance_draws_from_its_own_seeded_generator():
    class Draws:
        interval = "5m"

        def __init__(self):
            self.draws = []

        def initialize(self, setup):
            self.draws.append(setup.rng.random())

        def decide(self, ctx):
            self.draws.append(ctx.rng.random())

    def draws(seed):
        strategy = Draws()
        frames = make_frames({"AAA": [100.0] * 75, "BBB": [50.0] * 75})
        run("ISOLATED", [Instance(0, "D", strategy, config(), seed=seed)], frames)
        return strategy.draws

    assert draws(7) == draws(7)
    assert draws(7) != draws(8)
    assert instance_seed("v", "c", 0) == instance_seed("v", "c", 0)
    assert instance_seed("v", "c", 0) != instance_seed("v", "c", 1)


def test_the_ledger_records_each_decision_target_and_order_in_order():
    """Phase 5: SIGNAL, then TARGET_POSITION, then ORDER_INTENT, then that order's fill."""
    days = ("2024-01-01", "2024-01-02")
    frames = make_frames({"AAA": [100.0] * 75, "BBB": [50.0] * 75}, days=days)

    result = run("ISOLATED", [Instance(0, "R", Recorder(), config())], frames)

    signal, target, intent = result.events[:3]
    assert signal["kind"] == "SIGNAL" and signal["payload"] == {"weights": {"AAA": 0.5}}
    assert target["kind"] == "TARGET_POSITION"
    assert target["payload"] == {
        "symbol": "AAA", "previous": 0, "target": 500, "reason": "signal", "signal": signal["seq"]
    }  # fmt: skip
    assert intent["kind"] == "ORDER_INTENT" and intent["payload"]["targets"] == {"0": target["seq"]}
    assert intent["order_seq"] == intent["payload"]["seq"] == result.orders[0]["seq"]
    entry = result.fills[0]
    assert entry["order_seq"] == intent["order_seq"]
    assert signal["seq"] < target["seq"] < intent["seq"] < entry["seq"]
    kinds = [e["kind"] for e in result.events]
    assert kinds.count("SIGNAL") == 2, "a new day's answer is recorded even when it repeats"
    squares = [e["payload"]["reason"] for e in result.events if e["kind"] == "SQUARE_OFF"]
    assert squares == ["exit_time", "exit_time"]
    rows = (*result.events, *result.orders, *result.fills, *result.trades, *result.positions)
    seqs = [r["seq"] for r in rows]
    assert len(seqs) == len(set(seqs)), "one sequence across every ledger table"


def test_positions_held_overnight_are_valued_at_each_day_close():
    days = ("2024-01-01", "2024-01-02")
    prices = {"AAA": [100.0] * 74 + [110.0] * 76, "BBB": [50.0] * 150}
    hold = Instance(0, "C", Scripted({0: {"AAA": 1.0}}), config(product="CNC", exit_time=None))

    result = run("ISOLATED", [hold], make_frames(prices, days=days))

    (snapshot,) = result.positions  # day 2 ends flat: the last bar of the data closes it
    assert {k: v for k, v in snapshot.items() if k != "seq"} == {
        "ts": epoch(days[0], "15:25"), "instance": 0, "symbol": "AAA", "qty": 1000,
        "avg_price": 100.0, "mark": 110.0, "unrealized_pnl": 10_000.0,
    }  # fmt: skip


def test_a_daily_loss_limit_squares_off_and_stops_new_entries_until_the_next_day():
    """Phase 6: the portfolio's day P&L judged by the shared risk core (risk rejection)."""
    days = ("2024-01-01", "2024-01-02")
    prices = {"AAA": [100.0, 100.0, 98.0, 96.0] + [96.0] * 71 + [96.0] * 75, "BBB": [50.0] * 150}
    strategy = Scripted({0: {"AAA": 1.0}, 10: {"AAA": 1.0}})
    inst = Instance(0, "A", strategy, config())

    result = run("ISOLATED", [inst], make_frames(prices, days=days),
                 portfolio_limits={"daily_loss_limit": 3000})  # fmt: skip

    (breach,) = result.breaches
    assert breach["reason"] == "daily_loss" and breach["day_pnl"] == pytest.approx(-4000.0)
    assert breach["ts"] == epoch(days[0], "09:30")
    exits = [f for f in result.fills if f["reason"] == "daily_loss"]
    assert [(f["ts"], f["qty"], f["price"]) for f in exits] == [
        (epoch(days[0], "09:35"), -1000, 96.0)
    ]
    entries = [f for f in result.fills if f["reason"] == "signal"]
    assert [f["ts"] for f in entries] == [epoch(days[0], "09:20"), epoch(days[1], "09:20")]
    assert any(e["kind"] == "SESSION_EVENT" and e["payload"]["event"] == "portfolio limit"
               for e in result.events)  # fmt: skip
    reconcile(result)


def test_isolated_limits_stop_only_the_strategy_that_lost():
    prices = {"AAA": [100.0, 100.0, 96.0] + [96.0] * 72, "BBB": [50.0] * 75}
    loser = Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config())
    other = Instance(1, "B", Scripted({0: {"BBB": 1.0}, 20: {"BBB": 0.5}}), config())

    result = run("ISOLATED", [loser, other], make_frames(prices),
                 portfolio_limits={"daily_loss_limit": 3000})  # fmt: skip

    assert [b["portfolio"] for b in result.breaches] == [0]
    trims = [f for f in result.fills if f["instance"] == 1 and f["reason"] == "signal"]
    assert [f["qty"] for f in trims] == [2000, -1000], "B kept trading after A stopped"


def test_exposure_follows_what_each_book_holds():
    prices = {"AAA": [100.0] * 75, "BBB": [50.0] * 75}
    inst = Instance(0, "A", Scripted({0: {"AAA": 0.5}}), config())

    result = run("ISOLATED", [inst], make_frames(prices))

    assert result.book_gross[0, 0] == 0.0, "decided at bar 0, filled at bar 1"
    assert result.book_gross[0, 1] == pytest.approx(50_000.0)
    np.testing.assert_allclose(result.portfolio_gross, result.book_gross)
    np.testing.assert_allclose(result.portfolio_net, result.book_gross)


def test_netting_impact_counts_what_crossing_inside_the_portfolio_saved():
    prices = {"AAA": [100.0] * 75, "BBB": [50.0] * 75}
    a = Instance(0, "A", Scripted({0: {"AAA": 1.0}}), config())
    b = Instance(1, "B", Scripted({0: {"AAA": -1.0}}), config())
    costs = india_intraday("NSE")

    result = run("SHARED", [a, b], make_frames(prices), costs=costs)

    alone = (
        costs.breakdown(100_000.0, 0.0, orders=1)["total"]
        + costs.breakdown(0.0, 100_000.0, orders=1)["total"]
    )
    assert result.netting["orders_avoided"] == 4, "entry and exit, two books each"
    assert result.netting["notional_netted"] == pytest.approx(400_000.0)
    assert result.netting["costs_saved"] == pytest.approx(2 * alone)
    (pair,) = result.overlap
    assert pair["same_pct"] == 0 and pair["opposite_pct"] > 0


def test_run_metrics_report_capital_at_work_and_each_strategys_share():
    from research.worker import summarize_result

    prices = {"AAA": [100.0, 100.0] + [101.0] * 73, "BBB": [50.0, 50.0] + [51.0] * 73}
    a = Instance(0, "A", Scripted({0: {"AAA": 0.5}}), config())
    b = Instance(1, "B", Scripted({0: {"BBB": 0.5}}), config())

    result = run("SHARED", [a, b], make_frames(prices))
    metrics, attribution = summarize_result(result)

    assert metrics["final_equity"] == pytest.approx(200_000 + metrics["net_pnl"])
    assert 0 < metrics["capital_used_pct"] <= metrics["avg_exposure_pct"]
    assert metrics["time_in_market_pct"] > 0
    assert sum(r["contribution_pct"] for r in attribution) == pytest.approx(100.0)
    together = metrics["together"]
    assert together["order_groups"] and together["netting"]["orders_avoided"] == 0
    assert {(o["a"], o["b"]) for o in together["overlap"]} == {(0, 1)}


class Counting:
    """Buys AAA on the first bar it is asked about, and remembers how much history it saw."""

    interval = "5m"

    def __init__(self):
        self.seen = []

    def decide(self, ctx):
        self.seen.append(len(ctx.bars("AAA")))
        return {"AAA": 1.0} if len(self.seen) == 1 else None


def test_bars_before_the_first_day_are_read_as_history_and_never_traded():
    frames = make_frames({"AAA": [100.0], "BBB": [50.0]}, days=("2024-01-01", "2024-01-02"))
    strategy = Counting()
    result = Engine(
        "ISOLATED",
        [Instance(0, "a", strategy, config())],
        frames,
        FREE,
        trade_from=epoch("2024-01-02", "00:00"),
    ).run()

    # The first decision already sees the whole first day as history.
    assert strategy.seen[0] == BARS_PER_DAY + 1
    assert result.timestamps[0] == epoch("2024-01-02", "09:15")
    assert len(result.timestamps) == BARS_PER_DAY
    assert result.fills[0]["ts"] == epoch("2024-01-02", "09:20")

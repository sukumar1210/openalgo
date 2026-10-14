"""One strategy's share of the sandbox account, as the Sandbox P&L page shows it."""

from datetime import date, datetime

import pytest

from portfolio.strategy_pnl import attribute, book

DAY = date(2026, 10, 9)


def row(strategy, action, quantity, price, minute, symbol="SBIN", product="MIS"):
    return {
        "tradeid": f"{strategy}-{minute}", "strategy": strategy, "symbol": symbol,
        "exchange": "NSE", "product": product, "action": action, "quantity": quantity,
        "price": price, "timestamp": datetime(2026, 10, 9, 9, 15) .replace(minute=minute),
    }  # fmt: skip


def test_a_square_off_counts_only_up_to_what_the_strategy_held():
    trades = [
        row("Alpha", "BUY", 10, 800.0, 16),
        row("Other", "BUY", 50, 801.0, 20),
        row("AUTO_SQUARE_OFF", "SELL", 60, 810.0, 59),
        row("AUTO_SQUARE_OFF", "SELL", 5, 810.0, 59, symbol="INFY"),  # it never held INFY
    ]

    fills = attribute(trades, "Alpha")

    assert [(f["qty"], f["reason"]) for f in fills] == [(10, "signal"), (-10, "square_off")]
    assert book(fills, {}, DAY)["summary"]["all_time_realized_pnl"] == pytest.approx(100.0)


def test_realized_pnl_runs_from_its_own_average_price_and_open_is_marked_at_ltp():
    trades = [
        row("Alpha", "BUY", 10, 100.0, 16),
        row("Alpha", "BUY", 10, 110.0, 20),  # average 105
        row("Alpha", "SELL", 5, 120.0, 30),  # +75 realized
        row("Alpha", "SELL", 10, 90.0, 40, symbol="INFY"),  # opens a short
    ]

    result = book(attribute(trades, ["Alpha"]), {("SBIN", "NSE", "MIS"): 100.0}, DAY)

    sbin = next(p for p in result["positions"] if p["symbol"] == "SBIN")
    assert (sbin["quantity"], sbin["average_price"]) == (15, pytest.approx(105.0))
    assert sbin["today_realized_pnl"] == pytest.approx(75.0)
    assert sbin["unrealized_pnl"] == pytest.approx(15 * (100.0 - 105.0))
    infy = next(p for p in result["positions"] if p["symbol"] == "INFY")
    assert (infy["quantity"], infy["status"], infy["unrealized_pnl"]) == (-10, "Open", 0.0)
    assert result["daily"] == [{"date": "2026-10-09", "realized_pnl": 75.0, "trades": 4}]
    assert result["summary"]["today_total_mtm"] == pytest.approx(75.0 - 75.0)


def test_a_reversal_closes_at_the_old_average_and_opens_at_the_new_price():
    trades = [row("Alpha", "BUY", 10, 100.0, 16), row("Alpha", "SELL", 15, 110.0, 20)]

    [pos] = book(attribute(trades, "Alpha"), {}, DAY)["positions"]

    assert pos["all_time_realized_pnl"] == pytest.approx(100.0)
    assert (pos["quantity"], pos["average_price"]) == (-5, pytest.approx(110.0))


def test_charges_follow_the_sandbox_days_schedule_and_stay_with_their_day():
    from datetime import datetime

    from portfolio.costs import schedule_for
    from portfolio.strategy_pnl import capital_on, daily_results, fill_charges

    buy = {"symbol": "SBIN", "exchange": "NSE", "product": "MIS", "qty": 10, "price": 800.0}
    expected = schedule_for("india_intraday_nse").breakdown(8000.0, 0.0, orders=1)
    assert fill_charges(buy) == pytest.approx(expected["total"] - expected["slippage"])
    assert fill_charges({**buy, "exchange": "NFO"}) is None  # no schedule: not guessed

    fills = [
        {**buy, "timestamp": datetime(2026, 10, 8, 9, 20)},
        {**buy, "qty": -10, "price": 810.0, "timestamp": datetime(2026, 10, 8, 11, 0)},
        {**buy, "exchange": "NFO", "timestamp": datetime(2026, 10, 9, 9, 20)},
    ]
    day8, day9 = daily_results(fills, DAY)
    assert day8["pnl"] == 100.0 and day8["net"] == pytest.approx(100.0 - day8["charges"])
    assert day9["unmodelled"] == 1 and day9["charges"] == 0.0
    assert capital_on(1000.0, [day8, day9], datetime(2026, 10, 8).date()) == 1000.0
    assert capital_on(1000.0, [day8, day9], datetime(2026, 10, 9).date()) == pytest.approx(
        1000.0 + day8["net"]
    )

"""A Run backtested (portfolio/run_sim.py): the Sandbox's rules over replayed fills.

Every figure here is worked out by hand from the rules the Sandbox applies;
test/research/test_runs.py holds the simulator to the Sandbox itself.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio import run_sim as rs
from portfolio.strategy_pnl import fill_charges

DAY = date(2026, 10, 15)  # a Thursday
DAY2 = date(2026, 10, 16)


def at(hhmm: str, day: date = DAY) -> int:
    """Epoch seconds of an IST time on ``day``."""
    hour, minute = map(int, hhmm.split(":"))
    return (day - date(1970, 1, 1)).days * 86_400 - 19_800 + hour * 3600 + minute * 60


def member(seat, source, kind="fixed", value=300000, sizing="platform"):
    return rs.Member(seat, seat.title(), source, kind, value, sizing, f"Trial / {seat.title()}")


def run(*members, mode="isolated", policy="own", limit="capital", loss=None):
    return rs.RunDefinition(500000, mode, policy, limit, tuple(members), loss)


def charge(qty: int, price: float) -> float:
    cost = fill_charges({"exchange": "NSE", "product": "MIS", "qty": qty, "price": price})
    return float(Decimal(str(cost)).quantize(Decimal("0.01")))


def source(*fills, reference=10_000_000):
    return rs.Source(reference, "NSE", list(fills))


def test_a_member_holds_the_source_scaled_and_pays_its_own_charges():
    out = rs.simulate(
        run(member("alpha", "a")),
        {"a": source((at("10:00"), "RELIANCE", 1000, 100.0, "signal"),
                     (at("11:00"), "RELIANCE", -1000, 110.0, "signal"))},
    )  # fmt: skip
    (day,) = out["seats"]["alpha"]
    costs = charge(30, 100.0) + charge(-30, 110.0)  # Rs 3 lakh of a Rs 1 crore source: 30
    assert (day["trades"], day["pnl"]) == (2, 300.0)
    assert day["charges"] == pytest.approx(costs)
    assert day["net"] == pytest.approx(300.0 - costs)
    assert out["days"][0]["net"] == pytest.approx(day["net"])


def test_a_share_that_rounds_to_nothing_holds_nothing():
    out = rs.simulate(
        run(member("alpha", "a")), {"a": source((at("10:00"), "RELIANCE", 33, 100.0, "signal"))}
    )
    assert out["seats"]["alpha"][0]["trades"] == 0  # 33 x 0.03 = 0.99, toward zero


def test_the_exposure_limit_trims_a_copy():
    """Capital 3 lakh: 6,000 shares at 100 is 6 lakh, so it holds 3,000."""
    out = rs.simulate(
        run(member("alpha", "a")),
        {"a": source((at("10:00"), "RELIANCE", 200_000, 100.0, "signal"),
                     (at("10:05"), "RELIANCE", -200_000, 100.0, "signal"))},
    )  # fmt: skip
    (day,) = out["seats"]["alpha"]
    assert day["charges"] == pytest.approx(charge(3000, 100.0) + charge(-3000, 100.0))


def test_the_limit_counts_what_is_held_in_other_symbols_at_cost():
    """Rs 2 lakh of RELIANCE held leaves Rs 1 lakh of room: 1,000 ZEEL, not 2,000."""
    out = rs.simulate(
        run(member("alpha", "a")),
        {"a": source((at("10:00"), "RELIANCE", 2000, 100.0, "signal"),
                     (at("10:05"), "ZEEL", 2000, 100.0, "signal"), reference=300000)},
    )  # fmt: skip
    (day,) = out["seats"]["alpha"]
    assert day["charges"] == pytest.approx(
        charge(2000, 100.0) + charge(1000, 100.0) + charge(-2000, 100.0) + charge(-1000, 100.0)
    )  # both squared off at the last price


def test_a_self_sized_member_trades_the_sources_own_quantity():
    out = rs.simulate(
        run(member("alpha", "a", sizing="self")),
        {"a": source((at("10:00"), "RELIANCE", 50, 100.0, "signal"),
                     (at("10:05"), "RELIANCE", -50, 101.0, "signal"))},
    )  # fmt: skip
    assert out["seats"]["alpha"][0]["pnl"] == 50.0


def test_the_square_off_closes_at_the_last_price_and_the_sources_own_is_not_copied():
    out = rs.simulate(
        run(member("alpha", "a")),
        {"a": source((at("10:00"), "RELIANCE", 1000, 100.0, "signal"),
                     (at("15:15"), "RELIANCE", -1000, 104.0, "square_off"))},
    )  # fmt: skip
    (day,) = out["seats"]["alpha"]
    assert (day["trades"], day["pnl"]) == (2, 120.0)  # 30 bought, squared off once at 104
    assert out["refused"] == 0  # the source's own square-off is not a copy at all


@pytest.mark.parametrize(
    ("policy", "alpha", "beta"),
    [("reset", lambda a, b: 300000, lambda a, b: 200000),
     ("own", lambda a, b: 300000 + a, lambda a, b: 200000 + b),
     ("pooled", lambda a, b: (500000 + a + b) * 0.6, lambda a, b: (500000 + a + b) * 0.4)],
)  # fmt: skip
def test_day_two_opens_by_the_policy_from_day_ones_results(policy, alpha, beta):
    """A strategy sized on its own capital (its source at its allocation) wins and loses."""
    out = rs.simulate(
        run(member("alpha", "a"), member("beta", "b", "percent", 40), policy=policy),
        {
            "a": source((at("10:00"), "RELIANCE", 1000, 100.0, "signal"),
                        (at("11:00"), "RELIANCE", -1000, 110.0, "signal"), reference=300000),
            "b": source((at("10:00"), "TCS", 1000, 100.0, "signal"),
                        (at("11:00"), "TCS", -1000, 80.0, "signal"), reference=200000),
        },
        days=[DAY, DAY2],
    )  # fmt: skip
    a1, a2 = out["seats"]["alpha"]
    b1, b2 = out["seats"]["beta"]
    assert (a1["pnl"], b1["pnl"]) == (10000.0, -20000.0)
    assert a2["opening"] == pytest.approx(alpha(a1["net"], b1["net"]), abs=0.01)
    assert b2["opening"] == pytest.approx(beta(a1["net"], b1["net"]), abs=0.01)
    # Moved as a transfer from what it closed with, as a Sandbox account is.
    assert a2["transfer"] == pytest.approx(a2["opening"] - a1["opening"] - a1["net"], abs=0.01)
    assert (a1["transfer"], a2["trades"], b2["trades"]) == (0.0, 0, 0)


def test_a_shared_run_nets_its_members_in_one_account():
    """Alpha long 30 and Beta short 20 hold 10; the square-off closes those 10."""
    out = rs.simulate(
        run(member("alpha", "a"), member("beta", "b", "percent", 40), mode="shared",
            policy="pooled"),
        {
            "a": source((at("10:00"), "RELIANCE", 1000, 100.0, "signal"),
                        (at("15:15"), "RELIANCE", -1000, 102.0, "square_off")),
            "b": source((at("11:00"), "RELIANCE", -1000, 101.0, "signal"),
                        (at("15:15"), "RELIANCE", 1000, 102.0, "square_off")),
        },
    )  # fmt: skip
    (day,) = out["seats"]["run"]
    costs = charge(30, 100.0) + charge(-20, 101.0) + charge(-10, 102.0)
    assert (day["trades"], day["pnl"]) == (3, 40.0)  # 20 x 1 realized, then 10 x 2
    assert day["charges"] == pytest.approx(costs)
    alpha, beta = out["members"]["alpha"][0], out["members"]["beta"][0]
    assert (alpha["opening"], beta["opening"]) == (300000.0, 200000.0)
    assert (alpha["trades"], beta["trades"]) == (2, 1)  # its own order and its square-off share


def test_the_loss_limit_squares_off_at_the_minute_it_is_passed_and_refuses_new_entries():
    """1% of Rs 3 lakh is Rs 3,000; 1,000 shares falling Rs 3.50 is past it at 10:30."""

    def ltp(symbol, exchange, ts):
        return 96.5 if ts >= at("10:30") else 100.0

    out = rs.simulate(
        run(member("alpha", "a"), loss=1),
        {"a": source((at("10:00"), "RELIANCE", 1000, 100.0, "signal"),
                     (at("11:00"), "RELIANCE", 500, 97.0, "signal"),
                     (at("12:00"), "RELIANCE", -1500, 99.0, "signal"), reference=300000)},
        ltp=ltp,
    )  # fmt: skip
    (day,) = out["seats"]["alpha"]
    assert (day["trades"], day["pnl"]) == (2, -3500.0)  # bought, then squared off at 96.50
    assert out["stopped"] == [{"date": DAY.isoformat(), "seat": "alpha"}]
    assert out["refused"] == 1  # the 11:00 addition; the 12:00 exit finds it flat


def test_without_prices_the_loss_limit_is_checked_at_each_fill():
    """Only a fill moves a mark: the source adding a share at 96 shows the loss.

    The average is kept to the paise as the Sandbox stores it: 1,000 at 100 and
    1 at 96 average 99.996, held as 100.00, so 1,001 sold at 96 realize -4,004.
    """
    out = rs.simulate(
        run(member("alpha", "a"), loss=1),
        {"a": source((at("10:00"), "RELIANCE", 1000, 100.0, "signal"),
                     (at("11:00"), "RELIANCE", 1, 96.0, "signal"),
                     (at("12:00"), "RELIANCE", -1001, 99.0, "signal"), reference=300000)},
    )  # fmt: skip
    (day,) = out["seats"]["alpha"]
    assert out["stopped"] == [{"date": DAY.isoformat(), "seat": "alpha"}]
    assert (day["trades"], day["pnl"]) == (3, -4004.0)  # bought, added one, squared off at 96


def test_a_day_without_fills_still_opens_and_closes():
    out = rs.simulate(run(member("alpha", "a")), {"a": source()}, days=[DAY])
    assert [(d["date"], d["net"], d["trades"]) for d in out["days"]] == [(DAY.isoformat(), 0, 0)]

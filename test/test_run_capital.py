"""A Run's money rules (portfolio/run_capital.py), pinned to the plan's worked example."""

from decimal import Decimal

import pytest

from portfolio.run_capital import allocation, copy_target, day_open, fingerprint, limit_for

D = Decimal

# Rs 5 lakh Run: A is allocated a fixed Rs 3 lakh, B 40% of the Run (Rs 2 lakh).
# Day one: A nets +10,000 and B -20,000.
ALLOCATED = {"A": allocation(500000, "fixed", 300000), "B": allocation(500000, "percent", 40)}
CLOSED = {"A": D("310000"), "B": D("180000")}


def test_allocations_are_rupees_or_a_share_of_the_run():
    assert ALLOCATED == {"A": D("300000.00"), "B": D("200000.00")}
    with pytest.raises(ValueError):
        allocation(500000, "weight", 1)


@pytest.mark.parametrize(
    ("policy", "capital", "transfer"),
    [
        ("reset", {"A": "300000", "B": "200000"}, {"A": "-10000", "B": "20000"}),
        ("own", {"A": "310000", "B": "180000"}, {"A": "0", "B": "0"}),
        ("pooled", {"A": "294000", "B": "196000"}, {"A": "-16000", "B": "16000"}),
    ],
)
def test_day_two_opens_as_the_plan_says(policy, capital, transfer):
    opened = day_open(policy, ALLOCATED, CLOSED)
    assert {m: c for m, (c, _t) in opened.items()} == {m: D(v) for m, v in capital.items()}
    assert {m: t for m, (_c, t) in opened.items()} == {m: D(v) for m, v in transfer.items()}


def test_a_member_that_has_not_traded_opens_at_its_allocation():
    assert day_open("own", ALLOCATED, {})["B"] == (D("200000.00"), D("0.00"))


def test_pooling_never_creates_or_destroys_a_paisa():
    allocated = {"A": D("1"), "B": D("1"), "C": D("1")}
    opened = day_open("pooled", allocated, {"A": D("100"), "B": D("0"), "C": D("0")})
    assert sum(c for c, _t in opened.values()) == D("100")
    assert sum(t for _c, t in opened.values()) == D("0")


def test_the_limit_grows_with_capital_or_stays_at_the_allocation():
    assert limit_for("capital", D("310000"), D("300000")) == D("310000.00")
    assert limit_for("fixed", D("310000"), D("300000")) == D("300000.00")


def test_a_copy_is_the_source_scaled_and_rounded_toward_zero():
    common = {"sizing": "platform", "price": D("100"), "limit": D("1000000")}
    assert copy_target(101, scale=D("0.5"), **common) == 50
    assert copy_target(-101, scale=D("0.5"), **common) == -50
    assert copy_target(100, scale=D("0.004"), **common) == 0  # holds nothing
    assert copy_target(3, scale=D("0.9"), **common) == 2  # 2.7: toward zero, never up
    assert copy_target(-3, scale=D("0.9"), **common) == -2


def test_a_self_sized_strategy_is_copied_as_it_sized_itself():
    assert copy_target(37, scale=D("0.5"), sizing="self", price=D("100"), limit=D("1e6")) == 37


def test_a_copy_never_takes_the_member_past_its_limit():
    target = copy_target(
        200, scale=D("1"), sizing="platform", price=D("100"), limit=D("10000"),
        exposure_elsewhere=D("4000"),
    )  # fmt: skip
    assert target == 60
    assert copy_target(-200, scale=D("1"), sizing="platform", price=D("100"), limit=D("10000"),
                       exposure_elsewhere=D("12000")) == 0  # fmt: skip


def test_a_fingerprint_names_what_decides_trades_and_not_the_capital():
    base = {
        "exchange": "NSE",
        "schedule": {"start": "09:15", "stop": "15:10", "days": ["mon"]},
        "symbols": ["TCS", "SBIN"],
        "settings": {},
        "sizing": "platform",
    }
    same = fingerprint("v1", **base, capital=100000)
    assert same == fingerprint("v1", **base, capital=900000)
    assert same != fingerprint("v2", **base)
    assert same != fingerprint("v1", **{**base, "symbols": ["SBIN", "TCS"]})
    dependent = fingerprint("v1", **base, capital_dependent=True, capital=100000)
    assert dependent != fingerprint("v1", **base, capital_dependent=True, capital=900000)

"""Copying a source account's position into its followers (sandbox/copy_engine.py)."""

import uuid
from decimal import Decimal

import pytest
import pytz

from database.sandbox_db import (
    SandboxAccount,
    SandboxAccountEvent,
    SandboxCopy,
    SandboxFollow,
    SandboxFunds,
    SandboxOrders,
    SandboxPositions,
    SandboxTrades,
    db_session,
    init_db,
)
from sandbox import accounts, copy_engine
from sandbox.execution_engine import ExecutionEngine
from sandbox.order_manager import OrderManager

IST = pytz.timezone("Asia/Kolkata")
QUOTE = {"ltp": 100.0, "high": 101.0, "low": 99.0}


@pytest.fixture
def market(monkeypatch):
    """Accounts on, a fixed quote, and 11:00 IST: MIS is refused outside market hours."""
    import sandbox.order_manager as order_manager

    monkeypatch.setenv("SANDBOX_ACCOUNTS_ENABLED", "true")
    init_db()
    accounts._info.clear()
    real = order_manager.datetime

    class AtEleven(real):
        @classmethod
        def now(cls, tz=None):
            # Only the hour: order ids are built from the microseconds.
            moment = real.now(IST).replace(hour=11, minute=0)
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(order_manager, "datetime", AtEleven)
    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: dict(QUOTE))
    monkeypatch.setattr(
        ExecutionEngine,
        "_fetch_quotes_batch",
        lambda self, symbols: {(s, e): dict(QUOTE) for s, e in symbols},
    )


@pytest.fixture
def owner():
    name = f"copy-{uuid.uuid4().hex[:6]}"
    yield name
    principals = [name] + [a.principal for a in SandboxAccount.query.filter_by(owner=name)]
    follows = SandboxFollow.query.filter(SandboxFollow.source.in_(principals)).all()
    SandboxCopy.query.filter(SandboxCopy.follow_id.in_([f.id for f in follows])).delete(
        synchronize_session=False
    )
    SandboxFollow.query.filter(SandboxFollow.source.in_(principals)).delete(
        synchronize_session=False
    )
    for model in (SandboxOrders, SandboxTrades, SandboxPositions, SandboxFunds):
        model.query.filter(model.user_id.in_(principals)).delete(synchronize_session=False)
    SandboxAccountEvent.query.filter(SandboxAccountEvent.principal.in_(principals)).delete(
        synchronize_session=False
    )
    SandboxAccount.query.filter_by(owner=name).delete()
    db_session.commit()
    accounts._info.clear()


def _open(owner, kind, capital):
    return accounts.create(owner, kind, capital, kind, charges=False)["principal"]


def _trade(user_id, action, quantity, strategy="Alpha #abc123"):
    ok, response, _ = OrderManager(user_id).place_order(
        {
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "action": action,
            "quantity": quantity,
            "price_type": "MARKET",
            "product": "MIS",
            "strategy": strategy,
        }
    )
    assert ok, response
    copy_engine.wait_idle()
    return response["orderid"]


def _net(user_id):
    db_session.expire_all()
    row = SandboxPositions.query.filter_by(user_id=user_id, symbol="RELIANCE").first()
    return row.quantity if row else 0


def _names(user_id):
    db_session.expire_all()
    return sorted({t.strategy for t in SandboxTrades.query.filter_by(user_id=user_id)})


def test_followers_hold_the_source_scaled_to_their_capital(market, owner):
    source = _open(owner, "source", 100000)
    half = _open(owner, "member", 50000)
    double = _open(owner, "member", 200000)
    copy_engine.set_follow(source, half, "Run A / Alpha", scale="0.5", limit=50000)
    copy_engine.set_follow(source, double, "Run B / Alpha", scale="2", limit=200000)

    _trade(source, "BUY", 101)
    assert (_net(source), _net(half), _net(double)) == (101, 50, 202)
    assert _names(half) == ["Run A / Alpha"]

    _trade(source, "SELL", 41)  # an exit is copied as a new target, not a new delta
    assert (_net(source), _net(half), _net(double)) == (60, 30, 120)

    _trade(source, "SELL", 60)
    assert (_net(half), _net(double)) == (0, 0)


def test_a_copy_stays_within_the_followers_limit(market, owner):
    source = _open(owner, "source", 100000)
    capped = _open(owner, "member", 100000)
    copy_engine.set_follow(source, capped, "Capped", scale="2", limit=10000)
    _trade(source, "BUY", 100)
    assert _net(capped) == 100  # 200 wanted, Rs 10,000 at Rs 100 allows 100


def test_a_self_sized_strategy_is_copied_at_its_own_quantity(market, owner):
    source = _open(owner, "source", 100000)
    follower = _open(owner, "member", 500000)
    copy_engine.set_follow(source, follower, "Self", scale="5", limit=500000, sizing="self")
    _trade(source, "BUY", 30)
    assert _net(follower) == 30


def test_a_follower_whose_share_rounds_to_nothing_gets_no_order(market, owner):
    source = _open(owner, "source", 100000)
    tiny = _open(owner, "member", 1000)
    follow = copy_engine.set_follow(source, tiny, "Tiny", scale="0.004", limit=1000)
    _trade(source, "BUY", 100)
    assert _net(tiny) == 0
    assert SandboxCopy.query.filter_by(follow_id=follow.id).count() == 0


def test_the_sources_square_off_is_not_copied(market, owner):
    source = _open(owner, "source", 100000)
    follower = _open(owner, "member", 100000)
    copy_engine.set_follow(source, follower, "F", scale="1", limit=100000)
    _trade(source, "BUY", 10)
    _trade(source, "SELL", 10, strategy="AUTO_SQUARE_OFF")
    assert (_net(source), _net(follower)) == (0, 10)  # the follower is squared off on its own


def test_a_trade_is_copied_once_whoever_asks(market, owner):
    source = _open(owner, "source", 100000)
    follower = _open(owner, "member", 100000)
    follow = copy_engine.set_follow(source, follower, "F", scale="1", limit=100000)
    copy_engine.set_follow(source, follower, "F", scale="1", limit=100000, active=False)
    _trade(source, "BUY", 10)  # not followed yet
    db_session.expire_all()
    tradeid = SandboxTrades.query.filter_by(user_id=source).one().tradeid
    db_session.add(SandboxCopy(source_tradeid=tradeid, follow_id=follow.id, status="claimed"))
    db_session.commit()
    copy_engine.set_follow(source, follower, "F", scale="1", limit=100000)

    copy_engine._copy_all(source, "RELIANCE", "NSE", "MIS", tradeid, Decimal("100"))
    assert _net(follower) == 0  # someone else holds the claim
    copy_engine._copy_all(source, "RELIANCE", "NSE", "MIS", "another-trade", Decimal("100"))
    assert _net(follower) == 10  # a later trade catches the follower up


def test_a_shared_account_holds_each_member_by_its_order_name(market, owner):
    source = _open(owner, "source", 100000)
    run = _open(owner, "run", 200000)
    copy_engine.set_follow(source, run, "Run / M1", scale="0.5", limit=200000, shared=True)
    copy_engine.set_follow(source, run, "Run / M2", scale="0.3", limit=200000, shared=True)

    _trade(source, "BUY", 100)
    assert _net(run) == 80
    follows = SandboxFollow.query.filter_by(follower=run).order_by(SandboxFollow.id).all()
    held = [copy_engine._held(f, "RELIANCE", "NSE", "MIS") for f in follows]
    assert held == [50, 30]

    _trade(source, "SELL", 100)
    assert _net(run) == 0


def test_a_refused_copy_is_recorded_with_its_reason(market, owner):
    source = _open(owner, "source", 100000)
    poor = _open(owner, "member", 100)
    follow = copy_engine.set_follow(source, poor, "Poor", scale="1", limit=100000)
    _trade(source, "BUY", 100)
    copy = SandboxCopy.query.filter_by(follow_id=follow.id).one()
    assert copy.status == "refused" and copy.reason
    assert _net(poor) == 0


def test_what_a_follower_holds_elsewhere_counts_toward_its_limit(market, owner):
    source = _open(owner, "source", 100000)
    follower = _open(owner, "member", 100000)
    ok, response, _ = OrderManager(follower).place_order(
        {"symbol": "ZEEL", "exchange": "NSE", "action": "BUY", "quantity": 60,
         "price_type": "MARKET", "product": "MIS", "strategy": "own"}
    )  # fmt: skip
    assert ok, response
    copy_engine.set_follow(source, follower, "F", scale="1", limit=10000)
    _trade(source, "BUY", 100)
    assert _net(follower) == 40  # Rs 6,000 already held of Rs 10,000: room for 40 at Rs 100

"""The sandbox's MIS loss limit, as a broker's risk system squares off on an MTM loss.

Off by default, and then nothing changes: no job is scheduled and no order is
refused. On, once the day's loss on MIS positions (realized today plus open,
at current prices) reaches the set % of capital, every MIS position is squared
off and new MIS entries are refused until the next session. Turning it off
lifts the block; a new session does too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_gthread_sandbox_support import (
    configs_restored,
    prepare_databases,
    quote,
    release_sessions,
    reset_user,
    run_in_thread,
)

USER = "sandbox_loss_limit"
OWNER = "sandbox_loss_limit_runs"  # the owner of the Sandbox accounts below
ENTRY = Decimal("2500.00")


def _market(monkeypatch) -> dict:
    """A market the test moves, and no time-based square-off whatever the hour."""
    from sandbox import position_manager
    from sandbox.execution_engine import ExecutionEngine
    from sandbox.squareoff_manager import SquareOffManager

    market = {"ltp": ENTRY}
    monkeypatch.setattr(
        ExecutionEngine, "_fetch_quote", lambda self, symbol, exchange: quote(market["ltp"])
    )
    monkeypatch.setattr(
        position_manager.PositionManager,
        "_fetch_quotes_from_websocket",
        lambda self, symbols: {key: quote(market["ltp"]) for key in symbols},
    )
    # Only the loss limit decides here, whatever the hour the suite runs at.
    real_init = SquareOffManager.__init__

    def no_square_off_times(self):
        real_init(self)
        self.square_off_times = {}

    monkeypatch.setattr(SquareOffManager, "__init__", no_square_off_times)
    monkeypatch.setattr(position_manager, "cleanup_expired_contracts", lambda: None)
    return market


def _hold(user_id: str, quantity: int) -> None:
    """Open ``quantity`` RELIANCE (BSE, MIS) at the entry price, its margin blocked."""
    from database.sandbox_db import SandboxPositions, db_session
    from sandbox.fund_manager import FundManager

    margin = (ENTRY * quantity / 5).quantize(Decimal("0.01"))
    db_session.add(
        SandboxPositions(
            user_id=user_id, symbol="RELIANCE", exchange="BSE", product="MIS", quantity=quantity,
            average_price=ENTRY, ltp=ENTRY, pnl=Decimal("0.00"), pnl_percent=Decimal("0.00"),
            accumulated_realized_pnl=Decimal("0.00"), today_realized_pnl=Decimal("0.00"),
            margin_blocked=margin,
        )
    )  # fmt: skip
    db_session.commit()
    assert FundManager(user_id).block_margin(margin, "seed")[0]
    release_sessions()


def _open_account(kind="member", label="Trial / Alpha") -> str:
    """A Rs 10 lakh Sandbox account of OWNER's."""
    from sandbox import accounts

    return accounts.create(OWNER, label, 1000000, kind)["principal"]


def _close_account(principal: str) -> None:
    from database.sandbox_db import SandboxAccount, SandboxAccountEvent, SandboxFunds, db_session
    from sandbox import accounts

    release_sessions()
    reset_user(principal)
    SandboxFunds.query.filter_by(user_id=principal).delete()
    SandboxAccountEvent.query.filter_by(principal=principal).delete()
    SandboxAccount.query.filter_by(principal=principal).delete()
    db_session.commit()
    release_sessions()
    accounts._info.clear()


@pytest.fixture
def account(monkeypatch):
    """A 1 Cr account long 1000 RELIANCE (BSE, MIS) at 2500, and a market it can move."""
    prepare_databases()
    reset_user(USER)
    release_sessions()
    from database.sandbox_db import set_config

    market = _market(monkeypatch)
    _hold(USER, 1000)
    with configs_restored(["mis_loss_limit_percent"]):
        set_config("mis_loss_limit_percent", "1")  # Rs 1,00,000 of Rs 1 Cr
        release_sessions()
        yield market
    # The square-off tests sweep every BSE MIS position in the shared test
    # database, so this account leaves none behind.
    release_sessions()
    reset_user(USER)


def _sweep():
    from sandbox.squareoff_manager import SquareOffManager

    run_in_thread(SquareOffManager().check_loss_limit)


@pytest.fixture
def run_account(monkeypatch):
    """A Rs 10 lakh Sandbox account long 100 RELIANCE (BSE, MIS), the Sandbox's own limit off."""
    prepare_databases()
    monkeypatch.setenv("SANDBOX_ACCOUNTS_ENABLED", "true")
    from database.sandbox_db import set_config

    market = _market(monkeypatch)
    principal = _open_account()
    _hold(principal, 100)
    try:
        with configs_restored(["mis_loss_limit_percent"]):
            set_config("mis_loss_limit_percent", "0")
            release_sessions()
            yield market, principal
    finally:
        _close_account(principal)


def _position(user_id=USER):
    from database.sandbox_db import SandboxPositions, db_session

    db_session.remove()
    row = SandboxPositions.query.filter_by(user_id=user_id, symbol="RELIANCE").one()
    quantity = row.quantity
    db_session.remove()
    return quantity


def _hit_at():
    from database.sandbox_db import SandboxFunds, db_session

    db_session.remove()
    hit = SandboxFunds.query.filter_by(user_id=USER).one().loss_limit_hit_at
    db_session.remove()
    return hit


def _buy_mis(user_id=USER):
    from sandbox.order_manager import OrderManager

    def place():
        return OrderManager(user_id).place_order(
            {"symbol": "RELIANCE", "exchange": "BSE", "action": "BUY", "quantity": 10,
             "price_type": "MARKET", "product": "MIS"}
        )  # fmt: skip

    return run_in_thread(place)


def test_a_loss_short_of_the_limit_changes_nothing(account):
    account["ltp"] = Decimal("2410.00")  # down Rs 90,000
    _sweep()
    assert _position() == 1000
    assert _hit_at() is None


def test_the_limit_squares_off_mis_and_refuses_new_entries_until_turned_off(account):
    from database.sandbox_db import set_config

    account["ltp"] = Decimal("2390.00")  # down Rs 1,10,000: past 1% of Rs 1 Cr
    _sweep()

    assert _position() == 0
    assert _hit_at() is not None
    ok, response, status = _buy_mis()
    assert not ok and status == 400
    assert response["message"].startswith(
        "Today's loss on intraday (MIS) positions reached the sandbox loss limit of 1% of capital"
    )

    set_config("mis_loss_limit_percent", "0")
    release_sessions()
    ok, response, _status = _buy_mis()
    assert ok, response


def test_a_new_session_lifts_the_block(account):
    from database.sandbox_db import SandboxFunds, db_session
    from sandbox.squareoff_manager import _session_start_utc, loss_limit_refusal

    start = _session_start_utc()
    row = SandboxFunds.query.filter_by(user_id=USER).one()
    row.loss_limit_hit_at = start - timedelta(minutes=1)  # the session before
    db_session.commit()
    assert loss_limit_refusal(USER) is None
    row.loss_limit_hit_at = min(
        start + timedelta(minutes=1), datetime.now(UTC).replace(tzinfo=None)
    )
    db_session.commit()
    assert loss_limit_refusal(USER) is not None
    db_session.remove()


def test_the_check_is_scheduled_only_while_the_limit_is_on(monkeypatch):
    from sandbox import squareoff_thread

    class RecordingScheduler:
        def __init__(self):
            self.jobs = {}

        def add_job(self, func, id, **kwargs):
            self.jobs[id] = func
            return type("Job", (), {"id": id})()

    for value, scheduled in (("0", False), ("2.5", True)):
        monkeypatch.setattr(
            squareoff_thread,
            "get_config",
            lambda key, default=None, v=value: v if key == "mis_loss_limit_percent" else default,
        )
        scheduler = RecordingScheduler()
        squareoff_thread._schedule_square_off_jobs(scheduler)
        assert ("mis_loss_limit" in scheduler.jobs) is scheduled
        if scheduled:
            assert hasattr(scheduler.jobs["mis_loss_limit"], "__wrapped__")  # releases sessions


def test_with_sandbox_accounts_on_the_check_is_scheduled_for_their_own_limits(monkeypatch):
    """A Run sets its accounts' limits each morning, which reloads no schedule."""
    from sandbox import squareoff_thread

    class RecordingScheduler:
        def __init__(self):
            self.jobs = {}

        def add_job(self, func, id, **kwargs):
            self.jobs[id] = func
            return type("Job", (), {"id": id})()

    monkeypatch.setattr(
        squareoff_thread,
        "get_config",
        lambda key, default=None: "0" if key == "mis_loss_limit_percent" else default,
    )
    for enabled, scheduled in (("false", False), ("true", True)):
        monkeypatch.setenv("SANDBOX_ACCOUNTS_ENABLED", enabled)
        scheduler = RecordingScheduler()
        squareoff_thread._schedule_square_off_jobs(scheduler)
        assert ("mis_loss_limit" in scheduler.jobs) is scheduled


def test_an_accounts_own_limit_applies_with_the_sandboxs_limit_off(run_account):
    from sandbox import accounts

    market, principal = run_account
    accounts.set_daily_loss_limit(principal, 5000)
    market["ltp"] = Decimal("2460.00")  # down Rs 4,000
    _sweep()
    assert _position(principal) == 100

    market["ltp"] = Decimal("2440.00")  # down Rs 6,000: past its Rs 5,000
    _sweep()
    assert _position(principal) == 0
    ok, response, status = _buy_mis(principal)
    assert not ok and status == 400
    assert response["message"].startswith(
        "Today's loss reached this account's daily loss limit of ₹5000.00, set by its Run"
    )

    accounts.set_daily_loss_limit(principal, None)  # its Run removed it: the block lifts
    ok, response, _status = _buy_mis(principal)
    assert ok, response


def test_the_nearer_of_the_two_limits_applies_and_is_the_one_named(run_account):
    from database.sandbox_db import set_config
    from sandbox import accounts

    market, principal = run_account
    accounts.set_daily_loss_limit(principal, 5000)
    set_config("mis_loss_limit_percent", "0.2")  # Rs 2,000 of Rs 10 lakh
    release_sessions()
    market["ltp"] = Decimal("2470.00")  # down Rs 3,000: past the Sandbox's, short of its own
    _sweep()
    assert _position(principal) == 0
    ok, response, _status = _buy_mis(principal)
    assert not ok
    assert response["message"].startswith(
        "Today's loss on intraday (MIS) positions reached the sandbox loss limit of 0.2%"
    )


def test_a_source_account_is_held_to_neither_limit(monkeypatch):
    """A source is followed; each account copying it is held to its own limit."""
    prepare_databases()
    monkeypatch.setenv("SANDBOX_ACCOUNTS_ENABLED", "true")
    from database.sandbox_db import set_config
    from sandbox import accounts

    market = _market(monkeypatch)
    source = _open_account(kind="source", label="Alpha #abc123")
    try:
        _hold(source, 100)
        accounts.set_daily_loss_limit(source, 5000)
        with configs_restored(["mis_loss_limit_percent"]):
            set_config("mis_loss_limit_percent", "1")  # Rs 10,000 of Rs 10 lakh
            release_sessions()
            market["ltp"] = Decimal("2300.00")  # down Rs 20,000: past both
            _sweep()
            assert _position(source) == 100
            ok, response, _status = _buy_mis(source)
            assert ok, response
    finally:
        _close_account(source)


def test_turning_the_sandboxs_limit_off_lifts_it_while_accounts_keep_their_own(
    account, monkeypatch
):
    """The check keeps running for the accounts' limits, and leaves the main account be."""
    from database.sandbox_db import set_config
    from sandbox import accounts

    monkeypatch.setenv("SANDBOX_ACCOUNTS_ENABLED", "true")
    account["ltp"] = Decimal("2390.00")  # the main account past 1% of Rs 1 Cr
    _sweep()
    assert _position() == 0
    other = _open_account()
    try:
        accounts.set_daily_loss_limit(other, 5000)
        set_config("mis_loss_limit_percent", "0")
        release_sessions()
        ok, response, _status = _buy_mis()
        assert ok, response
        _sweep()
        assert _position() == 10  # bought back in, and left alone
    finally:
        _close_account(other)


@pytest.mark.parametrize(
    ("value", "refused"),
    [("0", False), ("2.5", False), ("100", False), ("-1", True), ("101", True), ("x", True)],
)
def test_the_setting_takes_a_percentage_from_0_to_100(value, refused):
    from blueprints.sandbox import validate_config

    assert (validate_config("mis_loss_limit_percent", value) is not None) is refused

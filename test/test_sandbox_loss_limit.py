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
ENTRY = Decimal("2500.00")


@pytest.fixture
def account(monkeypatch):
    """A 1 Cr account long 1000 RELIANCE (BSE, MIS) at 2500, and a market it can move."""
    prepare_databases()
    reset_user(USER)
    release_sessions()
    from database.sandbox_db import SandboxPositions, db_session, set_config
    from sandbox import position_manager
    from sandbox.execution_engine import ExecutionEngine
    from sandbox.fund_manager import FundManager
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

    db_session.add(
        SandboxPositions(
            user_id=USER, symbol="RELIANCE", exchange="BSE", product="MIS", quantity=1000,
            average_price=ENTRY, ltp=ENTRY, pnl=Decimal("0.00"), pnl_percent=Decimal("0.00"),
            accumulated_realized_pnl=Decimal("0.00"), today_realized_pnl=Decimal("0.00"),
            margin_blocked=Decimal("500000.00"),
        )
    )  # fmt: skip
    db_session.commit()
    assert FundManager(USER).block_margin(Decimal("500000.00"), "seed")[0]
    release_sessions()
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


def _position():
    from database.sandbox_db import SandboxPositions, db_session

    db_session.remove()
    row = SandboxPositions.query.filter_by(user_id=USER, symbol="RELIANCE").one()
    quantity = row.quantity
    db_session.remove()
    return quantity


def _hit_at():
    from database.sandbox_db import SandboxFunds, db_session

    db_session.remove()
    hit = SandboxFunds.query.filter_by(user_id=USER).one().loss_limit_hit_at
    db_session.remove()
    return hit


def _buy_mis():
    from sandbox.order_manager import OrderManager

    def place():
        return OrderManager(USER).place_order(
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


@pytest.mark.parametrize(
    ("value", "refused"),
    [("0", False), ("2.5", False), ("100", False), ("-1", True), ("101", True), ("x", True)],
)
def test_the_setting_takes_a_percentage_from_0_to_100(value, refused):
    from blueprints.sandbox import validate_config

    assert (validate_config("mis_loss_limit_percent", value) is not None) is refused

"""Sandbox accounts: money of its own inside one user's Sandbox (sandbox/accounts.py).

An account is a registered principal, ``<owner>~<token>``, used as the
Sandbox's ``user_id``. These pin what that has to mean: its money, orders,
positions and charges are its own; it reaches the owner's market-data feed and
order stream without disturbing either; nothing unregistered gets in; and the
main Sandbox behaves exactly as before.
"""

import inspect
import json
import uuid
from decimal import Decimal

import pytest
import pytz

from database.sandbox_db import (
    SandboxAccount,
    SandboxAccountEvent,
    SandboxFunds,
    SandboxGTT,
    SandboxOrders,
    SandboxPositions,
    SandboxTrades,
    db_session,
    init_db,
)
from portfolio.strategy_pnl import fill_charges
from sandbox import accounts
from sandbox.execution_engine import ExecutionEngine
from sandbox.fund_manager import FundManager
from sandbox.order_manager import OrderManager

QUOTE = {"ltp": 100.0, "high": 101.0, "low": 99.0}
IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("SANDBOX_ACCOUNTS_ENABLED", "true")
    init_db()
    accounts._info.clear()


@pytest.fixture
def quotes(monkeypatch):
    """A fixed quote, at 11:00 IST: the Sandbox refuses MIS orders outside market hours."""
    import sandbox.order_manager as order_manager

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
    """A user of its own, and afterwards every row it and its accounts left."""
    name = f"acct-{uuid.uuid4().hex[:6]}"
    yield name
    principals = [name] + [a.principal for a in SandboxAccount.query.filter_by(owner=name)]
    for model in (SandboxOrders, SandboxTrades, SandboxPositions, SandboxFunds, SandboxGTT):
        model.query.filter(model.user_id.in_(principals)).delete(synchronize_session=False)
    SandboxAccountEvent.query.filter(SandboxAccountEvent.principal.in_(principals)).delete(
        synchronize_session=False
    )
    SandboxAccount.query.filter_by(owner=name).delete()
    db_session.commit()
    accounts._info.clear()


def _mis(user_id, action, quantity, price_type="MARKET", price=None, trigger_price=None):
    order = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": action,
        "quantity": quantity,
        "price_type": price_type,
        "product": "MIS",
    }
    if price is not None:
        order["price"] = price
    if trigger_price is not None:
        order["trigger_price"] = trigger_price
    ok, response, _code = OrderManager(user_id).place_order(order)
    assert ok, response
    return response["orderid"]


def _funds(user_id):
    db_session.expire_all()
    return SandboxFunds.query.filter_by(user_id=user_id).first()


def _net(user_id, symbol="RELIANCE"):
    db_session.expire_all()
    row = SandboxPositions.query.filter_by(user_id=user_id, symbol=symbol).first()
    return row.quantity if row else 0


def test_an_account_opens_with_its_own_capital_and_says_so(on, owner):
    account = accounts.create(owner, "Run A / Alpha", 100000, "member")
    principal = account["principal"]

    assert principal == f"{owner}~{account['token']}"
    assert accounts.owner_of(principal) == owner
    assert accounts.owner_of(owner) == owner
    assert _funds(principal).available_balance == Decimal("100000.00")
    events = SandboxAccountEvent.query.filter_by(principal=principal).all()
    assert [(e.kind, e.amount) for e in events] == [("open", Decimal("100000.00"))]


def test_accounts_are_off_unless_enabled(owner, monkeypatch):
    monkeypatch.delenv("SANDBOX_ACCOUNTS_ENABLED", raising=False)
    with pytest.raises(accounts.AccountError):
        accounts.create(owner, "A", 100000, "member")
    assert accounts.principal_for(owner, "0123abcd") is None


def test_two_accounts_and_the_main_sandbox_trade_apart(on, owner, quotes):
    a = accounts.create(owner, "A", 100000, "member", charges=False)["principal"]
    b = accounts.create(owner, "B", 200000, "member", charges=False)["principal"]
    _mis(a, "BUY", 100)
    _mis(b, "SELL", 50)
    _mis(owner, "BUY", 10)
    ExecutionEngine().check_and_execute_pending_orders()

    assert (_net(a), _net(b), _net(owner)) == (100, -50, 10)
    # MIS margin is a fifth of the notional at the default 5x leverage.
    assert _funds(a).used_margin == Decimal("2000.00")
    assert _funds(b).used_margin == Decimal("1000.00")
    assert _funds(a).available_balance == Decimal("98000.00")
    assert _funds(b).available_balance == Decimal("199000.00")
    assert _funds(owner).used_margin == Decimal("200.00")


def test_an_account_is_charged_in_the_fill_itself(on, owner, quotes):
    a = accounts.create(owner, "A", 100000, "member")["principal"]
    _mis(a, "BUY", 100)
    _mis(owner, "BUY", 100)
    ExecutionEngine().check_and_execute_pending_orders()

    trade = SandboxTrades.query.filter_by(user_id=a).one()
    expected = fill_charges({"exchange": "NSE", "product": "MIS", "qty": 100, "price": 100.0})
    event = SandboxAccountEvent.query.filter_by(principal=a, kind="charges").one()
    assert event.reference == trade.tradeid
    assert event.amount == -Decimal(str(expected)).quantize(Decimal("0.01"))
    funds = _funds(a)
    assert funds.realized_pnl == event.amount
    assert funds.today_realized_pnl == event.amount
    assert funds.available_balance == Decimal("98000.00") + event.amount
    assert SandboxAccountEvent.query.filter_by(principal=owner).count() == 0
    assert _funds(owner).realized_pnl == Decimal("0.00")


def test_an_account_without_charges_is_not_charged(on, owner, quotes):
    a = accounts.create(owner, "A", 100000, "member", charges=False)["principal"]
    _mis(a, "BUY", 100)
    ExecutionEngine().check_and_execute_pending_orders()
    assert SandboxAccountEvent.query.filter_by(principal=a, kind="charges").count() == 0


def test_an_unregistered_account_gets_no_money(on):
    ghost = f"ghost-{uuid.uuid4().hex[:6]}~deadbeef"
    try:
        assert FundManager(ghost).get_funds() is None
        assert SandboxFunds.query.filter_by(user_id=ghost).first() is None
    finally:
        SandboxFunds.query.filter_by(user_id=ghost).delete()
        db_session.commit()


def test_an_account_closes_only_once_it_holds_nothing(on, owner, quotes):
    account = accounts.create(owner, "A", 100000, "member")
    a = account["principal"]
    _mis(a, "BUY", 10)
    ExecutionEngine().check_and_execute_pending_orders()
    with pytest.raises(accounts.AccountError):
        accounts.close(a)

    _mis(a, "SELL", 10)
    ExecutionEngine().check_and_execute_pending_orders()
    accounts.close(a)
    assert accounts.principal_for(owner, account["token"]) is None


def test_money_moves_as_events_and_never_beyond_what_is_free(on, owner):
    a = accounts.create(owner, "A", 100000, "member")["principal"]
    accounts.move_money(a, 50000, "deposit", reason="top-up")
    with pytest.raises(accounts.AccountError):
        accounts.move_money(a, -200000, "withdraw", reason="too much")
    accounts.move_money(a, -30000, "withdraw", reason="sweep")

    funds = _funds(a)
    assert (funds.total_capital, funds.available_balance) == (
        Decimal("120000.00"),
        Decimal("120000.00"),
    )
    kinds = [e.kind for e in SandboxAccountEvent.query.filter_by(principal=a).order_by("id")]
    assert kinds == ["open", "deposit", "withdraw"]


# --- the owner's feed and order stream ---------------------------------------


def test_the_feed_stays_subscribed_while_any_account_of_the_owner_holds_the_symbol(
    on, owner, monkeypatch
):
    from sandbox import websocket_execution_engine as wse

    calls = []
    monkeypatch.setattr(
        wse,
        "subscribe_to_symbols",
        lambda **k: calls.append(("sub", k["username"])) or (True, {}, 200),
    )
    monkeypatch.setattr(
        wse,
        "unsubscribe_from_symbols",
        lambda **k: calls.append(("unsub", k["username"])) or (True, {}, 200),
    )
    monkeypatch.setattr("database.auth_db.get_api_key_for_tradingview", lambda user: "key")
    monkeypatch.setattr("database.auth_db.get_broker_name", lambda key: "zerodha")
    engine = wse.WebSocketExecutionEngine.__new__(wse.WebSocketExecutionEngine)
    engine._user_symbol_refcounts = {}
    a, b = f"{owner}~0000aaaa", f"{owner}~0000bbbb"

    assert engine._increment_user_symbol_refcount(a, "NSE:TCS") is True
    assert engine._increment_user_symbol_refcount(b, "NSE:TCS") is False
    assert engine._increment_user_symbol_refcount(owner, "NSE:TCS") is False
    assert engine._decrement_user_symbol_refcount(a, "NSE:TCS") is False
    assert engine._decrement_user_symbol_refcount(owner, "NSE:TCS") is False
    assert engine._decrement_user_symbol_refcount(b, "NSE:TCS") is True

    engine._subscribe_ws_symbols(a, [("TCS", "NSE")])
    engine._unsubscribe_ws_symbols(b, [("TCS", "NSE")])
    assert calls == [("sub", owner), ("unsub", owner)]


def test_an_accounts_order_updates_reach_its_owners_stream(on, owner):
    from types import SimpleNamespace

    from subscribers.wsproxy_subscriber import _resolve_user_id

    event = SimpleNamespace(request_data={"user_id": f"{owner}~0000aaaa"}, api_key=None)
    assert _resolve_user_id(event) == owner


# --- the main Sandbox's own routines leave accounts alone --------------------


def test_the_weekly_reset_and_a_capital_rebase_leave_accounts_alone(on, owner, monkeypatch):
    """Both act on every funds row; recorded here rather than run, so no other test's money moves."""
    from sandbox import fund_manager

    a = accounts.create(owner, "A", 100000, "member")["principal"]
    FundManager(owner).get_funds()
    rebased, reset = [], []
    monkeypatch.setattr(
        fund_manager,
        "apply_funds_change",
        lambda user_id, compute: rebased.append(user_id) or (True, ""),
    )
    monkeypatch.setattr(FundManager, "_reset_funds", lambda self, funds: reset.append(self.user_id))

    fund_manager.rebase_starting_capital(Decimal("2500000"))
    fund_manager.reset_all_user_funds()
    assert owner in rebased and a not in rebased
    assert owner in reset and a not in reset


# --- keys ------------------------------------------------------------------------


@pytest.fixture
def user_key(owner):
    """A real API key for ``owner``."""
    from database.auth_db import ApiKeys, upsert_api_key
    from database.auth_db import db_session as auth_session
    from database.auth_db import init_db as init_auth_db

    init_auth_db()  # a fresh checkout's test database has no tables yet
    real = uuid.uuid4().hex + uuid.uuid4().hex
    upsert_api_key(owner, real)
    yield real
    auth_session.query(ApiKeys).filter_by(user_id=owner).delete()
    auth_session.commit()
    auth_session.remove()


def test_an_account_key_reaches_only_its_own_open_account(on, owner, user_key):
    from database.auth_db import verify_api_key
    from services.sandbox_service import get_user_id_from_apikey
    from utils.strategy_mode import account_token, sandbox_key_for

    account = accounts.create(owner, "A", 100000, "member")
    key = sandbox_key_for(user_key, account["token"])
    assert account_token(key) == account["token"]
    assert account_token(sandbox_key_for(user_key)) is None
    assert verify_api_key(key) == owner
    assert get_user_id_from_apikey(key) == account["principal"]
    assert get_user_id_from_apikey(sandbox_key_for(user_key)) == owner

    # No account under that token; a token swapped onto another key's MAC; a forged MAC.
    assert get_user_id_from_apikey(sandbox_key_for(user_key, "0123abcd")) is None
    other = accounts.create(owner, "B", 100000, "member")
    assert verify_api_key(key.replace(account["token"], other["token"], 1)) is None
    assert verify_api_key(f"sandbox-{account['token']}-" + "0" * 64) is None

    accounts.close(account["principal"])
    assert get_user_id_from_apikey(key) is None


def test_account_keys_are_refused_while_accounts_are_off(owner, user_key, monkeypatch):
    from database.auth_db import verify_api_key
    from utils.strategy_mode import sandbox_key_for

    monkeypatch.delenv("SANDBOX_ACCOUNTS_ENABLED", raising=False)
    assert verify_api_key(sandbox_key_for(user_key, "0123abcd")) is None
    assert verify_api_key(sandbox_key_for(user_key)) == owner  # the plain Sandbox key, as before


def test_every_sandbox_entry_point_refuses_a_key_for_no_account(on, owner, user_key):
    """No ghost accounts: a key whose account does not exist is refused everywhere."""
    from services import sandbox_service
    from utils.strategy_mode import sandbox_key_for

    ghost = sandbox_key_for(user_key, "0123abcd")
    checked = []
    for name, fn in inspect.getmembers(sandbox_service, inspect.isfunction):
        params = inspect.signature(fn).parameters
        if not name.startswith("sandbox_") or "api_key" not in params:
            continue
        args = {
            p: ({} if "dict" in str(param.annotation) else "1")
            for p, param in params.items()
            if p != "api_key" and param.default is inspect.Parameter.empty
        }
        ok, _body, status = fn(api_key=ghost, **args)
        assert (ok, status) == (False, 403), name
        checked.append(name)
    assert len(checked) >= 15
    assert SandboxFunds.query.filter_by(user_id=f"{owner}~0123abcd").first() is None


def test_a_research_session_refuses_an_account_key(on, owner, user_key):
    from services.research_sandbox_service import SandboxRefused, _authorise
    from utils.strategy_mode import sandbox_key_for

    account = accounts.create(owner, "A", 100000, "member")
    with pytest.raises(SandboxRefused):
        _authorise(sandbox_key_for(user_key, account["token"]), 1)


# --- three faults in the Sandbox itself, fixed alongside -------------------------


def test_cancel_all_cancels_stop_orders_too(on, owner, quotes, monkeypatch):
    from services import sandbox_service

    a = accounts.create(owner, "A", 100000, "member")["principal"]
    stop = _mis(a, "SELL", 10, price_type="SL-M", trigger_price=95)
    limit = _mis(a, "BUY", 10, price_type="LIMIT", price=90)
    monkeypatch.setattr(sandbox_service, "get_user_id_from_apikey", lambda key: a)
    sandbox_service.sandbox_cancel_all_orders({}, "key", {})

    db_session.expire_all()
    statuses = {o.orderid: o.order_status for o in SandboxOrders.query.filter_by(user_id=a)}
    assert statuses == {stop: "cancelled", limit: "cancelled"}


def test_a_fill_keeps_the_margin_of_an_order_still_resting(on, owner, quotes):
    a = accounts.create(owner, "A", 100000, "member")["principal"]
    resting = _mis(a, "BUY", 10, price_type="LIMIT", price=90)
    _mis(a, "BUY", 100)
    ExecutionEngine().check_and_execute_pending_orders()

    db_session.expire_all()
    held = SandboxOrders.query.filter_by(orderid=resting).one()
    assert held.order_status == "open" and held.margin_blocked > 0
    assert _funds(a).used_margin == Decimal("2000.00") + held.margin_blocked


def test_the_scheduled_reset_cancels_what_still_holds_margin(on, owner, quotes):
    FundManager(owner).get_funds()
    order = _mis(owner, "BUY", 10, price_type="LIMIT", price=90)
    FundManager(owner)._reset_funds(SandboxFunds.query.filter_by(user_id=owner).first())

    db_session.expire_all()
    row = SandboxOrders.query.filter_by(orderid=order).one()
    assert (row.order_status, row.margin_blocked) == ("cancelled", Decimal("0.00"))
    OrderManager(owner).cancel_order(order)  # nothing left to release
    funds = _funds(owner)
    assert funds.used_margin == Decimal("0.00")
    assert funds.available_balance == funds.total_capital


# --- the engines -----------------------------------------------------------------


def test_the_engines_keep_running_while_a_strategy_trades_in_the_sandbox(tmp_path, monkeypatch):
    from sandbox import engines
    from services import analyzer_service

    configs = tmp_path / "strategy_configs.json"
    configs.write_text(json.dumps({"s1": {"mode": "sandbox"}}))
    monkeypatch.setattr(engines, "HOST_CONFIGS", configs)
    monkeypatch.delenv("SANDBOX_ACCOUNTS_ENABLED", raising=False)
    stopped = []
    monkeypatch.setattr(
        "sandbox.execution_thread.stop_execution_engine",
        lambda: stopped.append("engine") or (True, ""),
    )
    monkeypatch.setattr(
        "sandbox.squareoff_thread.stop_squareoff_scheduler",
        lambda *a, **k: stopped.append("scheduler"),
    )

    analyzer_service._reconcile_sandbox(False, with_scheduler=True, catchup=False)
    assert stopped == []
    configs.write_text(json.dumps({"s1": {"mode": "live"}}))
    analyzer_service._reconcile_sandbox(False, with_scheduler=True, catchup=False)
    assert stopped == ["engine", "scheduler"]


def test_an_open_account_needs_the_engines(on, owner, tmp_path, monkeypatch):
    from sandbox import engines

    monkeypatch.setattr(engines, "HOST_CONFIGS", tmp_path / "none.json")
    a = accounts.create(owner, "A", 100000, "member")["principal"]
    assert engines.needed_without_analyzer() is True
    accounts.close(a)


def test_an_accounts_missing_funds_come_back_at_its_own_capital(on, owner):
    a = accounts.create(owner, "A", 100000, "member")["principal"]
    SandboxFunds.query.filter_by(user_id=a).delete()
    db_session.commit()
    assert FundManager(a).get_funds()["availablecash"] == 100000.0


def test_the_weekly_auto_reset_never_touches_an_account(on, owner, monkeypatch):
    from datetime import datetime

    from sandbox import fund_manager

    today = datetime.now(IST).strftime("%A")
    settings = {"reset_day": today, "reset_time": "00:00"}
    monkeypatch.setattr(
        fund_manager, "get_config", lambda key, default=None: settings.get(key, default)
    )
    a = accounts.create(owner, "A", 100000, "member")["principal"]
    funds = _funds(a)
    funds.last_reset_date = datetime(2020, 1, 1)
    db_session.commit()
    accounts.move_money(a, 5000, "deposit", reason="won")

    FundManager(a).get_funds()
    assert (_funds(a).reset_count, _funds(a).total_capital) == (0, Decimal("105000.00"))


def test_the_engines_come_back_after_a_sandbox_reset_while_still_needed(monkeypatch):
    from blueprints import sandbox as sandbox_bp

    started = []
    monkeypatch.setattr("database.settings_db.get_analyze_mode", lambda: False)
    monkeypatch.setattr("sandbox.engines.needed_without_analyzer", lambda: True)
    monkeypatch.setattr(
        "sandbox.execution_thread.start_execution_engine", lambda: started.append("engine")
    )
    monkeypatch.setattr(
        "sandbox.squareoff_thread.start_squareoff_scheduler", lambda: started.append("scheduler")
    )
    sandbox_bp._resume_sandbox_engines({"engine": True, "scheduler": True})
    assert started == ["engine", "scheduler"]

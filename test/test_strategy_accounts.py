"""A Python strategy can trade a Sandbox account of its own, with settings of its own.

Four paper runs of two models (Alpha A and RES-1, each with a 15:10 exit and a
13:30 exit with a stop) pick the same stock on most days. In the one main
Sandbox book their positions net: one run's exit halves the position another
believes it holds, and each run's end-of-day position check and its resume
read a book that is not its own. They also share one .env, so a run's exit
time and stop cannot differ from its sibling's.

Binding a strategy (POST /python/account/<id>) opens a Sandbox account of
kind "strategy" at a stated capital and sets the strategy to Sandbox mode. Its
runs are handed that account's key, so its orders, positions, square-off and
the Sandbox's daily loss limit (a share of its own capital, the main book's
rule) are its own, and they are handed the account's worth as
OPENALGO_STRATEGY_CAPITAL. Its own settings (POST /python/settings/<id>, names
starting ALGO_) reach only its own runs. A strategy that is not bound is
launched exactly as before.
"""

from __future__ import annotations

import inspect
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import Flask, session
from test_gthread_sandbox_support import (
    configs_restored,
    prepare_databases,
    quote,
    release_sessions,
    reset_user,
    run_in_thread,
)

from utils.strategy_mode import account_token, sandbox_key_for

REAL_KEY = "a" * 64
ENTRY = Decimal("2500.00")


@pytest.fixture
def app():
    """A bare Flask app for request contexts: the real one needs a .env this checkout has not."""
    flask_app = Flask(__name__)
    flask_app.secret_key = "test-only"
    return flask_app


@pytest.fixture
def owner(monkeypatch):
    """A trader of its own with Sandbox accounts on, and afterwards every row it left."""
    from database.sandbox_db import SandboxAccount, SandboxAccountEvent, SandboxFunds, db_session
    from sandbox import accounts

    prepare_databases()
    monkeypatch.setenv("SANDBOX_ACCOUNTS_ENABLED", "true")
    accounts._info.clear()
    name = f"bind-{uuid.uuid4().hex[:6]}"
    yield name
    release_sessions()
    principals = [name] + [a.principal for a in SandboxAccount.query.filter_by(owner=name)]
    for principal in principals:
        reset_user(principal)
        SandboxFunds.query.filter_by(user_id=principal).delete()
    SandboxAccountEvent.query.filter(SandboxAccountEvent.principal.in_(principals)).delete(
        synchronize_session=False
    )
    SandboxAccount.query.filter_by(owner=name).delete()
    db_session.commit()
    release_sessions()
    accounts._info.clear()


@pytest.fixture
def host(monkeypatch, tmp_path, owner):
    """Three of the owner's strategies in the host, stopped; nothing saved to disk."""
    import blueprints.python_strategy as host
    import database.auth_db as auth

    configs = {}
    for sid in ("alpha_1510", "res1_1510", "plain"):
        script = tmp_path / f"{sid}.py"
        script.write_text("import os\nKEY = os.environ['OPENALGO_API_KEY']\n")
        configs[sid] = {
            "name": sid,
            "user_id": owner,
            "file_path": str(script),
            "file_name": script.name,
            "exchange": "NSE",
            "is_running": False,
        }
        monkeypatch.setitem(host.STRATEGY_CONFIGS, sid, configs[sid])
    monkeypatch.setattr(host, "save_configs", lambda: True)
    monkeypatch.setattr(auth, "get_api_key_for_tradingview", lambda user: REAL_KEY)
    monkeypatch.setattr("sandbox.engines.ensure_running", lambda: None)
    return SimpleNamespace(module=host, configs=configs, owner=owner)


def call(app, host, view_name, sid, body=None):
    """A host route as the logged-in owner: (status, json)."""
    view = inspect.unwrap(getattr(host.module, view_name))
    with app.test_request_context(json=body or {}, method="POST"):
        session["user"] = host.owner
        response = view(sid)
    response, status = response if isinstance(response, tuple) else (response, 200)
    return status, response.get_json()


def bind(app, host, sid, label, capital=500000):
    status, body = call(app, host, "bind_account_route", sid, {"label": label, "capital": capital})
    assert status == 200, body
    return body["account"]


def principal_of(host, sid) -> str:
    """Where an order placed with the key this strategy's run is handed lands."""
    from sandbox import accounts

    config = host.configs[sid]
    key, refusal = host.module._strategy_api_key(sid, config, host.module.Path(config["file_path"]))
    assert refusal is None, refusal
    token = account_token(key)
    return accounts.principal_for(host.owner, token) if token else host.owner


def hold(user_id: str, quantity: int) -> None:
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


def net(user_id: str) -> int:
    from database.sandbox_db import SandboxPositions, db_session

    db_session.remove()
    rows = SandboxPositions.query.filter_by(user_id=user_id, symbol="RELIANCE").all()
    total = sum(r.quantity for r in rows)
    db_session.remove()
    return total


# ---------------------------------------------------------------- the account


def test_funds_after_binding_are_exactly_the_stated_capital(app, host):
    from database.sandbox_db import SandboxAccountEvent, SandboxFunds, db_session

    account = bind(app, host, "alpha_1510", "alpha_a_1510")
    config = host.configs["alpha_1510"]
    assert config["mode"] == "sandbox" and config["account_token"] == account["token"]
    assert account["label"] == "alpha_a_1510" and account["status"] == "active"
    principal = principal_of(host, "alpha_1510")
    db_session.remove()
    funds = SandboxFunds.query.filter_by(user_id=principal).one()
    assert (funds.total_capital, funds.available_balance, funds.used_margin) == (
        Decimal("500000.00"),
        Decimal("500000.00"),
        Decimal("0.00"),
    )
    events = SandboxAccountEvent.query.filter_by(principal=principal).all()
    assert [(e.kind, e.amount) for e in events] == [("open", Decimal("500000.00"))]
    env = host.module._capital_env("alpha_1510", config)
    assert env == {"OPENALGO_STRATEGY_CAPITAL": "500000.00"}


def test_PLANTED_a_bound_strategys_orders_land_only_in_its_own_account(app, host):
    bind(app, host, "alpha_1510", "alpha_a_1510")
    bind(app, host, "res1_1510", "res1_1510")
    a, b = principal_of(host, "alpha_1510"), principal_of(host, "res1_1510")
    assert len({a, b, host.owner}) == 3, "a bound strategy's key named the main Sandbox book"
    assert principal_of(host, "plain") == host.owner, "an unbound strategy moved off the main book"

    from sandbox.order_manager import OrderManager

    ok, response, _ = run_in_thread(
        lambda: OrderManager(a).place_order(
            {"symbol": "RELIANCE", "exchange": "BSE", "action": "BUY", "quantity": 10,
             "price_type": "LIMIT", "price": 2400, "product": "CNC"}
        )
    )  # fmt: skip
    assert ok, response
    from database.sandbox_db import SandboxOrders, db_session

    db_session.remove()
    placed = SandboxOrders.query.filter_by(orderid=response["orderid"]).one()
    assert placed.user_id == a
    assert SandboxOrders.query.filter(SandboxOrders.user_id.in_([b, host.owner])).count() == 0
    db_session.remove()


def test_funds_can_be_added_and_taken_out_only_from_what_is_free(app, host, monkeypatch):
    # What an unbound strategy would be handed: its own capital setting, 5 lakh by default.
    monkeypatch.setattr(
        "services.strategy_performance_service.strategy_capital", lambda *a, **k: 500000.0
    )
    bind(app, host, "alpha_1510", "alpha_a_1510")
    status, body = call(app, host, "fund_account_route", "alpha_1510", {"amount": 100000})
    assert status == 200 and body["account"]["equity"] == 600000.0
    # Handed the account's worth, not the strategy's own capital setting (5 lakh by default too).
    capital = host.module._capital_env("alpha_1510", host.configs["alpha_1510"])
    assert capital == {"OPENALGO_STRATEGY_CAPITAL": "600000.00"}
    status, body = call(app, host, "fund_account_route", "alpha_1510", {"amount": -700000})
    assert status == 400 and "free" in body["message"]
    status, body = call(app, host, "fund_account_route", "alpha_1510", {"amount": -100000})
    assert status == 200 and body["account"]["equity"] == 500000.0


def test_labels_are_plain_and_unique_among_open_accounts(app, host):
    for bad in ("Alpha A", "", "_x", "a" * 41, "alpha-a"):
        status, _ = call(
            app, host, "bind_account_route", "alpha_1510", {"label": bad, "capital": 500000}
        )
        assert status == 400, bad
    bind(app, host, "alpha_1510", "alpha_a_1510")
    status, body = call(
        app, host, "bind_account_route", "res1_1510", {"label": "alpha_a_1510", "capital": 500000}
    )
    assert status == 409 and "already named" in body["message"]
    status, _ = call(
        app, host, "bind_account_route", "res1_1510", {"label": "res1_1510", "capital": 5000}
    )
    assert status == 400, "below the minimum capital"


def test_a_bound_strategy_stays_in_the_sandbox_with_its_account(app, host, monkeypatch):
    bind(app, host, "alpha_1510", "alpha_a_1510")
    status, body = call(app, host, "set_mode_route", "alpha_1510", {"mode": "live"})
    assert status == 409 and body["error_code"] == "ACCOUNT_BOUND"
    status, body = call(app, host, "delete_strategy", "alpha_1510")
    assert status == 409 and body["error_code"] == "ACCOUNT_OPEN"
    status, _ = call(
        app, host, "bind_account_route", "alpha_1510", {"label": "again", "capital": 500000}
    )
    assert status == 409, "a second account"

    host.configs["alpha_1510"]["is_running"] = True
    for view, body in (
        ("fund_account_route", {"amount": 1000}),
        ("close_account_route", {}),
        ("set_settings_route", {"env": {}}),
    ):
        assert call(app, host, view, "alpha_1510", body)[0] == 409, view
    host.configs["alpha_1510"]["is_running"] = False

    principal = principal_of(host, "alpha_1510")
    hold(principal, 10)
    status, body = call(app, host, "close_account_route", "alpha_1510")
    assert status == 400 and "position" in body["message"]
    reset_user(principal, Decimal("500000.00"))
    status, _ = call(app, host, "close_account_route", "alpha_1510")
    assert status == 200
    config = host.configs["alpha_1510"]
    assert "account_token" not in config and config["mode"] == "sandbox"
    assert principal_of(host, "alpha_1510") == host.owner, "back on the main Sandbox book"


def test_a_closed_account_is_never_traded_by_mistake(app, host):
    from sandbox import accounts

    bind(app, host, "alpha_1510", "alpha_a_1510")
    accounts.close(principal_of(host, "alpha_1510"))  # closed elsewhere, the binding left behind
    config = host.configs["alpha_1510"]
    key, refusal = host.module._strategy_api_key(
        "alpha_1510", config, host.module.Path(config["file_path"])
    )
    assert key is None and "closed" in refusal
    config["mode"] = "live"  # set by hand
    accounts._info.clear()
    key, refusal = host.module._strategy_api_key(
        "alpha_1510", config, host.module.Path(config["file_path"])
    )
    assert key is None and "Sandbox mode" in refusal


# ---------------------------------------------------------------- the loss limit


@pytest.fixture
def market(monkeypatch):
    from sandbox import position_manager
    from sandbox.execution_engine import ExecutionEngine
    from sandbox.squareoff_manager import SquareOffManager

    prices = {"ltp": ENTRY}
    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: quote(prices["ltp"]))
    monkeypatch.setattr(
        position_manager.PositionManager,
        "_fetch_quotes_from_websocket",
        lambda self, symbols: {key: quote(prices["ltp"]) for key in symbols},
    )
    real_init = SquareOffManager.__init__

    def no_square_off_times(self):
        real_init(self)
        self.square_off_times = {}

    monkeypatch.setattr(SquareOffManager, "__init__", no_square_off_times)
    monkeypatch.setattr(position_manager, "cleanup_expired_contracts", lambda: None)
    return prices


def test_PLANTED_a_loss_limit_in_one_account_closes_nothing_in_another(app, host, market):
    from database.sandbox_db import set_config
    from sandbox.squareoff_manager import SquareOffManager

    bind(app, host, "alpha_1510", "alpha_a_1510")
    bind(app, host, "res1_1510", "res1_1510")
    a, b = principal_of(host, "alpha_1510"), principal_of(host, "res1_1510")
    hold(a, 100)  # Rs 2.5 lakh of a Rs 5 lakh account
    hold(b, 10)  # Rs 25,000 of another
    with configs_restored(["mis_loss_limit_percent"]):
        set_config("mis_loss_limit_percent", "2")  # Rs 10,000 of each Rs 5 lakh account
        release_sessions()
        market["ltp"] = Decimal("2350.00")  # a: down Rs 15,000, past its limit; b: down Rs 1,500
        run_in_thread(SquareOffManager().check_loss_limit)
    assert net(a) == 0, "the account past its limit was not squared off"
    assert net(b) == 10, "a sibling account was squared off by another's loss"


# ---------------------------------------------------------------- settings


def launched_env(host, monkeypatch, sid) -> dict:
    """The environment a run of ``sid`` would be started with."""
    captured = {}

    class Recorder:
        def __init__(self, cmd, **kwargs):
            captured["env"] = kwargs["env"]
            self.pid = 2_000_300_000

        def poll(self):
            return None

    monkeypatch.setattr(host.module.subprocess, "Popen", Recorder)
    monkeypatch.setattr(host.module, "check_master_contract_ready", lambda *a, **k: (True, "ready"))
    monkeypatch.setattr(host.module, "broadcast_status_update", lambda *a, **k: None)
    monkeypatch.setattr(
        host.module, "_capital_env", lambda s, c: {"OPENALGO_STRATEGY_CAPITAL": "1"}
    )
    ok, message = host.module.start_strategy_process(sid)
    assert ok, message
    host.module.RUNNING_STRATEGIES.pop(sid, None)
    host.configs[sid]["is_running"] = False
    return captured["env"]


def test_PLANTED_a_strategys_own_settings_reach_only_its_own_run(app, host, monkeypatch, tmp_path):
    monkeypatch.setattr(host.module, "LOGS_DIR", tmp_path)
    for sid, run in (("alpha_1510", "alpha_a_1510"), ("res1_1510", "res1_1510")):
        status, _ = call(
            app, host, "set_settings_route", sid,
            {"env": {"ALGO_PAPER_RUN": run, "ALGO_PAPER_EXIT_TIME": "15:10"}},
        )  # fmt: skip
        assert status == 200
    a = launched_env(host, monkeypatch, "alpha_1510")
    b = launched_env(host, monkeypatch, "res1_1510")
    plain = launched_env(host, monkeypatch, "plain")
    assert a["ALGO_PAPER_RUN"] == "alpha_a_1510" and b["ALGO_PAPER_RUN"] == "res1_1510"
    assert "ALGO_PAPER_RUN" not in plain and "ALGO_PAPER_EXIT_TIME" not in plain
    assert a["STRATEGY_ID"] == "alpha_1510", "a setting replaced what the host hands a run"


@pytest.mark.parametrize(
    "env",
    [
        {"PYTHONPATH": "/tmp"},
        {"OPENALGO_API_KEY": "x"},
        {"STRATEGY_ID": "x"},
        {"algo_paper_run": "x"},
        {"ALGO_PAPER_RUN": "two\nlines"},
        {"ALGO_PAPER_RUN": "x" * 257},
        {f"ALGO_{i}": "x" for i in range(33)},
    ],
)
def test_settings_names_and_values_are_allowlisted(app, host, env):
    status, _ = call(app, host, "set_settings_route", "alpha_1510", {"env": env})
    assert status == 400
    assert "env" not in host.configs["alpha_1510"]


# ---------------------------------------------------------------- unbound, unchanged


def test_unbound_strategies_are_launched_exactly_as_before(host):
    config = host.configs["plain"]
    path = host.module.Path(config["file_path"])
    assert host.module._strategy_api_key("plain", config, path) == (REAL_KEY, None)  # Live
    config["mode"] = "sandbox"
    assert host.module._strategy_api_key("plain", config, path) == (sandbox_key_for(REAL_KEY), None)
    assert host.module._account_view(config) is None and host.module._strategy_env(config) == {}


# ---------------------------------------------------------------- the analyzer interlock


def test_PLANTED_the_analyzer_interlock_passes_under_an_account_key(app, host, monkeypatch):
    """algo-trading's interlock asks analyzerstatus with the key its run was handed."""
    from database.analyzer_db import init_db as init_analyzer_db
    from database.auth_db import ApiKeys, db_session, init_db, upsert_api_key, verify_api_key
    from services import analyzer_service

    init_db()
    init_analyzer_db()  # analyzerstatus counts its log rows
    real = uuid.uuid4().hex + uuid.uuid4().hex
    upsert_api_key(host.owner, real)
    import database.auth_db as auth

    monkeypatch.setattr(auth, "get_api_key_for_tradingview", lambda user: real)
    try:
        bind(app, host, "alpha_1510", "alpha_a_1510")
        config = host.configs["alpha_1510"]
        key, _ = host.module._strategy_api_key(
            "alpha_1510", config, host.module.Path(config["file_path"])
        )
        assert account_token(key) == config["account_token"]
        assert verify_api_key(key) == verify_api_key(real) == host.owner
        monkeypatch.setattr(analyzer_service, "get_analyze_mode", lambda: True)
        monkeypatch.setattr(
            analyzer_service,
            "get_auth_token_broker",
            lambda k: ("token", "fyers") if verify_api_key(k) else (None, None),
        )
        for k in (real, key):
            ok, response, status = analyzer_service.get_analyzer_status({"apikey": k}, api_key=k)
            assert (ok, status) == (True, 200) and response["data"]["analyze_mode"] is True
    finally:
        db_session.query(ApiKeys).filter_by(user_id=host.owner).delete()
        db_session.commit()
        db_session.remove()

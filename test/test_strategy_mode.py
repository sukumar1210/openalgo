"""A Python strategy's mode decides where its orders go, not the platform's switch.

Sandbox mode is enforced by the key the host hands the script: any API request
carrying it is answered as if Analyzer mode were on. What these tests pin:
an order with a Sandbox key never reaches the broker while Analyzer mode is
off (and the same order with the real key does, so the first is not vacuous);
the key cannot be forged; a Sandbox order is never parked for approval, where
it would lose its key; a Sandbox strategy cannot flip the platform's switch;
and the host hands out keys by mode and refuses the unsafe changes.
"""

import inspect
import uuid
from types import SimpleNamespace

import pytest
from flask import Flask, session

import restx_api  # noqa: F401 - imported first, as the app does, to settle its import cycle
from utils.strategy_mode import is_sandbox_key, request_is_sandbox_only, sandbox_key_for

REAL_KEY = "ab" * 32
ORDER = {
    "strategy": "Alpha", "symbol": "SBIN", "exchange": "NSE", "action": "BUY",
    "quantity": "1", "pricetype": "MARKET", "product": "MIS", "price": "0",
    "trigger_price": "0", "disclosed_quantity": "0",
}  # fmt: skip

app = Flask(__name__)
app.secret_key = "test-only"


class Switch:
    """The platform's Analyzer switch, as get_analyze_mode's cache holds it."""

    def __init__(self, on):
        self.on = on
        self.generation = 0

    def get(self, key, default=None):
        return self.on if key == "analyze_mode" else default


@pytest.fixture
def analyzer_off(monkeypatch):
    from database import settings_db

    monkeypatch.setattr(settings_db, "_settings_cache", Switch(False))


@pytest.fixture
def routes(monkeypatch, analyzer_off):
    """Record whether an order reached the Sandbox or the broker."""
    import services.place_order_service as orders
    import services.sandbox_service as sandbox
    from services import order_router_service

    reached = []

    def to_sandbox(order_data, api_key, original_data, **kwargs):
        reached.append("sandbox")
        return True, {"status": "success", "orderid": "S1", "mode": "analyze"}, 200

    def to_broker(order_data, auth_token):
        reached.append("broker")
        return SimpleNamespace(status=200), {"status": "success"}, "B1"

    monkeypatch.setattr(sandbox, "sandbox_place_order", to_sandbox)
    monkeypatch.setattr(orders, "get_auth_token_broker", lambda key: ("token", "zerodha"))
    monkeypatch.setattr(
        orders, "import_broker_module", lambda name: SimpleNamespace(place_order_api=to_broker)
    )
    monkeypatch.setattr(orders, "bus", SimpleNamespace(publish=lambda event: None))
    monkeypatch.setattr(order_router_service, "verify_api_key", lambda key: None)
    return reached


def place(key):
    from services.place_order_service import place_order

    with app.test_request_context(
        "/api/v1/placeorder", method="POST", json={**ORDER, "apikey": key}
    ):
        return place_order(order_data={**ORDER, "apikey": key}, api_key=key)


def test_an_order_with_a_sandbox_key_stays_in_the_sandbox_while_analyzer_is_off(routes):
    success, response, _ = place(sandbox_key_for(REAL_KEY))

    assert routes == ["sandbox"] and success and response["mode"] == "analyze"


def test_the_same_order_with_the_real_key_reaches_the_broker(routes):
    place(REAL_KEY)

    assert routes == ["broker"]


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"json": {"apikey": "KEY"}},
        {"headers": {"X-API-KEY": "KEY"}},
        {"query_string": {"apikey": "KEY"}},
        {"data": '{"apikey": "KEY"}', "content_type": "text/plain"},
    ],
    ids=["json", "header", "query", "json-as-text"],
)
def test_a_sandbox_key_is_found_wherever_the_request_puts_it(analyzer_off, request_kwargs):
    from database.settings_db import get_analyze_mode

    key = sandbox_key_for(REAL_KEY)
    kwargs = {
        k: ({n: (key if x == "KEY" else x) for n, x in v.items()} if isinstance(v, dict) else v)
        for k, v in request_kwargs.items()
    }
    if "data" in kwargs:
        kwargs["data"] = kwargs["data"].replace("KEY", key)
    with app.test_request_context("/api/v1/positionbook", method="POST", **kwargs):
        assert get_analyze_mode() is True
    with app.test_request_context("/api/v1/positionbook", method="POST", json={"apikey": REAL_KEY}):
        assert get_analyze_mode() is False
    assert get_analyze_mode() is False  # outside a request: the platform's switch


def test_only_api_requests_are_read():
    key = sandbox_key_for(REAL_KEY)
    with app.test_request_context("/python/new", method="POST", json={"apikey": key}):
        assert request_is_sandbox_only() is False


def test_a_sandbox_key_verifies_as_its_user_and_cannot_be_forged():
    from database.auth_db import ApiKeys, db_session, init_db, upsert_api_key, verify_api_key

    init_db()  # a fresh checkout's test database has no tables yet
    user = f"mode-test-{uuid.uuid4().hex[:8]}"
    real = uuid.uuid4().hex + uuid.uuid4().hex
    upsert_api_key(user, real)
    try:
        assert verify_api_key(sandbox_key_for(real)) == user
        assert verify_api_key("sandbox-" + "0" * 64) is None
        assert verify_api_key(sandbox_key_for(REAL_KEY)) is None  # another user's derivation
        assert not is_sandbox_key(real)
    finally:
        db_session.query(ApiKeys).filter_by(user_id=user).delete()
        db_session.commit()
        db_session.remove()


def test_a_sandbox_order_is_never_parked_for_approval(monkeypatch):
    from services import order_router_service as router

    monkeypatch.setattr(router, "verify_api_key", lambda key: "trader")
    monkeypatch.setattr(router, "get_order_mode", lambda user: "semi_auto")

    assert router.should_route_to_pending(REAL_KEY, "placeorder") is True
    assert router.should_route_to_pending(sandbox_key_for(REAL_KEY), "placeorder") is False


def test_a_sandbox_strategy_cannot_switch_analyzer_mode(monkeypatch):
    from services import analyzer_service

    def flipped(*args, **kwargs):
        raise AssertionError("the platform's switch was changed")

    monkeypatch.setattr(analyzer_service, "apply_analyze_mode", flipped)
    with app.test_request_context("/api/v1/analyzer/toggle", method="POST"):
        ok, response, status = analyzer_service.toggle_analyzer_mode_with_auth(
            {"mode": False}, "token", "zerodha", {"apikey": sandbox_key_for(REAL_KEY)}
        )
    assert (ok, status) == (False, 403) and "Sandbox mode" in response["message"]


# --------------------------------------------------------------------------- #
# The strategy host
# --------------------------------------------------------------------------- #

SID = "mode_test_20261013100000"


@pytest.fixture
def host(monkeypatch, tmp_path):
    import blueprints.python_strategy as host

    script = tmp_path / f"{SID}.py"
    script.write_text("import os\nKEY = os.environ['OPENALGO_API_KEY']\n")
    config = {"name": "Alpha", "user_id": "trader", "file_path": str(script), "is_running": False}
    monkeypatch.setitem(host.STRATEGY_CONFIGS, SID, config)
    monkeypatch.setattr(host, "save_configs", lambda: True)
    import database.auth_db as auth

    monkeypatch.setattr(auth, "get_api_key_for_tradingview", lambda user: REAL_KEY)
    return SimpleNamespace(module=host, config=config, script=script)


def set_mode(host, mode):
    view = inspect.unwrap(host.module.set_mode_route)
    with app.test_request_context(json={"mode": mode}, method="POST"):
        session["user"] = "trader"
        response = view(SID)
    response, status = response if isinstance(response, tuple) else (response, 200)
    return status, response.get_json()


def test_a_run_is_handed_the_key_of_its_mode(host):
    key_for = host.module._strategy_api_key

    assert key_for(SID, host.config, host.script) == (REAL_KEY, None)  # Live, as before modes
    host.config["mode"] = "sandbox"
    assert key_for(SID, host.config, host.script) == (sandbox_key_for(REAL_KEY), None)

    host.script.write_text(f"API_KEY = '{REAL_KEY}'\n")
    key, refusal = key_for(SID, host.config, host.script)
    assert key is None and "OPENALGO_API_KEY" in refusal


def test_the_mode_changes_only_while_stopped_and_never_out_of_the_sandbox_with_positions(
    host, monkeypatch
):
    import services.research_hosted_service as hosted

    held, started = [], []
    monkeypatch.setattr(hosted, "open_sandbox_positions", lambda sid: held)
    monkeypatch.setattr("sandbox.engines.ensure_running", lambda: started.append(True))

    assert set_mode(host, "sandbox")[0] == 200 and host.config["mode"] == "sandbox"
    assert started == [True]  # its resting orders fill and it is squared off with Analyzer off
    assert set_mode(host, "paper")[0] == 400

    host.config["is_running"] = True
    assert set_mode(host, "live")[0] == 409 and host.config["mode"] == "sandbox"
    host.config["is_running"] = False

    held.append({"symbol": "SBIN", "quantity": 10})
    status, body = set_mode(host, "live")
    assert status == 409 and "SBIN" in body["message"] and host.config["mode"] == "sandbox"

    held.clear()
    assert set_mode(host, "live")[0] == 200 and host.config["mode"] == "live"
    assert started == [True]


def test_a_strategys_capital_is_set_only_while_stopped_and_never_below_the_minimum(host):
    view = inspect.unwrap(host.module.set_capital_route)

    def set_capital(value):
        with app.test_request_context(json={"capital": value}, method="POST"):
            session["user"] = "trader"
            response = view(SID)
        response, status = response if isinstance(response, tuple) else (response, 200)
        return status, response.get_json()

    assert set_capital(250000)[0] == 200 and host.config["capital"] == 250000
    assert set_capital(5000)[0] == 400 and set_capital("lots")[0] == 400
    host.config["is_running"] = True
    assert set_capital(300000)[0] == 409 and host.config["capital"] == 250000

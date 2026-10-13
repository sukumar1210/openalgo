"""Symbol lists for Python strategies: typed once, handed to every strategy that uses one.

What these pin: pasted text of any shape becomes a clean list; a symbol the
broker does not know is refused by name; a list a strategy uses cannot be
deleted from under it; a strategy is handed its list when it starts, and is
refused a start, never started without it, if the list has gone; a choice of
list is refused while running and across exchanges.
"""

import inspect
from types import SimpleNamespace

import pytest
from flask import Flask, session

import restx_api  # noqa: F401 - imported first, as the app does, to settle its import cycle
from services import symbol_list_service as lists

app = Flask(__name__)
app.secret_key = "test-only"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Lists in their own file, against a master contract holding a few NSE symbols."""
    import database.token_db_enhanced as tokens

    monkeypatch.setattr(lists, "LISTS_FILE", tmp_path / "symbol_lists.json")
    known = {"RELIANCE", "TCS", "INFY", "SBIN"}
    monkeypatch.setattr(tokens, "get_symbol_count", lambda: len(known))
    monkeypatch.setattr(
        tokens, "get_symbol_info", lambda s, e: object() if e == "NSE" and s in known else None
    )
    return lists


def test_pasted_text_of_any_shape_becomes_a_clean_list():
    text = "reliance, TCS\n INFY.NS;  tcs\t\nSBIN.BO"

    assert lists.parse_symbols(text) == ["RELIANCE", "TCS", "INFY", "SBIN"]


def test_a_symbol_the_broker_does_not_know_is_refused_by_name(store):
    with pytest.raises(lists.SymbolListError, match="Not found on NSE: TATAMOTOR"):
        store.save_list(None, "Core", "NSE", "RELIANCE TATAMOTOR")

    saved = store.save_list(None, "Core", "NSE", "RELIANCE, TCS")
    assert saved["symbols"] == ["RELIANCE", "TCS"] and store.get_list(saved["id"])["name"] == "Core"
    with pytest.raises(lists.SymbolListError, match="already exists"):
        store.save_list(None, "core", "NSE", "INFY")


def test_a_list_in_use_cannot_be_deleted(store):
    saved = store.save_list(None, "Core", "NSE", "RELIANCE")

    with pytest.raises(lists.SymbolListError, match="Alpha still use"):
        store.delete_list(saved["id"], ["Alpha"])
    store.delete_list(saved["id"], [])
    assert store.get_list(saved["id"]) is None


def test_a_strategy_is_handed_its_list_or_refused_if_it_has_gone(store):
    saved = store.save_list(None, "Core", "NSE", "RELIANCE INFY")

    assert store.strategy_env({"symbol_list": saved["id"]}) == {"OPENALGO_SYMBOLS": "RELIANCE,INFY"}
    assert store.strategy_env({}) == {}
    with pytest.raises(lists.SymbolListError, match="no longer exists"):
        store.strategy_env({"symbol_list": "gone"})


@pytest.fixture
def host(store, monkeypatch, tmp_path):
    import blueprints.python_strategy as host

    config = {"name": "Alpha", "user_id": "trader", "exchange": "NSE", "is_running": False}
    monkeypatch.setitem(host.STRATEGY_CONFIGS, "alpha_1", config)
    monkeypatch.setattr(host, "save_configs", lambda: True)
    return SimpleNamespace(module=host, config=config)


def choose(host, list_id):
    view = inspect.unwrap(host.module.set_symbol_list_route)
    with app.test_request_context(json={"list_id": list_id}, method="POST"):
        session["user"] = "trader"
        response = view("alpha_1")
    response, status = response if isinstance(response, tuple) else (response, 200)
    return status, response.get_json()


def test_a_strategys_list_is_chosen_only_while_stopped_and_on_its_own_exchange(host, store):
    nse = store.save_list(None, "Core", "NSE", "RELIANCE")
    from utils.json_file import write_json_atomic

    metals = {"name": "Metals", "exchange": "MCX", "symbols": ["GOLD"], "checked": False}
    write_json_atomic(store.LISTS_FILE, {**store.load_lists(), "mcx1": metals})

    status, body = choose(host, "mcx1")
    assert status == 400 and "MCX list" in body["message"]
    host.config["is_running"] = True
    assert choose(host, nse["id"])[0] == 409
    host.config["is_running"] = False
    assert choose(host, nse["id"])[0] == 200 and host.config["symbol_list"] == nse["id"]
    with app.test_request_context():
        listed = host.module._list_users(nse["id"])
    assert listed == ["Alpha"]
    assert choose(host, None)[0] == 200 and host.config["symbol_list"] is None

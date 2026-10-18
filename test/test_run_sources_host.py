"""A Run's sources in the strategy host (blueprints/python_strategy.add_source).

A source is a pinned copy of a script trading its own Sandbox account at a
reference capital. It is scheduled like any strategy, handed its account's
key, its capital, its own state directory and its symbols, and kept out of
the trader's own list.
"""

import inspect
import json
from types import SimpleNamespace

import pytest
from flask import Flask, session

import restx_api  # noqa: F401 - imported first, as the app does, to settle its import cycle
from utils.strategy_mode import sandbox_key_for

REAL_KEY = "cd" * 32
TOKEN = "0a1b2c3d"
SCHEDULE = {"start": "09:20", "stop": "15:10", "days": ["mon", "tue"]}

app = Flask(__name__)
app.secret_key = "test-only"


@pytest.fixture
def host(monkeypatch, tmp_path):
    import blueprints.python_strategy as host

    calls = SimpleNamespace(scheduled=[], started=[], stopped=[], unscheduled=[], within=False)
    monkeypatch.setattr(host, "STRATEGY_CONFIGS", {})
    monkeypatch.setattr(host, "STRATEGIES_DIR", tmp_path / "scripts")
    monkeypatch.setattr(host, "SOURCE_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(host, "save_configs", lambda: True)
    monkeypatch.setattr(
        host,
        "schedule_strategy",
        lambda sid, start, stop=None, days=None: calls.scheduled.append((sid, start, stop)),
    )
    monkeypatch.setattr(host, "is_within_schedule_time", lambda sid: calls.within)
    monkeypatch.setattr(host, "scheduled_start_strategy", lambda sid: calls.started.append(sid))
    monkeypatch.setattr(
        host, "stop_strategy_process", lambda sid, keep_record=False: calls.stopped.append(sid)
    )
    monkeypatch.setattr(host, "unschedule_strategy", lambda sid: calls.unscheduled.append(sid))
    monkeypatch.setattr("database.auth_db.get_api_key_for_tradingview", lambda user: REAL_KEY)
    pinned = tmp_path / "pinned.py"
    pinned.write_text("import os\nKEY = os.environ['OPENALGO_API_KEY']\n")
    return SimpleNamespace(module=host, calls=calls, pinned=pinned)


def _add(host, host_id="src_abc", **overrides):
    fields = {
        "name": "Alpha #abc123",
        "script_file": host.pinned,
        "owner": "trader",
        "exchange": "NSE",
        "schedule": SCHEDULE,
        "symbols": ["TCS", "SBIN"],
        "account_token": TOKEN,
        "reference_capital": 10_000_000,
    }
    fields.update(overrides)
    host.module.add_source(host_id, **fields)
    return host.module.STRATEGY_CONFIGS[host_id]


def test_a_source_runs_a_pinned_copy_on_its_schedule(host):
    config = _add(host)
    copy = host.module.STRATEGIES_DIR / "src_abc.py"
    assert copy.read_text() == host.pinned.read_text()
    assert (config["kind"], config["mode"], config["file_path"]) == ("source", "sandbox", str(copy))
    assert host.calls.scheduled == [("src_abc", "09:20", "15:10")]
    assert host.calls.started == []  # outside its window: it waits for its start


def test_a_source_added_inside_its_window_starts_at_once(host):
    host.calls.within = True
    _add(host)
    assert host.calls.started == ["src_abc"]


def test_a_source_is_handed_its_account_capital_state_and_symbols(host):
    from services import symbol_list_service

    config = _add(host)
    script = host.module.STRATEGIES_DIR / "src_abc.py"
    key, refusal = host.module._strategy_api_key("src_abc", config, script)
    assert (key, refusal) == (sandbox_key_for(REAL_KEY, TOKEN), None)

    env = host.module._capital_env("src_abc", config)
    assert env["OPENALGO_STRATEGY_CAPITAL"] == "10000000.00"
    assert env["OPENALGO_STATE_DIR"].endswith("state/src_abc")
    assert (host.module.SOURCE_STATE_DIR / "src_abc").is_dir()
    assert symbol_list_service.strategy_env(config) == {"OPENALGO_SYMBOLS": "TCS,SBIN"}


def test_adding_a_source_again_keeps_what_it_is_doing(host):
    config = _add(host)
    config["is_running"] = True
    config["manually_stopped"] = True
    again = _add(host, reference_capital=20_000_000)
    assert again["is_running"] is True and "manually_stopped" not in again
    assert again["reference_capital"] == 20_000_000.0


def test_a_source_is_not_one_of_the_traders_strategies(host, monkeypatch):
    import services.research_hosted_service as hosted

    _add(host)
    host.module.STRATEGY_CONFIGS["mine"] = {
        "name": "Mine",
        "user_id": "trader",
        "is_running": False,
    }
    monkeypatch.setattr(host.module, "cleanup_dead_processes", lambda: None)

    listed = inspect.unwrap(host.module.api_get_strategies)
    with app.test_request_context():
        session["user"] = "trader"
        body = json.loads(listed().get_data())
        ok, (_response, status) = host.module.verify_strategy_ownership("src_abc", "trader")
    assert [s["id"] for s in body["strategies"]] == ["mine"]
    assert (ok, status) == (False, 409)
    assert list(hosted._host_configs()) == ["mine"]


def test_removing_a_source_stops_it_and_forgets_it(host):
    _add(host)
    host.module.remove_source("src_abc")
    assert host.calls.stopped == ["src_abc"] and host.calls.unscheduled == ["src_abc"]
    assert "src_abc" not in host.module.STRATEGY_CONFIGS
    assert not (host.module.STRATEGIES_DIR / "src_abc.py").exists()

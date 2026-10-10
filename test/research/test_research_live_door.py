"""
The Live door and the Live gate (master PRD Phase 9: sections 25, 31, 46, 56, 58).

No test here can reach a broker. ``order_dispatch.dispatch_order``,
``fetch_order_status`` and ``cancel_order`` are replaced by a recording fake
broker, and the one test that runs OpenAlgo's real live pipe replaces
``place_order_with_auth`` itself, to read what it was asked to do. What is
pinned: a live order goes only through the live pipe and is sent once;
Sandbox and Shadow sessions are refused by name; nothing adds to a position
unless the server allows Live, the kill switch is off, every version is
approved and the deployment's capital limit holds; an exit is always sent;
fills arrive as order updates and the broker is asked only about an order
gone quiet; a stale update never undoes a fill; a partly filled order reports
what filled; an order a crash cut off is reported unknown; and a live
deployment needs every one of its gates.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from events import OrderUpdateEvent


class FakeBroker:
    """Stands in for order_dispatch: records every call, answers like a broker."""

    def __init__(self):
        self.placed, self.cancelled, self.checked = [], [], []
        self.status = {}
        self.trades = []
        self.fail = None
        self.no_id = False

    def dispatch_order(self, *, mode, api_key, order):
        assert mode == "live"
        self.placed.append(order)
        if self.fail:
            return SimpleNamespace(ok=False, broker_order_id=None, error=self.fail)
        if self.no_id:
            return SimpleNamespace(ok=True, broker_order_id=None, error=None)
        orderid = f"B{len(self.placed)}"
        self.status[orderid] = {"order_status": "open"}
        return SimpleNamespace(ok=True, broker_order_id=orderid, error=None)

    def fetch_order_status(self, *, mode, api_key, broker_order_id):
        assert mode == "live"
        self.checked.append(broker_order_id)
        return SimpleNamespace(ok=True, order=self.status.get(broker_order_id), error=None)

    def cancel_order(self, *, mode, api_key, broker_order_id):
        assert mode == "live"
        self.cancelled.append(broker_order_id)
        self.status[broker_order_id] = {"order_status": "cancelled"}
        return SimpleNamespace(ok=True, broker_order_id=broker_order_id, error=None)


@pytest.fixture
def door(research_db, monkeypatch):
    import database.auth_db as auth_db
    import services.strategy_module.order_dispatch as order_dispatch
    import services.tradebook_service as tradebook
    from services import research_live_service as live
    from services import research_service as svc

    monkeypatch.setenv("RESEARCH_LIVE_ENABLED", "True")
    monkeypatch.setattr(auth_db, "verify_api_key", {"key-a": "trader", "key-b": "other"}.get)
    broker = FakeBroker()
    for name in ("dispatch_order", "fetch_order_status", "cancel_order"):
        monkeypatch.setattr(order_dispatch, name, getattr(broker, name))
    monkeypatch.setattr(order_dispatch, "resolve_live_auth", lambda key: ("token", "zerodha", None))
    monkeypatch.setattr(
        tradebook, "get_tradebook", lambda **kw: (True, {"data": broker.trades}, 200)
    )
    monkeypatch.setattr(svc, "dispatch", lambda: None)

    rdb = research_db
    svc.sync_registry()
    strategy = rdb.ResearchStrategy.query.first()
    version = rdb.ResearchStrategyVersion.query.filter_by(strategy_id=strategy.id).first()
    config, _ = svc.save_config(strategy.id, {"capital": 100000, "universe": ["AAA"]})
    exp = svc.create_experiment("live")["id"]
    scenario = svc.create_scenario(
        exp,
        {"name": "s", "mode": "ISOLATED", "start": "2024-01-01", "end": "2024-01-02",
         "instances": [{"strategy_version_id": version.id, "config_version_id": config["id"]}]},
    )  # fmt: skip
    session = rdb.db_session
    live.set_live_approval("trader", version.id, True)

    def run(environment="LIVE", status="RUNNING", capital_limit=200_000):
        deployment = rdb.ResearchDeployment(
            scenario_id=scenario["id"], name="d", user_id="trader", schedule_start="09:10",
            schedule_stop="15:25", schedule_days=["mon"], environment=environment,
            capital_limit=capital_limit,
        )  # fmt: skip
        session.add(deployment)
        session.flush()
        row = rdb.ResearchRun(scenario_id=scenario["id"], status=status, environment=environment,
                              deployment_id=deployment.id, trading_date="2026-10-12")  # fmt: skip
        session.add(row)
        session.commit()
        return row.id

    def order(run_id, seq, quantity, price=100.0, symbol="AAA"):
        return {"platform_order_id": f"research:{run_id}:{seq}", "symbol": symbol,
                "exchange": "NSE", "quantity": quantity, "product": "MIS",
                "reference_price": price}  # fmt: skip

    return SimpleNamespace(live=live, svc=svc, rdb=rdb, broker=broker, run=run, order=order,
                           version=version, scenario=scenario)  # fmt: skip


def _update(door, orderid, status, filled=0, price=0.0, mode="live"):
    """An order update as services/order_update_service.py publishes it.

    The handler's own logic, without its session release, which would close the
    session this test is still reading through.
    """
    door.live._on_order_update.__wrapped__(
        OrderUpdateEvent(orderid=orderid, order_status=status, filled_quantity=filled,
                         average_price=price, mode=mode)
    )  # fmt: skip


def _quiet(door, seconds):
    """Let every live order go without an update for ``seconds``."""
    past = door.rdb.utc_now() - timedelta(seconds=seconds)
    door.rdb.db_session.query(door.rdb.LiveOrder).update({"updated_at": past, "checked_at": None})
    door.rdb.db_session.commit()


def test_a_live_order_goes_to_the_live_pipe_once(door):
    run_id = door.run()

    first = door.live.place_live_order("key-a", run_id, door.order(run_id, 7, 500))
    again = door.live.place_live_order("key-a", run_id, door.order(run_id, 7, 500))

    assert first["created"] and not again["created"]
    assert first["order_status"] == "open" and first["orderid"] == "B1"
    (sent,) = door.broker.placed
    assert (sent["action"], sent["quantity"], sent["product"]) == ("BUY", "500", "MIS")
    _update(door, "B1", "complete", 500, 100.5)
    status = door.live.live_order_status("key-a", run_id, f"research:{run_id}:7")
    assert (status["order_status"], status["filled_quantity"], status["average_price"]) == (
        "complete", 500, 100.5
    )  # fmt: skip


def test_fills_arrive_as_order_updates_and_a_sandbox_update_is_not_one(door):
    """The strategy module's pattern: one subscription, nothing polls the broker."""
    run_id = door.run()
    door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 100))
    tag = f"research:{run_id}:1"

    _update(door, "B1", "complete", 100, 99.0, mode="analyze")
    assert door.live.live_order_status("key-a", run_id, tag)["order_status"] == "open"
    _update(door, "B1", "open", 40, 100.0)
    assert door.live.live_order_status("key-a", run_id, tag)["filled_quantity"] == 40
    _update(door, "B1", "COMPLETE", 0, 100.25)  # some brokers repeat no quantity when done
    status = door.live.live_order_status("key-a", run_id, tag)
    assert (status["order_status"], status["filled_quantity"], status["average_price"]) == (
        "complete", 100, 100.25
    )  # fmt: skip
    assert door.broker.checked == []


def test_a_quiet_order_is_checked_with_the_broker_at_most_every_few_seconds(door):
    """Targeted reconciliation, for a broker with no order-update stream."""
    run_id = door.run()
    door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 100))
    tag = f"research:{run_id}:1"
    door.broker.status["B1"] = {"order_status": "complete", "average_price": 101.0}

    assert door.live.live_order_status("key-a", run_id, tag)["order_status"] == "open"
    assert door.broker.checked == []
    _quiet(door, door.live.RECONCILE_SECONDS + 1)
    door.broker.status["B1"] = {"order_status": "open"}
    door.live.live_order_status("key-a", run_id, tag)
    door.live.live_order_status("key-a", run_id, tag)
    assert door.broker.checked == ["B1"]  # once, however often the session asks
    door.broker.status["B1"] = {"order_status": "complete", "average_price": 101.0}
    _quiet(door, door.live.RECONCILE_SECONDS + 1)
    status = door.live.live_order_status("key-a", run_id, tag)
    assert (status["order_status"], status["average_price"]) == ("complete", 101.0)


def test_a_stale_update_never_overwrites_the_fill_that_came_after_it(door):
    """Two writers on one order: update callbacks run in parallel under gthread.

    This writer read the order while it was open; the other filled it first.
    Written attribute by attribute, the part fill it read would land on top of
    the fill and the order would read complete with 40 of 100 filled.
    """
    run_id = door.run()
    door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 100))
    rdb = door.rdb
    key = (run_id, f"research:{run_id}:1")
    stale = rdb.db_session.get(rdb.LiveOrder, key)
    assert (stale.status, stale.filled_quantity) == ("OPEN", 0)
    other = rdb.db_session.session_factory()
    other.query(rdb.LiveOrder).update(
        {"status": "COMPLETE", "filled_quantity": 100, "average_price": 101.0}
    )
    other.commit()
    other.close()

    assert door.live._fold(stale, "open", 40, 100.0) is False
    rdb.db_session.expire_all()
    row = rdb.db_session.get(rdb.LiveOrder, key)
    assert (row.status, row.filled_quantity, row.average_price) == ("COMPLETE", 100, 101.0)


def test_an_order_the_broker_took_without_an_id_is_reported_unknown(door):
    run_id = door.run()
    door.broker.no_id = True
    status = door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 10))
    assert (
        status["order_status"] == "unknown"
        and "without an order id" in (status["rejection_reason"])
    )


def test_the_live_pipe_is_forced_live_whatever_the_analyzer_switch_says(monkeypatch):
    """The real order_dispatch: it must ask the placement service for force_live.

    place_order_with_auth reads the platform-wide analyzer switch first unless
    force_live is set, which would send a live session's exit to the sandbox.
    The placement module is replaced (importing it directly here is circular),
    so what is read is exactly what order_dispatch asked for.
    """
    import sys
    from types import ModuleType

    from services.strategy_module import order_dispatch

    seen = {}

    def capture(order, auth_token, broker, original, **kwargs):
        seen.update(kwargs, order=order)
        return True, {"status": "success", "orderid": "B9"}, 200

    placement = ModuleType("services.place_order_service")
    placement.place_order_with_auth = capture
    monkeypatch.setitem(sys.modules, "services.place_order_service", placement)
    monkeypatch.setattr(order_dispatch, "resolve_live_auth", lambda key: ("t", "zerodha", None))

    result = order_dispatch.dispatch_order(
        mode="live", api_key="k",
        order=order_dispatch.build_order(symbol="AAA", exchange="NSE", action="BUY", quantity=1,
                                         product="MIS", strategy_name="research 1"),
    )  # fmt: skip

    assert result.ok and result.broker_order_id == "B9"
    assert seen["force_live"] is True


@pytest.mark.parametrize("environment", ["SANDBOX", "SHADOW"])
def test_a_sandbox_or_shadow_session_cannot_use_the_live_door(door, environment):
    """Critical safety tests 2 and 3: neither can reach the broker, even by asking."""
    from services import research_sandbox_service as sandbox

    run_id = door.run(environment=environment)
    with pytest.raises(door.live.LiveRefused, match="not a live session"):
        door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 10))
    assert door.broker.placed == []
    if environment == "SHADOW":
        with pytest.raises(sandbox.SandboxRefused, match="not a Sandbox session"):
            sandbox.place_sandbox_order("key-a", run_id, door.order(run_id, 1, 10))


def test_a_live_session_belongs_to_its_user(door):
    run_id = door.run()
    with pytest.raises(door.live.LiveRefused, match="another user"):
        door.live.place_live_order("key-b", run_id, door.order(run_id, 1, 10))
    with pytest.raises(door.live.LiveRefused, match="not valid"):
        door.live.place_live_order("wrong", run_id, door.order(run_id, 1, 10))


def test_live_is_off_until_the_server_allows_it(door, monkeypatch):
    monkeypatch.delenv("RESEARCH_LIVE_ENABLED")
    run_id = door.run()
    with pytest.raises(door.live.LiveRefused, match="RESEARCH_LIVE_ENABLED"):
        door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 10))
    assert door.broker.placed == []


def _filled(door, run_id, seq, quantity):
    door.live.place_live_order("key-a", run_id, door.order(run_id, seq, quantity))
    _update(door, f"B{len(door.broker.placed)}", "complete", abs(quantity), 100.0)


def test_the_kill_switch_refuses_new_positions_but_always_sends_exits(door):
    run_id = door.run()
    _filled(door, run_id, 1, 500)
    door.live.set_kill_switch("trader", True, "testing")

    with pytest.raises(door.live.LiveRefused, match="kill switch"):
        door.live.place_live_order("key-a", run_id, door.order(run_id, 2, 100))
    with pytest.raises(door.live.LiveRefused, match="kill switch"):
        door.live.place_live_order("key-a", run_id, door.order(run_id, 3, -600))  # a flip adds
    exit_ = door.live.place_live_order("key-a", run_id, door.order(run_id, 4, -500))
    assert exit_["created"] and door.broker.placed[-1]["action"] == "SELL"


def test_engaging_the_kill_switch_stops_every_live_session(door):
    live_run, sandbox_run = door.run(), door.run(environment="SANDBOX")
    door.live.set_kill_switch("trader", True)
    session = door.rdb.db_session
    session.expire_all()
    assert session.get(door.rdb.ResearchRun, live_run).status == "STOPPING"
    assert session.get(door.rdb.ResearchRun, sandbox_run).status == "RUNNING"


def test_a_revoked_version_cannot_add_to_a_live_position(door):
    run_id = door.run()
    door.live.set_live_approval("trader", door.version.id, False)
    with pytest.raises(door.live.LiveRefused, match="no longer allowed"):
        door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 10))


def test_a_live_session_stays_within_its_capital_limit(door):
    run_id = door.run(capital_limit=60_000)
    _filled(door, run_id, 1, 500)  # Rs 50,000
    with pytest.raises(door.live.LiveRefused, match="over its Rs 60,000 limit"):
        door.live.place_live_order("key-a", run_id, door.order(run_id, 2, 200))
    assert door.live.place_live_order("key-a", run_id, door.order(run_id, 3, 100))["created"]


def test_a_cancelled_order_reports_what_filled_before_the_cancel(door):
    run_id = door.run()
    door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 100))
    door.broker.trades = [
        {"orderid": "B1", "quantity": 20, "average_price": 100.0},
        {"orderid": "B1", "quantity": 10, "average_price": 103.0},
        {"orderid": "B2", "quantity": 99, "average_price": 1.0},
    ]
    status = door.live.cancel_live_order("key-a", run_id, f"research:{run_id}:1")
    assert door.broker.cancelled == ["B1"]
    assert (status["order_status"], status["filled_quantity"]) == ("cancelled", 30)
    assert status["average_price"] == pytest.approx(101.0)


def test_a_refused_order_says_why_and_is_never_sent_again(door):
    run_id = door.run()
    door.broker.fail = "Broker session is not available or has expired"
    first = door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 10))
    again = door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 10))
    assert first["order_status"] == "rejected" and "expired" in first["rejection_reason"]
    assert not again["created"] and len(door.broker.placed) == 1


def test_an_order_a_crash_cut_off_is_reported_unknown_not_guessed(door):
    run_id = door.run()
    session = door.rdb.db_session
    session.add(
        door.rdb.LiveOrder(run_id=run_id, platform_order_id=f"research:{run_id}:1", symbol="AAA",
                           exchange="NSE", quantity=10, product="MIS", reference_price=100.0,
                           status="PLACING",
                           created_at=door.rdb.utc_now() - timedelta(minutes=5))
    )  # fmt: skip
    session.commit()
    status = door.live.live_order_status("key-a", run_id, f"research:{run_id}:1")
    assert status["order_status"] == "unknown" and "cut off" in status["rejection_reason"]
    assert (
        door.live.place_live_order("key-a", run_id, door.order(run_id, 1, 10))["created"] is False
    )


def test_a_live_deployment_passes_every_gate_and_a_typed_confirmation(door, monkeypatch):
    from services import research_sandbox_service as sandbox

    monkeypatch.setattr(sandbox, "_schedule", lambda row: None)
    rdb = door.rdb
    base = {"scenario_id": door.scenario["id"], "name": "Alpha live", "environment": "LIVE",
            "capital_limit": 150_000, "confirm": "Alpha live"}  # fmt: skip

    with pytest.raises(sandbox.SandboxRefused, match="Sandbox for at least one full trading day"):
        sandbox.create_deployment("trader", base)
    session = rdb.db_session
    session.add(rdb.ResearchRun(scenario_id=door.scenario["id"], status="COMPLETED",
                                environment="SANDBOX", trading_date="2026-10-09"))  # fmt: skip
    session.commit()
    with pytest.raises(sandbox.SandboxRefused, match='type the deployment\'s name, "Alpha live"'):
        sandbox.create_deployment("trader", {**base, "confirm": "yes"})
    with pytest.raises(sandbox.SandboxRefused, match="can hold up to Rs 100,000"):
        sandbox.create_deployment("trader", {**base, "capital_limit": 50_000})
    door.live.set_live_approval("trader", door.version.id, False)
    with pytest.raises(sandbox.SandboxRefused, match="Allow these strategy versions"):
        sandbox.create_deployment("trader", base)
    door.live.set_live_approval("trader", door.version.id, True)

    made = sandbox.create_deployment("trader", base)
    assert made["environment"] == "LIVE" and made["capital_limit"] == 150_000
    assert made["confirmed_at"]
    monkeypatch.setenv("RESEARCH_LIVE_ENABLED", "False")
    with pytest.raises(sandbox.SandboxRefused, match="RESEARCH_LIVE_ENABLED"):
        sandbox.create_deployment("trader", base)


def test_no_live_session_opens_while_the_kill_switch_is_on(door, monkeypatch):
    from services import research_sandbox_service as sandbox

    monkeypatch.setattr(sandbox, "_trading_today", lambda exchange: True)
    monkeypatch.setattr(sandbox, "_start_worker", lambda deployment, run: None)
    rdb = door.rdb
    deployment = rdb.ResearchDeployment(
        scenario_id=door.scenario["id"], name="d", user_id="trader", schedule_start="00:00",
        schedule_stop="23:59", schedule_days=list(sandbox.DAYS), environment="LIVE",
        capital_limit=200_000,
    )  # fmt: skip
    rdb.db_session.add(deployment)
    rdb.db_session.commit()
    door.live.set_kill_switch("trader", True)

    assert sandbox.open_session(deployment.id, scheduled=True) is None
    with pytest.raises(sandbox.SandboxRefused, match="kill switch is on"):
        sandbox.open_session(deployment.id, scheduled=False)
    door.live.set_kill_switch("trader", False)
    opened = sandbox.open_session(deployment.id, scheduled=False)
    assert opened["environment"] == "LIVE"

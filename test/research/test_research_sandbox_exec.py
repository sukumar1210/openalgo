"""
The Sandbox execution door (PRD Phase 8.4 and 8.5, UAT PAPER-006, PAPER-007,
PAPER-009).

These place real orders through OpenAlgo's sandbox ``OrderManager`` against
the test sandbox database, with only the live quote stubbed. What is pinned:
an order reaches the sandbox and nothing else, whatever the analyzer switch
says; a retried platform order id never creates a second sandbox order; and
only the deploying user's live session may place orders at all.
"""

import pytest

QUOTE = {
    "ltp": 2500.0,
    "bid": 2499.5,
    "ask": 2500.5,
    "open": 2490.0,
    "high": 2510.0,
    "low": 2480.0,
    "close": 2495.0,
    "volume": 1_000_000,
}


@pytest.fixture
def sandbox(research_db, monkeypatch):
    import database.auth_db as auth_db
    from database.sandbox_db import SandboxOrders
    from database.sandbox_db import db_session as sandbox_session
    from database.sandbox_db import init_db as init_sandbox
    from database.symbol import SymToken, init_db
    from database.symbol import db_session as symbol_session
    from sandbox.execution_engine import ExecutionEngine

    init_db()
    init_sandbox()
    if SymToken.query.filter_by(symbol="RELIANCE", exchange="NSE").first() is None:
        symbol_session.add(
            SymToken(
                symbol="RELIANCE", brsymbol="RELIANCE-EQ", name="RELIANCE INDUSTRIES",
                exchange="NSE", brexchange="NSE", token="2885", lotsize=1,
                instrumenttype="EQ", tick_size=0.05,
            )
        )  # fmt: skip
        symbol_session.commit()
    monkeypatch.setattr(auth_db, "verify_api_key", {"key-a": "trader", "key-b": "other"}.get)
    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: dict(QUOTE))

    session = research_db.db_session
    experiment = research_db.ResearchExperiment(name="x")
    session.add(experiment)
    session.flush()
    scenario = research_db.ResearchScenario(
        experiment_id=experiment.id, name="s", mode="ISOLATED", dataset_spec={},
        cost_model={}, execution_model={}, risk_policy={},
    )  # fmt: skip
    session.add(scenario)
    session.flush()
    deployment = research_db.ResearchDeployment(
        scenario_id=scenario.id, name="d", user_id="trader", schedule_start="09:10",
        schedule_stop="15:25", schedule_days=["mon"],
    )  # fmt: skip
    session.add(deployment)
    session.flush()
    run = research_db.ResearchRun(
        scenario_id=scenario.id, status="RUNNING", environment="SANDBOX",
        deployment_id=deployment.id, trading_date="2026-10-07",
    )  # fmt: skip
    session.add(run)
    session.commit()

    def ours():
        return SandboxOrders.query.filter(SandboxOrders.strategy.like(f"research:{run.id}:%")).all()

    from services import research_sandbox_service as svc

    def clean():
        from database.sandbox_db import SandboxFunds, SandboxPositions, SandboxTrades

        for model in (SandboxOrders, SandboxPositions, SandboxTrades, SandboxFunds):
            model.query.filter_by(user_id="trader").delete()
        sandbox_session.commit()

    clean()
    yield svc, run, ours
    clean()


def order(run_id, seq=1, qty=10):
    return {
        "platform_order_id": f"research:{run_id}:{seq}",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "quantity": qty,
        "product": "MIS",
    }


def test_an_order_fills_in_the_sandbox_and_a_retry_does_not_duplicate_it(sandbox):
    svc, run, ours = sandbox

    first = svc.place_sandbox_order("key-a", run.id, order(run.id))
    again = svc.place_sandbox_order("key-a", run.id, order(run.id))

    assert first["created"] and first["order_status"] == "complete"
    assert first["filled_quantity"] == 10 and first["average_price"] > 0
    assert not again["created"] and again["orderid"] == first["orderid"]
    assert len(ours()) == 1
    status = svc.sandbox_order_status("key-a", run.id, f"research:{run.id}:1")
    assert status["orderid"] == first["orderid"]
    assert svc.sandbox_order_status("key-a", run.id, f"research:{run.id}:99") is None


def test_the_analyzer_switch_cannot_route_a_session_order_to_a_broker(sandbox, monkeypatch):
    """PAPER-006: with the platform switched to live, the order still lands in the sandbox."""
    import sys
    import types

    import database.settings_db as settings_db

    svc, run, ours = sandbox
    monkeypatch.setattr(settings_db, "get_analyze_mode", lambda: False)

    class LiveOrderPath(types.ModuleType):
        def __getattr__(self, name):
            raise AssertionError(f"a Sandbox session order reached the live order path ({name})")

    for name in ("services.place_order_service", "services.place_smart_order_service"):
        monkeypatch.setitem(sys.modules, name, LiveOrderPath(name))

    result = svc.place_sandbox_order("key-a", run.id, order(run.id, seq=2, qty=-5))

    assert result["order_status"] == "complete"
    assert [o.action for o in ours()] == ["SELL"]


def test_only_the_deploying_users_live_session_may_place_orders(sandbox, research_db):
    svc, run, ours = sandbox

    with pytest.raises(svc.SandboxRefused, match="another user") as refused:
        svc.place_sandbox_order("key-b", run.id, order(run.id))
    assert refused.value.status == 403
    with pytest.raises(svc.SandboxRefused, match="not valid"):
        svc.place_sandbox_order("bogus", run.id, order(run.id))
    with pytest.raises(svc.SandboxRefused, match="does not belong"):
        svc.place_sandbox_order("key-a", run.id, order(run.id + 1))

    research_db.db_session.query(research_db.ResearchRun).filter_by(id=run.id).update(
        {"status": "COMPLETED"}
    )
    research_db.db_session.commit()
    with pytest.raises(svc.SandboxRefused, match="not running"):
        svc.place_sandbox_order("key-a", run.id, order(run.id))
    assert ours() == []


def test_an_order_left_open_is_reported_as_the_sandbox_sees_it(sandbox, monkeypatch):
    """With a position held and no live quote, the sandbox keeps a MARKET order
    open on its last known price and fills it only later, and only if analyzer
    mode is on. Cancelling it can itself be refused by the sandbox's margin
    arithmetic for a reducing order. Either way the status reported must be the
    sandbox's own, so the session keeps tracking the order instead of assuming
    it is gone."""
    from database.sandbox_db import SandboxOrders
    from sandbox.execution_engine import ExecutionEngine

    svc, run, ours = sandbox
    svc.place_sandbox_order("key-a", run.id, order(run.id, seq=4))
    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: None)

    left = svc.place_sandbox_order("key-a", run.id, order(run.id, seq=5, qty=-10))
    assert left["order_status"] == "open" and left["filled_quantity"] == 0

    after = svc.cancel_sandbox_order("key-a", run.id, f"research:{run.id}:5")
    stored = SandboxOrders.query.filter_by(orderid=left["orderid"]).populate_existing().one()
    assert after["order_status"] == stored.order_status
    assert svc.cancel_sandbox_order("key-a", run.id, f"research:{run.id}:6") is None


def test_a_refused_order_is_reported_and_creates_nothing(sandbox, monkeypatch):
    """PAPER-009: no quote, no fill; the refusal comes back as the sandbox's own sentence."""
    from sandbox.execution_engine import ExecutionEngine

    svc, run, ours = sandbox
    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: None)

    result = svc.place_sandbox_order("key-a", run.id, order(run.id, seq=3))

    assert result["order_status"] == "rejected" and result["orderid"] is None
    assert result["rejection_reason"]
    assert ours() == []

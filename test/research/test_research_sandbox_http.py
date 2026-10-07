"""
The worker and the app, over real HTTP, through the real sandbox (UAT PAPER-001,
PAPER-004 and PAPER-010 end to end).

A real HTTP server serves the actual ``/api/v1/researchsandbox`` namespace,
whose orders go through OpenAlgo's real sandbox ``OrderManager`` into the test
sandbox database. A real worker ``Session`` with the real ``AppClient`` trades
a whole day against it; only the market (ticks, the quote the sandbox prices
from) and the API-key check are stand-ins. This is what pins the contract
between the two halves: request and response shapes, idempotent placement, and
fills read back at the sandbox's own price.
"""

import threading

import pytest
from flask import Blueprint, Flask
from flask_restx import Api
from test_research_sandbox_exec import QUOTE
from test_research_sandbox_session import Clock, FakeFeed, at, reconciles, run_row
from werkzeug.serving import make_server

from research import strategy as rs


@pytest.fixture
def served(research_db, monkeypatch, tmp_path):
    import database.auth_db as auth_db
    from database.sandbox_db import SandboxFunds, SandboxOrders, SandboxPositions, SandboxTrades
    from database.sandbox_db import db_session as sandbox_session
    from database.sandbox_db import init_db as init_sandbox
    from database.symbol import SymToken, init_db
    from database.symbol import db_session as symbol_session
    from limiter import limiter
    from restx_api.research_sandbox import api as namespace
    from sandbox.execution_engine import ExecutionEngine

    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "data"))
    init_db()
    init_sandbox()
    if SymToken.query.filter_by(symbol="AAA", exchange="NSE").first() is None:
        symbol_session.add(
            SymToken(symbol="AAA", brsymbol="AAA-EQ", name="AAA", exchange="NSE", brexchange="NSE",
                     token="99001", lotsize=1, instrumenttype="EQ", tick_size=0.05)
        )  # fmt: skip
        symbol_session.commit()
    monkeypatch.setattr(auth_db, "verify_api_key", {"key-a": "trader"}.get)
    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: dict(QUOTE))

    def clean():
        for model in (SandboxOrders, SandboxPositions, SandboxTrades, SandboxFunds):
            model.query.filter_by(user_id="trader").delete()
        sandbox_session.commit()

    clean()
    app = Flask(__name__)
    bp = Blueprint("api_v1_test", __name__, url_prefix="/api/v1")
    Api(bp).add_namespace(namespace, path="/researchsandbox")
    app.register_blueprint(bp)
    limiter.init_app(app)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    specs = {
        "buyer": rs.StrategySpec(
            "buyer", "entry_point", "test_research_sandbox_session:Buyer", "t", "1"
        ),
        "seller": rs.StrategySpec(
            "seller", "entry_point", "test_research_sandbox_session:Seller", "t", "1"
        ),
    }
    monkeypatch.setattr(rs, "discover", lambda: specs)
    yield f"http://127.0.0.1:{server.server_port}", research_db, SandboxOrders
    server.shutdown()
    clean()


def deploy(rdb, keys, mode):
    from services import research_service as svc

    svc.sync_registry()
    exp = svc.create_experiment("http")["id"]
    pairs = []
    for key in keys:
        strategy = rdb.ResearchStrategy.query.filter_by(key=key).one()
        version = rdb.ResearchStrategyVersion.query.filter_by(strategy_id=strategy.id).one()
        saved, _ = svc.save_config(
            strategy.id,
            {"capital": 100000, "universe": ["AAA"], "entry_time": "09:15", "exit_time": "15:10"},
        )
        pairs.append({"strategy_version_id": version.id, "config_version_id": saved["id"]})
    scenario = svc.create_scenario(
        exp,
        {"name": "s", "mode": mode, "start": "2026-01-01", "end": "2026-01-02",
         "cost_model": {"preset": "india_intraday_nse", "slippage_bps": 0}, "instances": pairs},
    )  # fmt: skip
    session = rdb.db_session
    deployment = rdb.ResearchDeployment(
        scenario_id=scenario["id"], name="d", user_id="trader", schedule_start="09:10",
        schedule_stop="15:25", schedule_days=["wed"],
    )  # fmt: skip
    session.add(deployment)
    session.flush()
    run = rdb.ResearchRun(scenario_id=scenario["id"], status="QUEUED", environment="SANDBOX",
                          deployment_id=deployment.id, trading_date="2026-10-07")  # fmt: skip
    session.add(run)
    session.commit()
    return run.id


def run_day(base_url, run_id):
    from research.sandbox_worker import AppClient, Session

    clock = Clock(at("09:14"))
    session = Session(run_id, AppClient(base_url, "key-a"), FakeFeed(), clock=clock)
    assert session.claim()
    session.set_status("STARTING", "Starting")
    session.start()
    while clock.t < at("16:00"):
        session.on_tick("AAA", 2500.0, 1000, clock.t)
        if not session.step():
            break
        clock.t += 30
    return session


def test_a_day_of_real_sandbox_orders_over_http(served):
    base_url, rdb, SandboxOrders = served
    run_id = deploy(rdb, ["buyer"], "ISOLATED")

    run_day(base_url, run_id)

    run = run_row(rdb, run_id)
    assert run.status == "COMPLETED", run.message
    orders = SandboxOrders.query.filter(SandboxOrders.strategy.like(f"research:{run_id}:%")).all()
    assert sorted(o.action for o in orders) == ["BUY", "SELL"]
    assert all(o.order_status == "complete" for o in orders)
    fills = rdb.LedgerFill.query.filter_by(run_id=run_id).order_by(rdb.LedgerFill.seq).all()
    by_tag = {o.strategy: float(o.average_price) for o in orders}
    for fill in fills:
        assert fill.price == pytest.approx(by_tag[f"research:{run_id}:{fill.order_seq}"])
    assert fills[0].price == pytest.approx(QUOTE["ask"]) and fills[1].price == pytest.approx(
        QUOTE["bid"]
    )
    assert run.metrics["trades"] == 1 and run.metrics["net_pnl"] < 0, "paid the spread and costs"
    reconciles(rdb, run_id)


def test_opposite_intents_in_a_shared_session_send_nothing_to_the_sandbox(served):
    base_url, rdb, SandboxOrders = served
    run_id = deploy(rdb, ["buyer", "seller"], "SHARED")

    run_day(base_url, run_id)

    assert run_row(rdb, run_id).status == "COMPLETED"
    assert (
        SandboxOrders.query.filter(SandboxOrders.strategy.like(f"research:{run_id}:%")).count() == 0
    )
    signals = rdb.LedgerFill.query.filter_by(run_id=run_id, reason="signal").all()
    assert sorted(f.qty for f in signals) == [-40, 40]
    reconciles(rdb, run_id)

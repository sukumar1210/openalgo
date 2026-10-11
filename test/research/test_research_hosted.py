"""
Python strategies hosted at /python, followed by Research.

A followed script is never run by Research. What these tests pin is that its
own Sandbox trading becomes Research runs that mean what they say: only the
trades its orders tagged, plus the sandbox's square-off of what it held (and
no more than it held); one run per finished day, never two; the same cost model
its backtest charges; and every version of its file kept, so the code behind a
result can always be read back.
"""

from datetime import date, datetime

import pytest

SCRIPT = "ema_cross_20261001090000"
TAG = "EMA Crossover Python"
HOST = {
    SCRIPT: {
        "name": "EMA cross",
        "exchange": "NSE",
        "schedule_start": "09:00",
        "schedule_stop": "15:30",
        "is_running": False,
    }
}


@pytest.fixture
def sandbox(tmp_path):
    """sandbox.db on its own file, so nothing here touches a shared one."""
    from database import sandbox_db
    from database.engine_factory import create_db_engine

    engine = create_db_engine(f"sqlite:///{tmp_path / 'sandbox.db'}")
    sandbox_db.Base.metadata.create_all(engine)
    sandbox_db.db_session.remove()
    sandbox_db.db_session.configure(bind=engine)
    yield sandbox_db
    sandbox_db.db_session.remove()
    sandbox_db.db_session.configure(bind=sandbox_db.engine)
    engine.dispose()


@pytest.fixture
def hosted(research_db, sandbox, tmp_path, monkeypatch):
    from services import research_hosted_service as module

    scripts = tmp_path / "strategies" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / f"{SCRIPT}.py").write_text("print('v1')\n")
    monkeypatch.setattr(module, "SCRIPT_DIR", scripts)
    monkeypatch.setattr(module, "_host_configs", lambda: {k: dict(v) for k, v in HOST.items()})
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    return module


def trade(sandbox, strategy, symbol, action, quantity, price, at):
    session = sandbox.db_session
    count = session.query(sandbox.SandboxTrades).count()
    session.add(
        sandbox.SandboxTrades(
            tradeid=f"T{count}",
            orderid=f"O{count}",
            user_id="trader",
            symbol=symbol,
            exchange="NSE",
            action=action,
            quantity=quantity,
            price=price,
            product="MIS",
            strategy=strategy,
            trade_timestamp=at,
        )
    )
    session.commit()


def follow(hosted, **overrides):
    data = {"order_tag": TAG, "capital": 500000, "universe": ["SBIN", "INFY"], **overrides}
    return hosted.follow_script("trader", SCRIPT, data)


def test_following_a_script_makes_it_a_strategy_with_its_schedule_and_a_kept_file(
    hosted, research_db
):
    item = follow(hosted)

    session = research_db.db_session
    row = session.get(research_db.ResearchStrategy, item["strategy_id"])
    assert (row.key, row.source) == (SCRIPT, "script")
    config = session.query(research_db.ResearchConfigVersion).filter_by(strategy_id=row.id).one()
    assert (config.config["entry_time"], config.config["exit_time"]) == ("09:00", "15:30")
    version = session.query(research_db.ResearchStrategyVersion).filter_by(strategy_id=row.id).one()
    kept = hosted.version_copy(version.code_hash, version.dist_name)
    assert kept.read_text() == "print('v1')\n"

    # Editing the script in /python is a new version; the old copy stays.
    hosted.script_path(SCRIPT).write_text("print('v2')\n")
    hosted.sync_scripts(session)
    versions = session.query(research_db.ResearchStrategyVersion).filter_by(strategy_id=row.id)
    assert versions.count() == 2
    assert kept.read_text() == "print('v1')\n"


def test_a_day_records_its_own_trades_and_only_the_square_off_of_what_it_held(hosted, sandbox):
    follow(hosted)
    day = datetime(2026, 10, 9)
    trade(sandbox, TAG, "SBIN", "BUY", 10, 800.0, day.replace(hour=9, minute=16))
    trade(sandbox, "Someone else", "SBIN", "BUY", 50, 801.0, day.replace(hour=9, minute=20))
    # The sandbox squares off its net 60 SBIN; the script held 10 of them.
    trade(sandbox, "AUTO_SQUARE_OFF", "SBIN", "SELL", 60, 810.0, day.replace(hour=15, minute=15))
    trade(sandbox, "AUTO_SQUARE_OFF", "INFY", "SELL", 5, 1500.0, day.replace(hour=15, minute=15))

    fills = hosted.tagged_fills(TAG, date(2026, 10, 9))

    assert [(f.symbol, f.qty, f.reason) for f in fills] == [
        ("SBIN", 10, "signal"),
        ("SBIN", -10, "square_off"),
    ]


def test_each_finished_day_is_recorded_once_with_the_scenarios_costs(hosted, sandbox, research_db):
    follow(hosted)
    for day in (datetime(2026, 10, 8), datetime(2026, 10, 9)):
        trade(sandbox, TAG, "SBIN", "BUY", 10, 800.0, day.replace(hour=9, minute=16))
        trade(sandbox, TAG, "SBIN", "SELL", 10, 810.0, day.replace(hour=11, minute=0))

    first = hosted.record_hosted(now=datetime(2026, 10, 10, 9, 0))
    again = hosted.record_hosted(now=datetime(2026, 10, 10, 9, 0))

    assert len(first) == 2 and again == []
    session = research_db.db_session
    run = session.get(research_db.ResearchRun, first[0])
    assert (run.environment, run.status, run.trading_date) == ("HOSTED", "COMPLETED", "2026-10-08")
    [trip] = session.query(research_db.LedgerTrade).filter_by(run_id=run.id).all()
    assert trip.gross_pnl == pytest.approx(100.0)
    # The scenario's intraday cost model was charged, as a backtest of it would be.
    assert trip.fees > 0
    assert run.metrics["net_pnl"] == pytest.approx(100.0 - trip.fees - trip.slippage)


def test_today_waits_until_the_script_has_stopped_for_the_day(hosted, sandbox):
    follow(hosted)
    today = datetime(2026, 10, 12)
    trade(sandbox, TAG, "SBIN", "BUY", 10, 800.0, today.replace(hour=9, minute=16))

    assert hosted.record_hosted(now=today.replace(hour=14)) == []
    HOST[SCRIPT]["is_running"] = True
    try:
        assert hosted.record_hosted(now=today.replace(hour=16)) == []
    finally:
        HOST[SCRIPT]["is_running"] = False
    assert len(hosted.record_hosted(now=today.replace(hour=16))) == 1


def test_a_followed_script_runs_alone_and_cannot_be_deployed(hosted, research_db):
    from services import research_service as svc
    from services.research_sandbox_service import SandboxRefused, check_deployable

    item = follow(hosted)
    with pytest.raises(svc.ResearchError, match="places its own exits"):
        svc.save_config(
            item["strategy_id"],
            {
                "capital": 500000,
                "universe": ["SBIN"],
                "entry_time": "09:00",
                "exit_time": "15:30",
                "stop_pct": 1.0,
            },
        )
    scenario = svc.quick_scenario(item["strategy_id"], {})
    row = research_db.db_session.get(research_db.ResearchScenario, scenario["id"])
    with pytest.raises(SandboxRefused, match="Python Strategies page"):
        check_deployable(row)


def test_sandbox_and_research_tags_are_not_offered_as_a_scripts_tag(hosted, sandbox):
    session = sandbox.db_session
    for n, tag in enumerate((TAG, "AUTO_SQUARE_OFF", "research:4:1", TAG)):
        session.add(
            sandbox.SandboxOrders(
                orderid=f"O{n}",
                user_id="trader",
                strategy=tag,
                symbol="SBIN",
                exchange="NSE",
                action="BUY",
                quantity=1,
                price_type="MARKET",
                product="MIS",
                order_status="complete",
                pending_quantity=0,
                filled_quantity=1,
            )
        )
    session.commit()

    assert [(t["tag"], t["orders"]) for t in hosted.order_tags()] == [(TAG, 2)]
    with pytest.raises(hosted.HostedError, match="sandbox's own orders"):
        follow(hosted, order_tag="AUTO_SQUARE_OFF")

"""
Research as part of Python Strategies: every script followed, with no settings of
its own.

A Python strategy has one set of settings, its own in Python Strategies, and
trades with the sandbox's money. What these tests pin: every script is
followed without being asked; its orders are recognised by the names they
carry, learned from when it was running and never guessed when two scripts
ran together; each finished Sandbox day becomes one run, once; and a backtest
runs on the script's own schedule, weekdays and exchange and the sandbox's
capital, never on a second configuration.
"""

import os
from datetime import date, datetime

import pytest

SCRIPT = "ema_cross_20261001090000"
OTHER = "breakout_20261001090000"
TAG = "EMA Crossover Python"


def host_config(name, **overrides):
    return {
        "name": name,
        "exchange": "NSE",
        "schedule_start": "09:00",
        "schedule_stop": "15:30",
        "schedule_days": ["mon", "tue", "wed", "thu", "fri"],
        "is_running": False,
        **overrides,
    }


HOST = {SCRIPT: host_config("EMA cross"), OTHER: host_config("Breakout")}


@pytest.fixture
def sandbox(tmp_path):
    """sandbox.db on its own file, so nothing here touches a shared one."""
    from database import sandbox_db
    from database.engine_factory import create_db_engine

    engine = create_db_engine(f"sqlite:///{tmp_path / 'sandbox.db'}")
    sandbox_db.Base.metadata.create_all(engine)
    sandbox_db.db_session.remove()
    sandbox_db.db_session.configure(bind=engine)
    session = sandbox_db.db_session
    for key, value in (("starting_capital", "500000"), ("equity_mis_leverage", "5")):
        session.add(sandbox_db.SandboxConfig(config_key=key, config_value=value))
    session.commit()
    yield sandbox_db
    sandbox_db.db_session.remove()
    sandbox_db.db_session.configure(bind=sandbox_db.engine)
    engine.dispose()


@pytest.fixture
def hosted(research_db, sandbox, tmp_path, monkeypatch):
    from services import research_hosted_service as module

    scripts = tmp_path / "strategies" / "scripts"
    scripts.mkdir(parents=True)
    for script in HOST:
        (scripts / f"{script}.py").write_text(f"print('{script} v1')\n")
    logs = tmp_path / "log" / "strategies"
    logs.mkdir(parents=True)
    monkeypatch.setattr(module, "SCRIPT_DIR", scripts)
    monkeypatch.setattr(module, "LOG_DIR", logs)
    monkeypatch.setattr(module, "_host_configs", lambda: {k: dict(v) for k, v in HOST.items()})
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    module.fetched = []
    monkeypatch.setattr(module, "_fetch_bars", lambda fills, ex, day: module.fetched.append(day))
    return module


def ran(hosted, script, began: datetime, ended: datetime):
    """Leave a run log the way the strategy host does, ending when it last wrote."""
    path = hosted.LOG_DIR / f"{script}_{began:%Y%m%d_%H%M%S}_IST.log"
    path.write_text("started\n")
    when = hosted.IST.localize(ended).timestamp()
    os.utime(path, (when, when))


def order(sandbox, tag, at, symbol="SBIN"):
    session = sandbox.db_session
    n = session.query(sandbox.SandboxOrders).count()
    session.add(
        sandbox.SandboxOrders(
            orderid=f"O{n}", user_id="trader", strategy=tag, symbol=symbol, exchange="NSE",
            action="BUY", quantity=1, price_type="MARKET", product="MIS",
            order_status="complete", pending_quantity=0, filled_quantity=1, order_timestamp=at,
        )
    )  # fmt: skip
    session.commit()


def trade(sandbox, strategy, symbol, action, quantity, price, at):
    session = sandbox.db_session
    n = session.query(sandbox.SandboxTrades).count()
    session.add(
        sandbox.SandboxTrades(
            tradeid=f"T{n}", orderid=f"O{n}", user_id="trader", symbol=symbol, exchange="NSE",
            action=action, quantity=quantity, price=price, product="MIS", strategy=strategy,
            trade_timestamp=at,
        )
    )  # fmt: skip
    session.commit()


def link_of(research_db, script):
    return (
        research_db.db_session.query(research_db.ResearchScriptLink)
        .filter_by(script_id=script)
        .one()
    )


def test_every_script_is_followed_without_being_asked_and_its_versions_are_kept(
    hosted, research_db
):
    session = research_db.db_session
    hosted.sync_scripts(session)

    link = link_of(research_db, SCRIPT)
    row = session.get(research_db.ResearchStrategy, link.strategy_id)
    assert (row.key, row.source, link.name) == (SCRIPT, "script", "EMA cross")
    version = session.query(research_db.ResearchStrategyVersion).filter_by(strategy_id=row.id).one()
    kept = hosted.version_copy(version.code_hash, version.dist_name)
    assert kept.read_text() == f"print('{SCRIPT} v1')\n"

    hosted.script_path(SCRIPT).write_text("print('v2')\n")
    hosted.sync_scripts(session)
    assert (
        session.query(research_db.ResearchStrategyVersion).filter_by(strategy_id=row.id).count()
        == 2
    )
    assert kept.read_text() == f"print('{SCRIPT} v1')\n"


def test_its_orders_are_learned_from_when_it_ran_and_never_guessed(hosted, sandbox, research_db):
    day = datetime(2026, 10, 9)
    ran(hosted, SCRIPT, day.replace(hour=9), day.replace(hour=15, minute=10))
    ran(hosted, OTHER, day.replace(hour=13), day.replace(hour=14))
    order(sandbox, TAG, day.replace(hour=9, minute=16))
    order(sandbox, "SHARED NAME", day.replace(hour=13, minute=30))  # both were running
    order(sandbox, "AUTO_SQUARE_OFF", day.replace(hour=15, minute=15))
    order(sandbox, "Manual", day.replace(hour=16))  # after it stopped

    hosted.record_hosted(now=datetime(2026, 10, 10, 9, 0))

    assert link_of(research_db, SCRIPT).learned_tags == [TAG]
    assert link_of(research_db, OTHER).learned_tags == []


def test_each_finished_day_is_recorded_once_with_the_sandboxs_money(hosted, sandbox, research_db):
    for day in (datetime(2026, 10, 8), datetime(2026, 10, 9)):
        ran(hosted, SCRIPT, day.replace(hour=9), day.replace(hour=15, minute=30))
        order(sandbox, TAG, day.replace(hour=9, minute=16))
        trade(sandbox, TAG, "SBIN", "BUY", 10, 800.0, day.replace(hour=9, minute=16))
        trade(sandbox, TAG, "SBIN", "SELL", 10, 810.0, day.replace(hour=11))

    first = hosted.record_hosted(now=datetime(2026, 10, 10, 9, 0))
    again = hosted.record_hosted(now=datetime(2026, 10, 10, 9, 0))

    assert len(first) == 2 and again == []
    session = research_db.db_session
    run = session.get(research_db.ResearchRun, first[0])
    assert (run.environment, run.trading_date) == ("HOSTED", "2026-10-08")
    [trip] = session.query(research_db.LedgerTrade).filter_by(run_id=run.id).all()
    assert trip.gross_pnl == pytest.approx(100.0) and trip.fees > 0
    assert run.metrics["capital"] == 500000  # the sandbox's own capital
    config = session.get(
        research_db.ResearchConfigVersion, run.provenance["instances"][0]["config_version_id"]
    )
    assert config.config == {
        "capital": 500000.0, "leverage": 5.0, "entry_time": "09:00", "exit_time": "15:30",
        "exchange": "NSE", "days": ["mon", "tue", "wed", "thu", "fri"],
    }  # fmt: skip


def test_a_square_off_counts_only_up_to_what_the_script_held(hosted, sandbox):
    day = datetime(2026, 10, 9)
    trade(sandbox, TAG, "SBIN", "BUY", 10, 800.0, day.replace(hour=9, minute=16))
    trade(sandbox, "Someone else", "SBIN", "BUY", 50, 801.0, day.replace(hour=9, minute=20))
    trade(sandbox, "AUTO_SQUARE_OFF", "SBIN", "SELL", 60, 810.0, day.replace(hour=15, minute=15))

    fills = hosted.tagged_fills([TAG], date(2026, 10, 9))

    assert [(f.qty, f.reason) for f in fills] == [(10, "signal"), (-10, "square_off")]


def test_today_waits_until_the_script_has_stopped_for_the_day(hosted, sandbox):
    today = datetime(2026, 10, 12)
    ran(hosted, SCRIPT, today.replace(hour=9), today.replace(hour=10))
    order(sandbox, TAG, today.replace(hour=9, minute=16))
    trade(sandbox, TAG, "SBIN", "BUY", 10, 800.0, today.replace(hour=9, minute=16))

    assert hosted.record_hosted(now=today.replace(hour=14)) == []
    HOST[SCRIPT]["is_running"] = True
    try:
        assert hosted.record_hosted(now=today.replace(hour=16)) == []
    finally:
        HOST[SCRIPT]["is_running"] = False
    assert len(hosted.record_hosted(now=today.replace(hour=16))) == 1


def test_a_backtest_runs_on_the_scripts_own_settings_and_history_before_it(
    hosted, sandbox, research_db, monkeypatch
):
    from services import research_service as svc

    monkeypatch.setattr(svc, "dispatch", lambda: None)
    day = datetime(2026, 10, 9)
    ran(hosted, SCRIPT, day.replace(hour=9), day.replace(hour=15))
    order(sandbox, TAG, day.replace(hour=9, minute=16))
    trade(sandbox, TAG, "INFY", "BUY", 1, 1500.0, day.replace(hour=9, minute=16))
    hosted.record_hosted(now=datetime(2026, 10, 10, 9, 0))

    run = hosted.backtest("trader", SCRIPT, "2026-09-01", "2026-09-30")

    session = research_db.db_session
    scenario = session.get(research_db.ResearchScenario, run["scenario_id"])
    assert scenario.execution_model == {"fill": "script_replay", "trade_from": "2026-09-01"}
    assert scenario.dataset_spec["start"] == "2026-07-18"  # 45 days of history first
    assert scenario.dataset_spec["symbols"] == ["INFY"]  # what it is known to trade
    assert session.get(research_db.ResearchRun, run["id"]).provenance == {"requested_by": "trader"}
    with pytest.raises(hosted.HostedError, match="past days only"):
        hosted.backtest("trader", SCRIPT, "2026-09-01", datetime.now().date().isoformat())


def test_the_strategys_tabs_read_its_backtests_days_and_totals(hosted, sandbox):
    day = datetime(2026, 10, 9)
    ran(hosted, SCRIPT, day.replace(hour=9), day.replace(hour=15))
    order(sandbox, TAG, day.replace(hour=9, minute=16))
    trade(sandbox, TAG, "SBIN", "BUY", 10, 800.0, day.replace(hour=9, minute=16))
    trade(sandbox, TAG, "SBIN", "SELL", 10, 790.0, day.replace(hour=11))

    item = hosted.overview(SCRIPT)

    assert item["order_tags"] == [TAG] and item["settings"]["capital"] == 500000
    [day_row] = item["sandbox_days"]
    assert day_row["trading_date"] == "2026-10-09" and day_row["net_pnl"] < -100
    assert item["summary"]["days"] == 1 and item["summary"]["winning_days"] == 0
    with pytest.raises(hosted.HostedError, match="not in Python Strategies"):
        hosted.overview("nope")


def test_python_strategies_stay_out_of_the_research_registry(hosted, research_db):
    """They have their own tabs at /python and settings the registry page cannot show."""
    from services import research_service as svc

    hosted.sync_scripts(research_db.db_session)

    keys = {item["key"] for item in svc.list_strategies()["items"]}
    assert SCRIPT not in keys and OTHER not in keys
    assert "first_bar_leader" in keys


def test_a_day_without_minute_bars_has_no_drawdown_until_they_arrive(
    hosted, sandbox, research_db, monkeypatch
):
    """Marked only at its fills, a winning day would read as never having been down."""
    import pandas as pd

    from research.dataset import normalise
    from research.fills import fill_frames

    day = datetime(2026, 10, 9)
    ran(hosted, SCRIPT, day.replace(hour=9), day.replace(hour=15, minute=30))
    order(sandbox, TAG, day.replace(hour=9, minute=16))
    trade(sandbox, TAG, "SBIN", "BUY", 100, 800.0, day.replace(hour=9, minute=16))
    trade(sandbox, TAG, "SBIN", "SELL", 100, 810.0, day.replace(hour=15, minute=10))

    [run_id] = hosted.record_hosted(now=datetime(2026, 10, 10, 9, 0))
    session = research_db.db_session
    run = session.get(research_db.ResearchRun, run_id)
    assert run.provenance["marks"] == "fills" and run.metrics["max_drawdown_pct"] is None
    assert hosted.fetched == [date(2026, 10, 9)]  # its bars are asked for once
    assert hosted.record_hosted(now=datetime(2026, 10, 10, 9, 5)) == []  # still waiting

    def stored(fills, exchange, d):
        """Minute bars for the day: the stock fell to 780 at 11:00 before closing higher."""
        frames = fill_frames(fills)
        dip = pd.DataFrame({"timestamp": [hosted._epoch(day.replace(hour=11))], "open": [780.0],
                            "high": [780.0], "low": [780.0], "close": [780.0], "volume": [0.0]})  # fmt: skip
        frames["SBIN"] = normalise(pd.concat([frames["SBIN"], dip]))
        return frames, "historify"

    monkeypatch.setattr(hosted, "_frames_for", stored)
    assert hosted.record_hosted(now=datetime(2026, 10, 10, 15, 50)) == [run_id]  # same run

    session.expire_all()
    run = session.get(research_db.ResearchRun, run_id)
    assert run.provenance["marks"] == "historify"
    assert run.metrics["net_pnl"] > 0 and run.metrics["max_drawdown_pct"] < -0.3  # down Rs 2,000
    assert session.query(research_db.LedgerTrade).filter_by(run_id=run_id).count() == 1


def test_open_sandbox_positions_are_the_ones_it_still_holds(hosted, sandbox, monkeypatch):
    """What stops a strategy leaving Sandbox mode with a Sandbox position open."""
    day = datetime(2026, 10, 9)
    ran(hosted, SCRIPT, day.replace(hour=9), day.replace(hour=15, minute=30))
    order(sandbox, TAG, day.replace(hour=9, minute=16))
    trade(sandbox, TAG, "SBIN", "BUY", 10, 800.0, day.replace(hour=9, minute=16))
    trade(sandbox, TAG, "SBIN", "SELL", 10, 810.0, day.replace(hour=11))
    trade(sandbox, TAG, "INFY", "BUY", 5, 1500.0, day.replace(hour=12))  # still held

    [held] = hosted.open_sandbox_positions(SCRIPT)

    assert (held["symbol"], held["quantity"]) == ("INFY", 5)
    assert hosted.open_sandbox_positions(OTHER) == []  # it never traded

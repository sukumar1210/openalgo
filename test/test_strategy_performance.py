"""The strategy dashboard: every strategy's Sandbox trading, alone and as one account.

What these pin: order names are grouped into the strategy that sent them (a
Python strategy's old and new names into one row, a strategy-module strategy by
its name, anything else on its own, Research's sessions and the square-off
never as rows); each row's daily P&L is the sandbox's own rule, square-off
included; the combined curve adds the chosen strategies day by day.
"""

from datetime import datetime

import pytest

from services import strategy_performance_service as perf

USER = "trader"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from database import sandbox_db
    from database.engine_factory import create_db_engine

    engine = create_db_engine(f"sqlite:///{tmp_path / 'sandbox.db'}")
    sandbox_db.Base.metadata.create_all(engine)
    sandbox_db.db_session.remove()
    sandbox_db.db_session.configure(bind=engine)
    monkeypatch.setattr(
        perf, "_python_strategies", lambda: [("alpha_1", "Alpha", {"alpha_obs_1", "Alpha"})]
    )
    monkeypatch.setattr(perf, "_module_strategies", lambda user: [(7, "Iron fly")])
    import services.research_hosted_service as hosted

    monkeypatch.setattr(hosted, "sandbox_money", lambda: (100000.0, 5.0))
    yield sandbox_db
    sandbox_db.db_session.remove()
    sandbox_db.db_session.configure(bind=sandbox_db.engine)
    engine.dispose()


def trade(sandbox, strategy, action, qty, price, day, hour, symbol="SBIN"):
    session = sandbox.db_session
    n = session.query(sandbox.SandboxTrades).count()
    session.add(
        sandbox.SandboxTrades(
            tradeid=f"T{n}", orderid=f"O{n}", user_id=USER, symbol=symbol, exchange="NSE",
            action=action, quantity=qty, price=price, product="MIS", strategy=strategy,
            trade_timestamp=datetime(2026, 10, day, hour),
        )
    )  # fmt: skip
    session.commit()


@pytest.fixture
def book(sandbox):
    trade(sandbox, "alpha_obs_1", "BUY", 10, 100.0, 8, 9)  # Alpha under its old name
    trade(sandbox, "alpha_obs_1", "SELL", 10, 110.0, 8, 14)  # +100
    trade(sandbox, "Alpha", "BUY", 10, 100.0, 9, 9)  # Alpha under its own name
    trade(sandbox, "AUTO_SQUARE_OFF", "SELL", 10, 95.0, 9, 15)  # squared off: -50
    trade(sandbox, "Iron fly", "SELL", 5, 200.0, 9, 9, symbol="INFY")
    trade(sandbox, "Iron fly", "BUY", 5, 180.0, 9, 14, symbol="INFY")  # +100
    trade(sandbox, "webhook", "BUY", 1, 50.0, 9, 10, symbol="TCS")
    trade(sandbox, "research:3:x", "BUY", 1, 50.0, 9, 10, symbol="TCS")
    return sandbox


def test_order_names_are_grouped_into_the_strategy_that_sent_them(book):
    rows = perf.strategies(USER, {t["strategy"] for t in perf.sandbox_trades(USER)})

    assert [(r["key"], r["kind"], r["tags"]) for r in rows] == [
        ("python:alpha_1", "python", ["Alpha", "alpha_obs_1"]),
        ("module:7", "module", ["Iron fly"]),
        ("name:webhook", "other", ["webhook"]),
    ]


def test_each_strategys_days_and_the_combined_account(book):
    data = perf.performance(USER, combine=["python:alpha_1", "module:7"])

    alpha = next(r for r in data["strategies"] if r["key"] == "python:alpha_1")
    assert [(d["date"], d["pnl"], d["cumulative"]) for d in alpha["daily"]] == [
        ("2026-10-08", 100.0, 100.0),
        ("2026-10-09", -50.0, 50.0),  # its share of the square-off counted
    ]
    assert alpha["summary"]["max_drawdown"] == -50.0 and alpha["summary"]["winning_days"] == 1
    assert alpha["summary"]["max_drawdown_pct"] == pytest.approx(-50 / 100100 * 100)
    combined = data["combined"]
    assert [(d["date"], d["pnl"]) for d in combined["daily"]] == [
        ("2026-10-08", 100.0),
        ("2026-10-09", 50.0),  # -50 + 100
    ]
    assert combined["summary"]["total_pnl"] == 150.0
    assert {c["name"]: c["total_pnl"] for c in combined["contributions"]} == {
        "Alpha": 50.0,
        "Iron fly": 100.0,
    }


def test_a_window_counts_only_its_own_days(book):
    data = perf.performance(USER, start="2026-10-09", end="2026-10-09")

    alpha = next(r for r in data["strategies"] if r["key"] == "python:alpha_1")
    assert alpha["summary"]["total_pnl"] == -50.0 and alpha["summary"]["days"] == 1
    assert data["combined"] is None


def test_a_ledger_shows_its_own_fills_and_its_share_of_the_square_off(book):
    fills = perf.ledger(USER, "python:alpha_1")

    assert [(f["action"], f["quantity"], f["square_off"], f["order_name"]) for f in fills] == [
        ("SELL", 10, True, "AUTO_SQUARE_OFF"),
        ("BUY", 10, False, "Alpha"),
        ("SELL", 10, False, "alpha_obs_1"),
        ("BUY", 10, False, "alpha_obs_1"),
    ]
    assert perf.ledger(USER, "python:nope") == []


def test_a_name_two_strategies_could_claim_is_counted_once(book, monkeypatch):
    """Counted twice, the same fills would sit in two rows and twice in a combined curve."""
    monkeypatch.setattr(perf, "_module_strategies", lambda user: [(7, "Iron fly"), (8, "Alpha")])

    rows = perf.strategies(USER, {t["strategy"] for t in perf.sandbox_trades(USER)})

    owners = [tag for row in rows for tag in row["tags"]]
    assert sorted(owners) == sorted(set(owners))
    assert "module:8" not in {row["key"] for row in rows}  # the Python strategy learned it

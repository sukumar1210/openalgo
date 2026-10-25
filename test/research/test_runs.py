"""Runs: strategies trading together with their own money (services/run_service.py).

The host is recorded rather than run, so no script starts; everything else is
real: the research database, the Sandbox accounts, follows and transfers.
"""

import uuid
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from database.sandbox_db import (
    SandboxAccount,
    SandboxAccountEvent,
    SandboxCopy,
    SandboxFollow,
    SandboxFunds,
    SandboxOrders,
    SandboxPositions,
    SandboxTrades,
    db_session,
    init_db,
)
from sandbox import accounts

DAY1 = date(2026, 10, 15)
DAY2 = date(2026, 10, 16)
SCRIPT = "print('a strategy')\n"


def _config(name, start="09:20"):
    return {
        "name": name,
        "exchange": "NSE",
        "schedule_start": start,
        "schedule_stop": "15:10",
        "schedule_days": ["mon", "tue", "wed", "thu", "fri"],
    }


@pytest.fixture
def world(research_db, tmp_path, monkeypatch):
    """A research database, two scripts at /python, and a recorded host."""
    import blueprints.python_strategy as host
    import services.research_hosted_service as hosted
    from services import run_service

    monkeypatch.setenv("SANDBOX_ACCOUNTS_ENABLED", "true")
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "data"))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for script_id in ("alpha", "beta"):
        (scripts / f"{script_id}.py").write_text(SCRIPT + f"# {script_id}\n")
    monkeypatch.setattr(hosted, "SCRIPT_DIR", scripts)
    configs = {"alpha": _config("Alpha"), "beta": _config("Beta")}
    monkeypatch.setattr(hosted, "_host_configs", lambda: {k: dict(v) for k, v in configs.items()})
    monkeypatch.setattr("database.auth_db.get_api_key_for_tradingview", lambda user: "key")
    monkeypatch.setattr(run_service, "_is_trading_day", lambda day: day.weekday() < 5)
    monkeypatch.setattr(run_service, "_today", lambda: DAY1)  # the day the tests trade

    calls = {"add": [], "remove": []}
    monkeypatch.setattr(
        host, "add_source", lambda host_id, **kw: calls["add"].append((host_id, kw))
    )
    monkeypatch.setattr(host, "remove_source", lambda host_id: calls["remove"].append(host_id))

    init_db()
    accounts._info.clear()
    owner = f"runs-{uuid.uuid4().hex[:6]}"
    world = type("World", (), {"owner": owner, "calls": calls, "configs": configs})
    yield world
    principals = [owner] + [a.principal for a in SandboxAccount.query.filter_by(owner=owner)]
    follows = SandboxFollow.query.filter(SandboxFollow.follower.in_(principals)).all()
    SandboxCopy.query.filter(SandboxCopy.follow_id.in_([f.id for f in follows])).delete(
        synchronize_session=False
    )
    for model in (SandboxFollow,):
        model.query.filter(model.follower.in_(principals)).delete(synchronize_session=False)
    for model in (SandboxOrders, SandboxTrades, SandboxPositions, SandboxFunds):
        model.query.filter(model.user_id.in_(principals)).delete(synchronize_session=False)
    SandboxAccountEvent.query.filter(SandboxAccountEvent.principal.in_(principals)).delete(
        synchronize_session=False
    )
    SandboxAccount.query.filter_by(owner=owner).delete()
    db_session.commit()
    accounts._info.clear()


def _members(**allocations):
    """Members by script id: alpha=("fixed", 300000) and so on."""
    return [
        {"script_id": sid, "allocation_kind": kind, "allocation_value": value}
        for sid, (kind, value) in allocations.items()
    ]


def _create(world, name="Trial", mode="isolated", policy="own", limit="capital", members=None):
    from services import run_service

    return run_service.create(
        world.owner,
        name,
        capital=500000,
        mode=mode,
        policy=policy,
        limit=limit,
        members=_members(alpha=("fixed", 300000), beta=("percent", 40))
        if members is None
        else members,
    )


def _start(world, run, monkeypatch, day=DAY1):
    """Start a Run on ``day`` before the close, with the engines left alone."""
    from services import run_service

    monkeypatch.setattr("sandbox.engines.ensure_running", lambda: None)
    monkeypatch.setattr(run_service, "_today", lambda: day)

    class Clock(run_service.datetime):
        @classmethod
        def now(cls, tz=None):
            return run_service.datetime(day.year, day.month, day.day, 10, 0, tzinfo=tz)

    monkeypatch.setattr(run_service, "datetime", Clock)
    return run_service.start(run["id"], world.owner)


def _follows(run_id):
    db_session.expire_all()
    return (
        SandboxFollow.query.filter_by(run_id=run_id, active=True).order_by(SandboxFollow.id).all()
    )


# --- defining ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"mode": "shared", "policy": "own"}, "cannot be kept apart"),
        ({"members": [{"script_id": "gone", "allocation_kind": "fixed", "allocation_value": 1}]},
         "no longer exists"),
        ({"members": _members(alpha=("fixed", 0))}, "allocation"),
        ({"members": []}, "at least one"),
        ({"name": ""}, "name"),
    ],
)  # fmt: skip
def test_a_run_that_cannot_trade_is_refused_with_the_reason(world, change, says):
    from services.run_service import RunError

    with pytest.raises(RunError, match=says):
        _create(world, **change)


def test_a_member_is_frozen_on_its_version_and_configuration(world):
    run = _create(world)
    assert run["status"] == "draft"
    alpha, beta = run["version"]["members"]
    assert (alpha["allocated"], beta["allocated"]) == (300000.0, 200000.0)
    assert alpha["settings"]["schedule"]["start"] == "09:20"
    assert alpha["order_name"] == "Trial / Alpha"
    assert alpha["fingerprint"] != beta["fingerprint"]


def test_identical_configurations_share_one_fingerprint_whatever_their_capital(world):
    one = _create(world, name="One", members=_members(alpha=("fixed", 100000)))
    two = _create(world, name="Two", members=_members(alpha=("fixed", 900000)))
    assert (
        one["version"]["members"][0]["fingerprint"] == two["version"]["members"][0]["fingerprint"]
    )
    world.configs["alpha"]["schedule_start"] = "09:30"
    three = _create(world, name="Three", members=_members(alpha=("fixed", 100000)))
    assert (
        three["version"]["members"][0]["fingerprint"] != one["version"]["members"][0]["fingerprint"]
    )


def test_a_run_name_is_used_once(world):
    from services.run_service import RunError

    _create(world)
    with pytest.raises(RunError, match="already exists"):
        _create(world)


# --- starting and days -----------------------------------------------------------


def test_starting_opens_each_member_an_account_and_follows_its_source(world, monkeypatch):
    run = _start(world, _create(world), monkeypatch)
    assert run["status"] == "active"
    alpha, beta = run["version"]["members"]
    principals = run["accounts"]
    assert set(principals) == {alpha["seat"], beta["seat"]}
    assert accounts.equity(principals[alpha["seat"]]) == Decimal("300000.00")
    assert accounts.equity(principals[beta["seat"]]) == Decimal("200000.00")

    added = [host_id for host_id, _kw in world.calls["add"]]
    assert added == [f"src_{alpha['fingerprint'][:12]}", f"src_{beta['fingerprint'][:12]}"]
    follows = _follows(run["id"])
    assert [(f.order_name, Decimal(f.scale)) for f in follows] == [
        ("Trial / Alpha", Decimal("0.03")),  # Rs 3 lakh of a Rs 1 crore source
        ("Trial / Beta", Decimal("0.02")),
    ]
    assert [Decimal(f.limit_amount) for f in follows] == [
        Decimal("300000.00"),
        Decimal("200000.00"),
    ]


def test_two_runs_with_one_configuration_share_its_source(world, monkeypatch):
    one = _start(
        world, _create(world, name="One", members=_members(alpha=("fixed", 100000))), monkeypatch
    )
    two = _start(
        world, _create(world, name="Two", members=_members(alpha=("fixed", 200000))), monkeypatch
    )
    sources = {f.source for f in _follows(one["id"]) + _follows(two["id"])}
    assert len(sources) == 1


def test_opening_a_day_twice_repeats_nothing(world, monkeypatch):
    import database.research_db as rdb
    from services import run_service

    run = _start(world, _create(world), monkeypatch)
    run_service.open_day(run["id"], DAY1)
    assert rdb.db_session.query(rdb.StrategyRunDay).filter_by(run_id=run["id"]).count() == 2
    assert SandboxAccount.query.filter_by(owner=world.owner, kind="member").count() == 2


@pytest.mark.parametrize(
    ("policy", "alpha", "beta"),
    [("reset", "300000", "200000"), ("own", "310000", "180000"), ("pooled", "294000", "196000")],
)
def test_day_two_opens_by_the_policy_as_recorded_transfers(world, monkeypatch, policy, alpha, beta):
    from services import run_service

    run = _start(world, _create(world, policy=policy), monkeypatch)
    seats = [m["seat"] for m in run["version"]["members"]]
    a, b = (run["accounts"][s] for s in seats)
    # Day one's result, as the accounts would hold it after trading.
    accounts.move_money(a, 10000, "deposit", reason="day one's profit")
    accounts.move_money(b, -20000, "withdraw", reason="day one's loss")
    run_service.close_day(run["id"], DAY1)

    run_service.open_day(run["id"], DAY2)
    assert (accounts.equity(a), accounts.equity(b)) == (Decimal(alpha), Decimal(beta))
    run_service.open_day(run["id"], DAY2)  # and only once
    assert (accounts.equity(a), accounts.equity(b)) == (Decimal(alpha), Decimal(beta))


def _limited(world, mode="isolated", policy="own"):
    from services import run_service

    return run_service.create(
        world.owner, f"Limited, {mode}", capital=500000, mode=mode, policy=policy, limit="capital",
        members=_members(alpha=("fixed", 300000), beta=("percent", 40)), loss_limit_pct=2,
    )  # fmt: skip


def test_each_account_opens_the_day_with_its_loss_limit(world, monkeypatch):
    """2% of what each opened with: a member's own when isolated, the whole Run's when shared."""
    from services import run_service

    run = _start(world, _limited(world), monkeypatch)
    alpha, beta = (run["accounts"][m["seat"]] for m in run["version"]["members"])
    assert (accounts.daily_loss_limit(alpha), accounts.daily_loss_limit(beta)) == (
        Decimal("6000.00"),
        Decimal("4000.00"),
    )
    accounts.move_money(alpha, 10000, "deposit", reason="day one's profit")
    run_service.close_day(run["id"], DAY1)
    run_service.open_day(run["id"], DAY2)
    assert accounts.daily_loss_limit(alpha) == Decimal("6200.00")  # on its 3,10,000

    shared = _start(world, _limited(world, mode="shared", policy="pooled"), monkeypatch)
    assert accounts.daily_loss_limit(shared["accounts"]["run"]) == Decimal("10000.00")


def test_a_run_with_no_loss_limit_sets_none(world, monkeypatch):
    run = _start(world, _create(world), monkeypatch)
    assert [accounts.daily_loss_limit(p) for p in run["accounts"].values()] == [None, None]


def test_a_shared_run_trades_one_account_split_by_member(world, monkeypatch):
    run = _start(world, _create(world, mode="shared", policy="pooled"), monkeypatch)
    assert list(run["accounts"]) == ["run"]
    assert accounts.equity(run["accounts"]["run"]) == Decimal("500000.00")
    follows = _follows(run["id"])
    assert {f.follower for f in follows} == {run["accounts"]["run"]}
    assert all(f.shared for f in follows)
    assert [Decimal(f.scale) for f in follows] == [Decimal("0.03"), Decimal("0.02")]


def test_a_closed_day_is_booked_from_the_account(world, monkeypatch):
    from services import run_service

    run = _start(world, _create(world), monkeypatch)
    seat = run["version"]["members"][0]["seat"]
    accounts.move_money(run["accounts"][seat], 1500, "deposit", reason="the day's result")
    run_service.close_day(run["id"], DAY1)
    day = next(d for d in run_service.get(run["id"], world.owner)["days"] if d["seat"] == seat)
    assert (day["closed"], day["net"], day["charges"]) == (True, 1500.0, 0.0)


# --- changing course ------------------------------------------------------------


def test_pausing_stops_the_copies_and_starting_again_resumes_them(world, monkeypatch):
    from services import run_service

    run = _start(world, _create(world), monkeypatch)
    run_service.pause(run["id"], world.owner)
    assert _follows(run["id"]) == []
    run_service.start(run["id"], world.owner)
    assert len(_follows(run["id"])) == 2


def test_the_kill_switch_closes_what_every_account_holds(world, monkeypatch):
    from services import run_service

    flattened = []
    monkeypatch.setattr(
        "sandbox.squareoff_manager.SquareOffManager._flatten_mis",
        lambda self, user_id: flattened.append(user_id),
    )
    run = _start(world, _create(world), monkeypatch)
    killed = run_service.kill(run["id"], world.owner)
    assert killed["status"] == "paused"
    assert sorted(flattened) == sorted(run["accounts"].values())
    assert _follows(run["id"]) == []


def test_an_edit_applies_from_the_next_trading_day(world, monkeypatch):
    from services import run_service

    run = _start(world, _create(world), monkeypatch)
    edited = run_service.edit(
        run["id"], world.owner, capital=500000, mode="isolated", policy="own", limit="capital",
        members=_members(alpha=("fixed", 300000)),
    )  # fmt: skip
    assert len(edited["version"]["members"]) == 2  # today keeps what it started with
    assert edited["upcoming"]["effective_from"] == DAY2.isoformat()

    run_service.open_day(run["id"], DAY2)
    assert [f.order_name for f in _follows(run["id"])] == ["Trial / Alpha"]


def test_sources_no_run_needs_are_retired(world, monkeypatch):
    from services import run_service

    run = _start(world, _create(world), monkeypatch)
    run_service.stop(run["id"], world.owner)
    run_service.retire_sources(DAY2)
    assert len(world.calls["remove"]) == 2


def test_only_a_run_that_never_traded_can_be_deleted(world, monkeypatch):
    from services import run_service
    from services.run_service import RunError

    draft = _create(world, name="Draft")
    run_service.delete(draft["id"], world.owner)
    traded = _start(world, _create(world, name="Traded"), monkeypatch)
    with pytest.raises(RunError):
        run_service.delete(traded["id"], world.owner)


def test_runs_are_refused_while_accounts_are_off(world, monkeypatch):
    from services.run_service import RunError

    monkeypatch.delenv("SANDBOX_ACCOUNTS_ENABLED")
    with pytest.raises(RunError, match="SANDBOX_ACCOUNTS_ENABLED"):
        _create(world)


# --- end to end ---------------------------------------------------------------


def test_a_source_trading_reaches_its_member_scaled_and_the_day_books_it(world, monkeypatch):
    """The whole chain: a Run's source trades, its member copies it, the close books it."""
    import sandbox.order_manager as order_manager
    from sandbox import copy_engine
    from sandbox.execution_engine import ExecutionEngine
    from services import run_service

    quote = {"ltp": 100.0, "high": 101.0, "low": 99.0}
    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: dict(quote))
    real = order_manager.datetime

    class AtEleven(real):
        @classmethod
        def now(cls, tz=None):
            moment = real.now(order_manager.pytz.timezone("Asia/Kolkata")).replace(
                hour=11, minute=0
            )
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(order_manager, "datetime", AtEleven)
    # Trades and charges carry the real date, so the Run trades the real today.
    today = real.now(order_manager.pytz.timezone("Asia/Kolkata")).date()
    monkeypatch.setattr(run_service, "_is_trading_day", lambda day: True)
    monkeypatch.setattr(run_service, "_today", lambda: today)
    run = _start(
        world, _create(world, members=_members(alpha=("fixed", 300000))), monkeypatch, day=today
    )
    follow = _follows(run["id"])[0]
    member = run["accounts"][run["version"]["members"][0]["seat"]]

    def source_trades(action, quantity):
        ok, response, _ = order_manager.OrderManager(follow.source).place_order(
            {"symbol": "RELIANCE", "exchange": "NSE", "action": action, "quantity": quantity,
             "price_type": "MARKET", "product": "MIS", "strategy": "Alpha #abc123"}
        )  # fmt: skip
        assert ok, response
        copy_engine.wait_idle()

    def held():
        db_session.expire_all()
        row = SandboxPositions.query.filter_by(user_id=member, symbol="RELIANCE").first()
        return row.quantity if row else 0

    source_trades("BUY", 1000)
    assert held() == 30  # Rs 3 lakh of a Rs 1 crore source
    source_trades("SELL", 1000)
    assert held() == 0

    run_service.close_day(run["id"], today)
    day = run_service.get(run["id"], world.owner)["days"][0]
    assert day["trades"] == 2 and day["charges"] > 0
    assert day["net"] == pytest.approx(-day["charges"])  # bought and sold at one price


def test_a_versions_definition_refuses_to_be_edited(world):
    """A version is what a day traded on; an edit adds a version instead."""
    import database.research_db as rdb

    run = _create(world)
    session = rdb.db_session
    version = session.query(rdb.StrategyRunVersion).filter_by(run_id=run["id"]).one()
    version.capital = 1
    with pytest.raises(rdb.ImmutableRecordError):
        session.commit()
    session.rollback()
    member = session.query(rdb.StrategyRunMember).filter_by(version_id=version.id).first()
    member.allocation_value = 1
    with pytest.raises(rdb.ImmutableRecordError):
        session.commit()
    session.rollback()


# --- the API the Runs pages call ----------------------------------------------


def _call(view, method="GET", body=None, **kwargs):
    import inspect

    from flask import Flask, session

    app = Flask(__name__)
    app.secret_key = "test-only"
    with app.test_request_context(method=method, json=body):
        session["user"] = _call.owner
        response, status = inspect.unwrap(view)(**kwargs)
    return status, response.get_json()


def test_the_api_creates_starts_and_refuses_in_the_traders_words(world, monkeypatch):
    from blueprints import runs

    _call.owner = world.owner
    body = {
        "name": "From the page", "capital": 500000, "mode": "isolated", "policy": "own",
        "limit": "capital", "members": _members(alpha=("fixed", 300000)),
    }  # fmt: skip
    status, created = _call(runs.create_run, "POST", body)
    assert status == 201 and created["data"]["status"] == "draft"
    run_id = created["data"]["id"]

    status, refused = _call(runs.create_run, "POST", body)
    assert status == 400 and "already exists" in refused["message"]

    _start(world, created["data"], monkeypatch)
    status, listed = _call(runs.list_runs)
    assert [r["status"] for r in listed["data"]] == ["active"]
    status, paused = _call(runs.act, "POST", run_id=run_id, action="pause")
    assert paused["data"]["status"] == "paused"
    assert _call(runs.act, "POST", run_id=run_id, action="launch")[0] == 404
    assert _call(runs.delete_run, "DELETE", run_id=run_id)[0] == 400


# --- the P&L page's data ---------------------------------------------------------


def test_a_runs_pnl_adds_its_seats_and_the_combined_adds_the_runs(world, monkeypatch):
    from services import run_service

    run = _start(world, _create(world), monkeypatch)
    a, b = (run["accounts"][m["seat"]] for m in run["version"]["members"])
    accounts.move_money(a, 1500, "deposit", reason="the day's result")
    accounts.move_money(b, -500, "withdraw", reason="the day's result")
    run_service.close_day(run["id"], DAY1)

    data = run_service.pnl(world.owner, runs=[run["id"]])
    (series,) = data["series"]
    assert [(d["date"], d["net"], d["opening"]) for d in series["daily"]] == [
        (DAY1.isoformat(), 1000.0, 500000.0)
    ]
    assert series["summary"]["total_pnl"] == 1000.0
    assert data["combined"]["daily"][0]["net"] == 1000.0


def test_today_is_read_live_from_the_accounts_before_the_close(world, monkeypatch):
    from services import run_service

    run = _start(world, _create(world), monkeypatch)
    seat = run["version"]["members"][0]["seat"]
    accounts.move_money(run["accounts"][seat], 700, "deposit", reason="so far today")
    (series,) = run_service.pnl(world.owner, members=[(run["id"], seat)])["series"]
    assert [d["net"] for d in series["daily"]] == [700.0]


def test_the_window_bounds_the_days(world, monkeypatch):
    from services import run_service

    run = _start(world, _create(world), monkeypatch)
    run_service.close_day(run["id"], DAY1)
    later = run_service.pnl(world.owner, runs=[run["id"]], start=DAY2.isoformat())
    assert later["series"][0]["daily"] == []


def test_a_shared_runs_strategies_are_split_by_their_order_names(world, monkeypatch):
    """Inside one account, each strategy's own orders, charged by the account's schedule."""
    import sandbox.order_manager as order_manager
    from sandbox import copy_engine
    from sandbox.execution_engine import ExecutionEngine
    from services import run_service

    quote = {"ltp": 100.0, "high": 101.0, "low": 99.0}
    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: dict(quote))
    real = order_manager.datetime

    class AtEleven(real):
        @classmethod
        def now(cls, tz=None):
            moment = real.now(order_manager.pytz.timezone("Asia/Kolkata")).replace(
                hour=11, minute=0
            )
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(order_manager, "datetime", AtEleven)
    today = real.now(order_manager.pytz.timezone("Asia/Kolkata")).date()
    monkeypatch.setattr(run_service, "_is_trading_day", lambda day: True)
    monkeypatch.setattr(run_service, "_today", lambda: today)
    run = _start(world, _create(world, mode="shared", policy="pooled"), monkeypatch, day=today)
    for follow in _follows(run["id"]):
        ok, _response, _ = order_manager.OrderManager(follow.source).place_order(
            {"symbol": "RELIANCE", "exchange": "NSE", "action": "BUY", "quantity": 1000,
             "price_type": "MARKET", "product": "MIS", "strategy": "source"}
        )  # fmt: skip
        assert ok
    copy_engine.wait_idle()

    seats = [m["seat"] for m in run["version"]["members"]]
    data = run_service.pnl(world.owner, members=[(run["id"], s) for s in seats], runs=[run["id"]])
    whole, alpha, beta = data["series"]  # Runs first, then their strategies
    assert [d["trades"] for d in alpha["daily"]] == [1]  # Alpha's 30 shares, Beta's 20
    assert [d["trades"] for d in beta["daily"]] == [1]
    assert alpha["daily"][0]["charges"] > 0 and beta["daily"][0]["charges"] > 0
    assert [alpha["daily"][0]["opening"], beta["daily"][0]["opening"]] == [300000.0, 200000.0]
    assert whole["daily"][0]["trades"] == 2
    assert data["combined"]["daily"][0]["trades"] == 4  # everything asked for, added up


def _trade(principal, strategy, action, quantity, price, day):
    db_session.add(
        SandboxTrades(
            tradeid=uuid.uuid4().hex, orderid=uuid.uuid4().hex, user_id=principal,
            symbol="RELIANCE", exchange="NSE", action=action, quantity=quantity,
            price=Decimal(str(price)), product="MIS", strategy=strategy,
            trade_timestamp=datetime.combine(day, time(11, 0)),
        )
    )  # fmt: skip
    db_session.commit()


def test_the_main_account_is_everything_it_traded_manual_orders_too(world, monkeypatch):
    from services import run_service

    _trade(world.owner, "", "BUY", 10, 100, DAY1)  # placed by hand: no order name
    _trade(world.owner, "", "SELL", 10, 110, DAY1)
    _trade(world.owner, "Alpha", "BUY", 5, 100, DAY1)
    _trade(world.owner, "Alpha", "SELL", 5, 90, DAY1)
    (series,) = run_service.pnl(world.owner, main=True)["series"]
    assert (series["key"], series["kind"]) == ("main", "main")
    (day,) = series["daily"]
    assert (day["pnl"], day["trades"]) == (50.0, 4)
    assert day["net"] == pytest.approx(50.0 - day["charges"]) and day["charges"] > 0


def test_a_shared_members_capital_each_day_is_its_share_that_day(world, monkeypatch):
    """An edit that changes an allocation changes the share from its day, not before."""
    from services import run_service

    run = _start(world, _create(world, mode="shared", policy="pooled"), monkeypatch)
    alpha = run["version"]["members"][0]
    run_service.edit(
        run["id"], world.owner, capital=500000, mode="shared", policy="pooled", limit="capital",
        members=_members(alpha=("fixed", 100000), beta=("percent", 40)),
    )  # fmt: skip
    run_service.close_day(run["id"], DAY1)
    run_service.open_day(run["id"], DAY2)
    for day in (DAY1, DAY2):
        _trade(run["accounts"]["run"], alpha["order_name"], "BUY", 10, 100, day)
        _trade(run["accounts"]["run"], alpha["order_name"], "SELL", 10, 101, day)
    monkeypatch.setattr(run_service, "_today", lambda: DAY2)
    (series,) = run_service.pnl(world.owner, members=[(run["id"], alpha["seat"])])["series"]
    # 3,00,000 of 5,00,000 allocated on day one; 1,00,000 of 3,00,000 on day two.
    assert [d["opening"] for d in series["daily"]] == pytest.approx([300000.0, 500000 / 3])
    assert [d["pnl"] for d in series["daily"]] == [10.0, 10.0]


def test_the_pnl_api_refuses_a_date_it_cannot_read(world):
    from blueprints import runs

    _call.owner = world.owner
    import inspect

    from flask import Flask, session

    app = Flask(__name__)
    app.secret_key = "test-only"
    with app.test_request_context("/runs/api/pnl?from=yesterday"):
        session["user"] = world.owner
        response, status = inspect.unwrap(runs.pnl)()
    assert status == 400 and "2026-01-31" in response.get_json()["message"]


@pytest.mark.parametrize(("mode", "policy"), [("isolated", "own"), ("shared", "pooled")])
def test_a_simulated_day_books_as_the_sandbox_does(world, monkeypatch, mode, policy):
    """The same source fills, through the Sandbox and through portfolio/run_sim, to the paise.

    At several prices and in pieces, so positions average, reduce and close
    partly: the Sandbox keeps an average to the paise and realizes against it.
    """
    from collections import defaultdict

    import database.research_db as rdb
    import sandbox.order_manager as order_manager
    from portfolio import run_sim
    from sandbox import copy_engine
    from sandbox.execution_engine import ExecutionEngine
    from services import run_service

    quote = {"ltp": 100.0, "high": 300.0, "low": 1.0}
    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: dict(quote))
    real = order_manager.datetime

    class AtEleven(real):
        @classmethod
        def now(cls, tz=None):
            moment = real.now(order_manager.pytz.timezone("Asia/Kolkata")).replace(
                hour=11, minute=0
            )
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(order_manager, "datetime", AtEleven)
    today = real.now(order_manager.pytz.timezone("Asia/Kolkata")).date()
    monkeypatch.setattr(run_service, "_is_trading_day", lambda day: True)
    monkeypatch.setattr(run_service, "_today", lambda: today)
    from database.symbol import SymToken
    from database.symbol import db_session as symbols

    if SymToken.query.filter_by(symbol="ZEEL", exchange="NSE").first() is None:
        symbols.add(
            SymToken(symbol="ZEEL", brsymbol="ZEEL-EQ", name="ZEE ENTERTAINMENT", exchange="NSE",
                     brexchange="NSE", token="3812", lotsize=1, instrumenttype="EQ",
                     tick_size=0.05, contract_value=1.0)
        )  # fmt: skip
        symbols.commit()
    run = _start(world, _create(world, mode=mode, policy=policy), monkeypatch, day=today)
    alpha, beta = run["version"]["members"]
    source_of = {f.order_name: f.source for f in _follows(run["id"])}

    def trade(member, action, quantity, symbol, price):
        quote["ltp"] = price
        ok, response, _ = order_manager.OrderManager(source_of[member["order_name"]]).place_order(
            {"symbol": symbol, "exchange": "NSE", "action": action, "quantity": quantity,
             "price_type": "MARKET", "product": "MIS", "strategy": "source"}
        )  # fmt: skip
        assert ok, response
        copy_engine.wait_idle()

    trade(alpha, "BUY", 1000, "RELIANCE", 100.05)
    trade(alpha, "BUY", 2000, "RELIANCE", 100.10)
    trade(beta, "SELL", 500, "RELIANCE", 100.15)
    trade(beta, "BUY", 1000, "ZEEL", 200.00)
    trade(alpha, "SELL", 1500, "RELIANCE", 100.20)
    trade(beta, "BUY", 500, "RELIANCE", 100.00)
    trade(beta, "SELL", 1000, "ZEEL", 201.50)
    trade(alpha, "SELL", 1500, "RELIANCE", 99.95)
    run_service.close_day(run["id"], today)
    rdb.db_session.expire_all()
    booked = {
        row.seat: (round(row.net, 2), round(row.charges, 2), row.trades)
        for row in rdb.db_session.query(rdb.StrategyRunDay).filter_by(run_id=run["id"])
    }

    # The sources' fills in the order they happened, as a replay records them.
    ten = (today - date(1970, 1, 1)).days * 86_400 - 19_800 + 10 * 3600
    fills = defaultdict(list)
    for n, t in enumerate(
        SandboxTrades.query.filter(SandboxTrades.user_id.in_(set(source_of.values()))).order_by(
            SandboxTrades.id
        )
    ):
        qty = t.quantity if t.action == "BUY" else -t.quantity
        fills[t.user_id].append((ten + n, t.symbol, qty, float(t.price), "signal"))
    sources = {
        s.fingerprint: run_sim.Source(s.reference_capital, "NSE", fills[s.principal])
        for s in rdb.db_session.query(rdb.StrategyRunSource)
        if s.principal in set(source_of.values())
    }
    version = run_service.version_in_force(run["id"], today)
    simulated = run_sim.simulate(run_service.sim_definition(version), sources)["seats"]
    assert {
        seat: (round(r["net"], 2), round(r["charges"], 2), r["trades"])
        for seat, (r,) in simulated.items()
    } == booked
    assert all(trades for _net, _charges, trades in booked.values())


def test_a_netted_square_off_does_not_carry_into_the_next_day(world, monkeypatch):
    """Alpha long 30 and Beta short 20 net to 10, so the square-off sells only 10."""
    from portfolio.strategy_pnl import SQUARE_OFF_TAG
    from services import run_service

    run = _start(world, _create(world, mode="shared", policy="pooled"), monkeypatch)
    alpha, beta = run["version"]["members"]
    account = run["accounts"]["run"]
    _trade(account, alpha["order_name"], "BUY", 30, 100, DAY1)
    _trade(account, beta["order_name"], "SELL", 20, 101, DAY1)
    _trade(account, SQUARE_OFF_TAG, "SELL", 10, 102, DAY1)
    run_service.close_day(run["id"], DAY1)
    run_service.open_day(run["id"], DAY2)
    _trade(account, alpha["order_name"], "BUY", 10, 110, DAY2)
    _trade(account, alpha["order_name"], "SELL", 10, 115, DAY2)
    monkeypatch.setattr(run_service, "_today", lambda: DAY2)
    (series,) = run_service.pnl(world.owner, members=[(run["id"], alpha["seat"])])["series"]
    assert series["daily"][-1]["pnl"] == 50.0  # day two is its own round trip, nothing older


# --- backtests: each configuration replayed once, the Run simulated over them -------


def _replays(backtest):
    import database.research_db as rdb

    return [rdb.db_session.get(rdb.ResearchRun, r["research_run_id"]) for r in backtest["replays"]]


def test_a_backtest_replays_each_configuration_once_at_its_reference_capital(world, monkeypatch):
    import database.research_db as rdb
    from services import research_service as svc
    from services import run_service

    monkeypatch.setattr(svc, "dispatch", lambda: None)
    first = run_service.backtest(_create(world)["id"], world.owner, "2026-09-01", "2026-09-30")
    assert first["status"] == "running" and len(first["replays"]) == 2  # Alpha and Beta
    for replay in _replays(first):
        scenario = rdb.db_session.get(rdb.ResearchScenario, replay.scenario_id)
        assert scenario.execution_model == {"fill": "script_replay", "trade_from": "2026-09-01"}
        (instance,) = rdb.db_session.query(rdb.ResearchScenarioInstance).filter_by(
            scenario_id=scenario.id
        )
        config = rdb.db_session.get(rdb.ResearchConfigVersion, instance.config_version_id).config
        assert (config["capital"], config["fixed_capital"]) == (10_000_000, True)
        assert (config["entry_time"], config["exit_time"]) == ("09:20", "15:10")

    # Another Run of the same configurations over the same days: the same replays.
    second = run_service.backtest(
        _create(world, name="Trial two")["id"], world.owner, "2026-09-01", "2026-09-30"
    )
    assert [r["research_run_id"] for r in second["replays"]] == [
        r["research_run_id"] for r in first["replays"]
    ]


def test_a_finished_backtest_is_the_run_simulated_over_its_replays(world, monkeypatch):
    """No Sandbox account is touched: the days are portfolio/run_sim's, over the fills."""
    import database.research_db as rdb
    from portfolio import run_sim
    from services import research_service as svc
    from services import run_service

    monkeypatch.setattr(svc, "dispatch", lambda: None)
    run = _create(world)
    started = run_service.backtest(run["id"], world.owner, "2026-09-01", "2026-09-30")
    alpha_replay, beta_replay = _replays(started)
    alpha_source, beta_source = (r["fingerprint"] for r in started["replays"])
    ten = (date(2026, 9, 1) - date(1970, 1, 1)).days * 86_400 - 19_800 + 10 * 3600
    fills = [
        (ten, "RELIANCE", 1000, 100.0, "signal"),
        (ten + 3600, "RELIANCE", -1000, 110.0, "signal"),
    ]
    for seq, (ts, symbol, qty, price, reason) in enumerate(fills):
        rdb.db_session.add(
            rdb.LedgerFill(run_id=alpha_replay.id, seq=seq, instance=0, ts=ts, symbol=symbol,
                           qty=qty, price=price, fees=0.0, slippage=0.0, reason=reason)
        )  # fmt: skip
    rdb.db_session.commit()
    assert run_service.backtests(run["id"], world.owner)[0]["status"] == "running"
    for replay in (alpha_replay, beta_replay):
        replay.status = rdb.COMPLETED
    rdb.db_session.commit()

    done = run_service.get_backtest(run["id"], world.owner, started["id"])
    assert done["status"] == "done"
    days = done["result"]["run"]["daily"]
    assert len(days) == 22  # every weekday of September 2026 opens and closes
    assert (days[0]["date"], days[0]["trades"], days[0]["pnl"]) == ("2026-09-01", 2, 300.0)
    expected = run_sim.simulate(
        run_service.sim_definition(run_service._versions(run["id"])[-1]),
        {alpha_source: run_sim.Source(10_000_000, "NSE", fills),
         beta_source: run_sim.Source(10_000_000, "NSE", [])},
    )  # fmt: skip
    assert days[0]["net"] == pytest.approx(expected["days"][0]["net"])
    assert done["summary"]["total_pnl"] == pytest.approx(expected["days"][0]["net"])
    assert SandboxAccount.query.filter_by(owner=world.owner).count() == 0


def test_a_failed_replay_fails_the_backtest_with_its_reason(world, monkeypatch):
    import database.research_db as rdb
    from services import research_service as svc
    from services import run_service

    monkeypatch.setattr(svc, "dispatch", lambda: None)
    run = _create(world)
    started = run_service.backtest(run["id"], world.owner, "2026-09-01", "2026-09-30")
    replay = _replays(started)[1]
    replay.status, replay.message = rdb.FAILED, "The script raised on its first day."
    rdb.db_session.commit()
    (listed,) = run_service.backtests(run["id"], world.owner)
    assert (listed["status"], listed["message"]) == (
        "failed",
        "The script raised on its first day.",
    )


def test_the_backtest_api_starts_one_and_refuses_days_not_yet_past(world, monkeypatch):
    from blueprints import runs
    from services import research_service as svc

    monkeypatch.setattr(svc, "dispatch", lambda: None)
    _call.owner = world.owner
    run = _create(world)
    status, body = _call(
        runs.start_backtest, "POST", {"from": "2026-09-01", "to": "2026-09-30"}, run_id=run["id"]
    )
    assert status == 201 and body["data"]["status"] == "running"
    status, body = _call(
        runs.start_backtest, "POST", {"from": "2026-09-01", "to": "2099-01-01"}, run_id=run["id"]
    )
    assert status == 400 and "past days only" in body["message"]
    status, body = _call(runs.list_backtests, run_id=run["id"])
    assert status == 200 and [b["from"] for b in body["data"]] == ["2026-09-01"]


def test_deleting_a_run_never_traded_takes_its_backtests_with_it(world, monkeypatch):
    import database.research_db as rdb
    from services import research_service as svc
    from services import run_service

    monkeypatch.setattr(svc, "dispatch", lambda: None)
    run = _create(world)
    run_service.backtest(run["id"], world.owner, "2026-09-01", "2026-09-30")
    run_service.delete(run["id"], world.owner)
    assert rdb.db_session.query(rdb.StrategyRunBacktest).filter_by(run_id=run["id"]).count() == 0


def test_a_backtest_request_is_not_taken_for_a_run_action():
    from flask import Flask

    from blueprints.runs import runs_bp

    app = Flask(__name__)
    app.register_blueprint(runs_bp)
    endpoint, _args = app.url_map.bind("").match("/runs/api/runs/7/backtests", method="POST")
    assert endpoint == "runs.start_backtest"


def test_the_pnl_api_refuses_a_run_it_cannot_name(world):
    from blueprints import runs

    _call.owner = world.owner
    for query in ("runs=first", "runs=%C2%B2", "members=7", "members=x:alpha"):
        status, body = _call_query(runs.pnl, query)
        assert (status, body["message"]) == (400, "That Run does not exist."), query


def _call_query(view, query):
    import inspect

    from flask import Flask, session

    app = Flask(__name__)
    app.secret_key = "test-only"
    with app.test_request_context(f"/runs/api/pnl?{query}"):
        session["user"] = _call.owner
        response, status = inspect.unwrap(view)()
    return status, response.get_json()

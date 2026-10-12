"""
Sandbox sessions, a whole trading day at a time (PRD Phase 8.7 to 8.11, UAT
PAPER-001 to PAPER-005 and PAPER-008 to PAPER-012).

The worker's real ``Session`` runs against the real research database, with
OpenAlgo replaced by a fake that fills like the sandbox does (MARKET at the
latest price) and a clock the test advances, so a day from 09:15 to 15:31 runs
in well under a second. What is pinned is the session's own behaviour: books
and ledger, isolation and netting, resumption after a restart without a
duplicate order, rejection, unfilled orders, stopping, stale data, and the
lease that keeps two workers off one session.
"""

import time
from datetime import datetime

import pytest

from research import strategy as rs
from research.strategy import IST_OFFSET_SECONDS

DAY = "2026-10-07"


def at(hhmm: str, second: int = 0) -> float:
    local = datetime.fromisoformat(f"{DAY}T{hhmm}:00")
    return (local - datetime(1970, 1, 1)).total_seconds() - IST_OFFSET_SECONDS + second


class Buyer:
    """Goes long AAA at the first decision of the day and holds."""

    interval = "5m"

    def __init__(self):
        self.done = False

    def decide(self, ctx):
        if self.done:
            return None
        self.done = True
        return {"AAA": 1.0}


class Seller(Buyer):
    def decide(self, ctx):
        if self.done:
            return None
        self.done = True
        return {"AAA": -1.0}


class Fragile(Buyer):
    """Goes long once; raises at any decision once ``broken`` is set."""

    broken = False

    def decide(self, ctx):
        if Fragile.broken:
            raise ValueError("the model file is missing")
        return super().decide(ctx)


class Greedy(Buyer):
    """Goes long once; then, when ``broken``, asks for twice its capital."""

    broken = False

    def decide(self, ctx):
        if Greedy.broken:
            return {"AAA": 2.0}
        return super().decide(ctx)


class Watcher(Buyer):
    """Buyer that writes down the position it sees at every decision, live or replayed."""

    seen: list = []

    def decide(self, ctx):
        Watcher.seen.append((ctx.time, ctx.position("AAA")))
        return super().decide(ctx)


class Scorer(Buyer):
    """Buyer that records a score for every symbol at every decision."""

    def decide(self, ctx):
        ctx.record("score", dict.fromkeys(ctx.symbols, 1.0))
        return super().decide(ctx)


class Pair(Buyer):
    """Goes half long AAA and half long BBB at the first decision of the day."""

    def decide(self, ctx):
        if self.done:
            return None
        self.done = True
        return {"AAA": 0.5, "BBB": 0.5}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeApp:
    """OpenAlgo as the worker sees it: history, and a sandbox that fills MARKET orders."""

    def __init__(self, prices):
        self.prices = prices
        self.orders = {}
        self.placed = []
        self.reject = False
        self.leave_open = False
        self.refuse_cancel = False
        self.door = "researchsandbox"
        self.partial = None  # fill only this many of the next order, then cancel the rest
        self.unknown = set()  # symbols whose orders' outcome the venue cannot tell

    def with_door(self, door):
        self.door = door
        return self

    def history(self, symbol, exchange, interval, start, end):
        return []

    def place(self, run_id, order):
        self.placed.append(order["platform_order_id"])
        if order["platform_order_id"] in self.orders:
            return {**self.orders[order["platform_order_id"]], "created": False}
        if self.reject:
            return {"orderid": None, "order_status": "rejected", "filled_quantity": 0,
                    "average_price": 0.0, "rejection_reason": "Insufficient funds"}  # fmt: skip
        status = {
            "orderid": f"SB{len(self.orders) + 1}",
            "order_status": "open" if self.leave_open else "complete",
            "filled_quantity": 0 if self.leave_open else abs(order["quantity"]),
            "average_price": 0.0 if self.leave_open else self.prices[order["symbol"]],
            "rejection_reason": None,
            "quantity": order["quantity"],
            "symbol": order["symbol"],
        }
        if self.partial is not None:
            status.update(order_status="cancelled", filled_quantity=self.partial)
            self.partial = None
        if order["symbol"] in self.unknown:
            status.update(order_status="unknown", filled_quantity=0, average_price=0.0)
        self.orders[order["platform_order_id"]] = status
        return {**status, "created": True}

    def status(self, run_id, platform_order_id):
        if getattr(self, "down", False):
            raise RuntimeError("the venue is not answering")
        return self.orders.get(platform_order_id)

    def cancel(self, run_id, platform_order_id):
        status = self.orders.get(platform_order_id)
        if status and status["order_status"] == "open" and not self.refuse_cancel:
            status["order_status"] = "cancelled"
        return status

    def fill_open(self):
        for status in self.orders.values():
            if status["order_status"] == "open":
                status.update(
                    order_status="complete",
                    filled_quantity=abs(status["quantity"]),
                    average_price=self.prices[status["symbol"]],
                )


class FakeFeed:
    def __init__(self):
        self.on_tick = None

    def start(self, symbols, exchange, on_tick):
        self.on_tick = on_tick

    def stop(self):
        pass


@pytest.fixture
def world(research_db, monkeypatch, tmp_path):
    """A registry with Buyer and Seller, and a helper that deploys a scenario."""
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "data"))
    specs = {
        "buyer": rs.StrategySpec("buyer", "entry_point", f"{__name__}:Buyer", "test", "1"),
        "seller": rs.StrategySpec("seller", "entry_point", f"{__name__}:Seller", "test", "1"),
        "fragile": rs.StrategySpec("fragile", "entry_point", f"{__name__}:Fragile", "test", "1"),
        "greedy": rs.StrategySpec("greedy", "entry_point", f"{__name__}:Greedy", "test", "1"),
        "watcher": rs.StrategySpec("watcher", "entry_point", f"{__name__}:Watcher", "test", "1"),
        "pair": rs.StrategySpec("pair", "entry_point", f"{__name__}:Pair", "test", "1"),
        "scorer": rs.StrategySpec("scorer", "entry_point", f"{__name__}:Scorer", "test", "1"),
    }
    monkeypatch.setattr(rs, "discover", lambda: specs)
    from services import research_service as svc

    svc.sync_registry()
    rdb = research_db

    def deploy(keys, mode="ISOLATED", capital=100_000, policy="PAUSE", environment="SANDBOX",
               slippage_bps=0, scenario_id=None, **config):  # fmt: skip
        if scenario_id is not None:
            return _session_row(scenario_id, policy, environment)
        exp = svc.create_experiment("sandbox")["id"]
        pairs = []
        for key in keys:
            strategy = rdb.ResearchStrategy.query.filter_by(key=key).one()
            version = rdb.ResearchStrategyVersion.query.filter_by(strategy_id=strategy.id).one()
            cfg = {"capital": capital, "universe": ["AAA", "BBB"], "entry_time": "09:15",
                   "exit_time": "15:10", **config}  # fmt: skip
            saved, _ = svc.save_config(strategy.id, cfg)
            pairs.append({"strategy_version_id": version.id, "config_version_id": saved["id"]})
        scenario = svc.create_scenario(
            exp,
            {"name": "s", "mode": mode, "start": "2026-01-01", "end": "2026-01-02",
             "cost_model": {"preset": "india_intraday_nse", "slippage_bps": slippage_bps},
             "instances": pairs},
        )  # fmt: skip
        return _session_row(scenario["id"], policy, environment)

    def _session_row(scenario_id, policy, environment):
        session = rdb.db_session
        deployment = rdb.ResearchDeployment(
            scenario_id=scenario_id, name="d", user_id="trader", schedule_start="09:10",
            schedule_stop="15:25", schedule_days=["wed"], stale_policy=policy,
            environment=environment, capital_limit=1e9 if environment == "LIVE" else None,
        )  # fmt: skip
        session.add(deployment)
        session.flush()
        run = rdb.ResearchRun(
            scenario_id=scenario_id, status="QUEUED", environment=environment,
            deployment_id=deployment.id, trading_date=DAY,
        )  # fmt: skip
        session.add(run)
        session.commit()
        return run.id

    return svc, rdb, deploy


def new_session(run_id, app, clock):
    from research.sandbox_worker import Session

    session = Session(run_id, app, FakeFeed(), clock=clock)
    assert session.claim()
    session.set_status("STARTING", "Starting")
    session.start()
    return session


def trade(session, clock, prices, until, every=30, before=None):
    """Advance the clock tick by tick, feeding prices, stepping the session."""
    while clock.t < until:
        if before:
            before(clock.t)
        for symbol, price in prices.items():
            session.on_tick(symbol, price, 1000, clock.t)
        if not session.step():
            return False
        clock.t += every
    return True


def run_row(rdb, run_id):
    rdb.db_session.expire_all()
    return rdb.db_session.get(rdb.ResearchRun, run_id)


def reconciles(rdb, run_id):
    run = run_row(rdb, run_id)
    trades = rdb.LedgerTrade.query.filter_by(run_id=run_id).all()
    for row in run.attribution:
        net = sum(t.net_pnl for t in trades if t.instance == row["instance"])
        assert row["net_pnl"] == pytest.approx(net, abs=1e-6)
    assert run.metrics["net_pnl"] == pytest.approx(
        sum(r["net_pnl"] for r in run.attribution), abs=1e-6
    )


def test_a_single_strategy_trades_a_full_day_and_completes(world):
    """PAPER-001 and PAPER-010."""
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, prices, app = (
        Clock(at("09:14")),
        {"AAA": 100.0, "BBB": 50.0},
        FakeApp({"AAA": 100.0, "BBB": 50.0}),
    )
    session = new_session(run_id, app, clock)

    def move(t):
        prices["AAA"] = app.prices["AAA"] = 100.0 + (t - at("09:14")) / 600

    assert trade(session, clock, prices, at("16:00"), before=move) is False

    run = run_row(rdb, run_id)
    assert run.status == "COMPLETED", run.message
    assert run.provenance["execution_environment"] == "SANDBOX"
    assert run.provenance["instances"][0]["strategy_key"] == "buyer"
    fills = rdb.LedgerFill.query.filter_by(run_id=run_id).order_by(rdb.LedgerFill.seq).all()
    assert [(f.qty > 0, f.reason) for f in fills] == [(True, "signal"), (False, "exit_time")]
    assert run.metrics["net_pnl"] > 0 and run.metrics["trades"] == 1
    kinds = [e.kind for e in rdb.LedgerEvent.query.filter_by(run_id=run_id)]
    assert kinds.count("ORDER_INTENT") == kinds.count("ORDER_FILLED") == 2
    assert len(app.placed) == len(set(app.placed)) == 2
    assert rdb.SessionBar.query.filter_by(run_id=run_id, symbol="AAA").count() > 70
    reconciles(rdb, run_id)


def test_isolated_books_never_net_and_shared_books_do(world):
    """PAPER-002, PAPER-003 and PAPER-004: opposite intents on one symbol."""
    svc, rdb, deploy = world
    results = {}
    for mode in ("ISOLATED", "SHARED"):
        run_id = deploy(["buyer", "seller"], mode=mode)
        clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
        session = new_session(run_id, app, clock)
        trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))
        results[mode] = (run_id, app)

    iso_run, iso_app = results["ISOLATED"]
    shared_run, shared_app = results["SHARED"]
    assert run_row(rdb, iso_run).status == run_row(rdb, shared_run).status == "COMPLETED"
    # Isolated: each book sends its own order, entry and exit.
    assert len(iso_app.placed) == 4
    # Shared: +1000 and -1000 cross inside the portfolio; nothing reaches the sandbox.
    assert shared_app.placed == []
    shared_fills = rdb.LedgerFill.query.filter_by(run_id=shared_run).all()
    assert sorted(f.qty for f in shared_fills if f.reason == "signal") == [-1000, 1000]
    assert all(f.order_seq is None for f in shared_fills)
    for run_id, _ in results.values():
        reconciles(rdb, run_id)


def test_concurrent_sessions_keep_their_own_books(world):
    """PAPER-005: three sessions stepping side by side on the same ticks."""
    svc, rdb, deploy = world
    specs = [(["buyer", "seller"], "ISOLATED", 100_000), (["buyer", "seller"], "SHARED", 100_000),
             (["buyer"], "SHARED", 60_000)]  # fmt: skip
    sessions = []
    for keys, mode, capital in specs:
        run_id = deploy(keys, mode=mode, capital=capital)
        clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
        sessions.append((run_id, new_session(run_id, app, clock), clock))
    alive = set(range(len(sessions)))
    while alive:
        for n in sorted(alive):
            _run_id, session, clock = sessions[n]
            session.on_tick("AAA", 100.0, 1000, clock.t)
            session.on_tick("BBB", 50.0, 1000, clock.t)
            if not session.step():
                alive.discard(n)
            clock.t += 30
    capitals = [sorted(b["capital"] for b in run_row(rdb, r).attribution) for r, _, _ in sessions]
    assert capitals == [[100_000, 100_000], [100_000, 100_000], [60_000]]
    for run_id, _, _ in sessions:
        assert run_row(rdb, run_id).status == "COMPLETED"
        reconciles(rdb, run_id)


def test_a_restarted_session_resumes_without_a_duplicate_order(world):
    """PAPER-008 and PAPER-012: the worker dies mid-day; a new one takes over."""
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    first = new_session(run_id, app, clock)
    trade(first, clock, {"AAA": 100.0, "BBB": 50.0}, at("10:00"))
    held = dict(first.engine.books[0].qty)
    assert held.get("AAA") == 1000

    from research.sandbox_worker import Session

    clock.t += 31  # the dead worker's lease runs out
    second = Session(run_id, app, FakeFeed(), clock=clock)
    assert second.claim()
    second.start()

    assert second.engine.books[0].qty.get("AAA") == 1000
    assert second.engine.books[0].cash == pytest.approx(first.engine.books[0].cash)
    trade(second, clock, {"AAA": 101.0, "BBB": 50.0}, at("16:00"))
    assert run_row(rdb, run_id).status == "COMPLETED"
    assert len(app.placed) == len(set(app.placed)) == 2, "the entry was not placed again"
    from research.sandbox_worker import SessionStop

    with pytest.raises(SessionStop):
        first.heartbeat(force=True)  # the old worker has lost the session
    reconciles(rdb, run_id)


def test_a_second_worker_cannot_take_a_live_session(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock = Clock(at("09:14"))
    new_session(run_id, FakeApp({"AAA": 100.0, "BBB": 50.0}), clock)

    from research.sandbox_worker import Session

    assert not Session(run_id, FakeApp({}), FakeFeed(), clock=clock).claim()


def test_a_rejected_order_leaves_the_book_flat(world):
    """PAPER-009."""
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    app.reject = True
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))

    assert run_row(rdb, run_id).status == "COMPLETED"
    assert rdb.LedgerFill.query.filter_by(run_id=run_id).count() == 0
    rejected = rdb.LedgerEvent.query.filter_by(run_id=run_id, kind="ORDER_REJECTED").one()
    assert rejected.payload["reason"] == "Insufficient funds"


def test_an_order_left_open_is_cancelled_or_filled_late_never_forgotten(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    app.leave_open, app.refuse_cancel = True, True
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:25"))
    assert session.waiting, "the sandbox refused the cancel, so the order is still tracked"
    assert (
        rdb.LedgerEvent.query.filter_by(run_id=run_id, kind="SESSION_EVENT")
        .filter(rdb.LedgerEvent.payload["event"].as_string() == "cancel_refused")
        .count()
        == 1
    )

    app.leave_open = False
    app.fill_open()  # analyzer mode came back on and the sandbox filled it
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:26"))
    assert not session.waiting
    assert session.engine.books[0].qty.get("AAA") == 1000


def test_stopping_mid_day_closes_positions_and_completes(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("10:00"))
    rdb.db_session.query(rdb.ResearchRun).filter_by(id=run_id).update({"status": "STOPPING"})
    rdb.db_session.commit()

    assert trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("11:00")) is False
    run = run_row(rdb, run_id)
    assert run.status == "COMPLETED"
    exits = rdb.LedgerFill.query.filter_by(run_id=run_id, reason="square_off").all()
    assert [f.qty for f in exits] == [-1000]


def test_paused_sessions_take_no_new_entries(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    rdb.db_session.query(rdb.ResearchRun).filter_by(id=run_id).update({"status": "PAUSED"})
    rdb.db_session.commit()
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("10:00"))

    assert app.placed == []
    assert session.live_state()["accept_new"] is False


def test_fail_safe_on_stale_data_flattens_and_fails(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"], policy="FAIL_SAFE")
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:40"))
    assert session.engine.books[0].qty.get("AAA") == 1000

    while session.step():  # ticks stop arriving
        clock.t += 30
        assert clock.t < at("12:00")

    run = run_row(rdb, run_id)
    assert run.status == "FAILED" and "Market data stopped" in run.message
    assert not any(session.engine.books[0].qty.values())
    reconciles(rdb, run_id)


@pytest.mark.parametrize(
    ("strategy", "key", "said"),
    [(Fragile, "fragile", "A strategy stopped with an error while deciding."),
     (Greedy, "greedy", "greedy (config v1) asked for 200% of its capital")],
)  # fmt: skip
def test_a_strategy_that_fails_mid_day_is_squared_off_not_abandoned(
    world, monkeypatch, strategy, key, said
):
    """PAPER-012: a failing strategy must not leave its position in the sandbox unmanaged."""
    svc, rdb, deploy = world
    run_id = deploy([key])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:40"))
    assert session.engine.books[0].qty.get("AAA") == 1000

    monkeypatch.setattr(strategy, "broken", True)
    assert not trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("12:00"))

    run = run_row(rdb, run_id)
    assert run.status == "FAILED" and run.message.startswith(said), run.message
    assert "closed its positions" in run.message
    assert not any(session.engine.books[0].qty.values())
    assert [s["quantity"] for s in app.orders.values()] == [1000, -1000]
    reconciles(rdb, run_id)


def test_a_strategy_that_fails_while_resuming_is_squared_off(world, monkeypatch):
    """The replay after a restart asks the strategy too; failing there must still close."""
    svc, rdb, deploy = world
    run_id = deploy(["fragile"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    first = new_session(run_id, app, clock)
    trade(first, clock, {"AAA": 100.0, "BBB": 50.0}, at("10:00"))

    from research.sandbox_worker import Session

    monkeypatch.setattr(Fragile, "broken", True)
    clock.t += 31
    second = Session(run_id, app, FakeFeed(), clock=clock)
    assert second.claim()
    second.start()
    assert second.engine.books[0].qty.get("AAA") == 1000, "books restored before closing"
    assert not trade(second, clock, {"AAA": 100.0, "BBB": 50.0}, at("12:00"))

    run = run_row(rdb, run_id)
    assert run.status == "FAILED" and "closed its positions" in run.message
    assert not any(second.engine.books[0].qty.values())
    assert [s["quantity"] for s in app.orders.values()] == [1000, -1000]
    reconciles(rdb, run_id)


def test_a_resumed_strategy_replays_with_the_positions_it_held(world, monkeypatch):
    """Fills are replayed between the bars they settled between, not after them all."""
    svc, rdb, deploy = world
    run_id = deploy(["watcher"])
    monkeypatch.setattr(Watcher, "seen", [])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    first = new_session(run_id, app, clock)
    trade(first, clock, {"AAA": 100.0, "BBB": 50.0}, at("10:00"))
    lived = list(Watcher.seen)
    assert (at("09:20"), 1000) in lived

    from research.sandbox_worker import Session

    Watcher.seen.clear()
    clock.t += 31
    second = Session(run_id, app, FakeFeed(), clock=clock)
    assert second.claim()
    second.start()

    assert Watcher.seen == lived


def test_a_session_ledger_holds_each_intent_once_and_before_it_was_sent(world):
    """Phase 5: the engine records the intent; the session commits it before placing."""
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))

    events = rdb.LedgerEvent.query.filter_by(run_id=run_id).order_by(rdb.LedgerEvent.seq).all()
    intents = [e for e in events if e.kind == "ORDER_INTENT"]
    assert len(intents) == len({e.platform_order_id for e in intents}) == 2, "entry and exit"
    for intent in intents:
        (sent,) = [e for e in events if e.kind == "ORDER_SUBMITTED"
                   and e.platform_order_id == intent.platform_order_id]  # fmt: skip
        assert intent.seq < sent.seq
    kinds = {e.kind for e in events}
    assert {"SIGNAL", "TARGET_POSITION", "SQUARE_OFF"} <= kinds
    state = run_row(rdb, run_id).live_state
    assert state["last_signal"]["weights"] == {"AAA": 1.0}
    assert state["last_order"]["net"] == -1000
    reconciles(rdb, run_id)


def test_a_session_journal_and_reconciliation_hold_after_a_day(world):
    """Phase 5 audit tools on a session: venue ids in the journal, every check passing."""
    from services import research_ledger_service as ledger

    svc, rdb, deploy = world
    run_id = deploy(["buyer", "seller"], mode="SHARED")
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))

    report = ledger.reconcile(run_id)
    assert report["passed"] and all(c["passed"] is not False for c in report["checks"]), report
    entries = ledger.journal(run_id, limit=1000)["items"]
    kinds = {e["kind"] for e in entries}
    assert {"SIGNAL", "TARGET_POSITION", "SQUARE_OFF", "FILL", "MARK"} <= kinds
    assert "ORDER_SUBMITTED" not in kinds, "crossed intents send nothing"
    crossed = [e for e in entries if e["kind"] == "FILL"]
    assert all(e["platform_order_id"] is None for e in crossed)
    assert all("crossed inside the portfolio" in e["detail"] for e in crossed)


def test_a_session_journal_names_the_venue_order(world):
    from services import research_ledger_service as ledger

    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))

    fills = ledger.journal(run_id, kinds={"FILL"})["items"]
    assert [f["execution_order_id"] for f in fills] == ["SB1", "SB2"]
    assert all(f["requested_price"] == 100.0 for f in fills)
    assert ledger.reconcile(run_id)["passed"]


def test_reconciliation_finds_a_fill_the_orders_do_not_explain(world):
    """The checks can fail: an extra fill on an order breaks two of them."""
    from services import research_ledger_service as ledger

    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))
    entry = rdb.LedgerFill.query.filter_by(run_id=run_id).order_by(rdb.LedgerFill.seq).first()
    rdb.db_session.add(
        rdb.LedgerFill(run_id=run_id, seq=10_000, order_seq=entry.order_seq, instance=0,
                       ts=entry.ts, symbol="AAA", qty=5, price=100.0, fees=1.0, slippage=0.0,
                       reason="signal")
    )  # fmt: skip
    rdb.db_session.commit()

    report = ledger.reconcile(run_id)
    failed = {c["name"] for c in report["checks"] if c["passed"] is False}
    assert not report["passed"]
    assert "Every order equals the book fills it was netted from" in failed
    assert "Every order's costs are allocated to its books in full" in failed


def test_a_session_records_exposure_and_carries_its_analytics_across_a_restart(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer", "seller"], mode="ISOLATED")
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    first = new_session(run_id, app, clock)
    trade(first, clock, {"AAA": 100.0, "BBB": 50.0}, at("10:00"))
    marks = rdb.LedgerEvent.query.filter_by(run_id=run_id, kind="MARK").all()
    assert marks[-1].payload["book_exposure"] == [100_000.0, 100_000.0]
    seen = run_row(rdb, run_id).live_state["analytics"]
    assert seen["bars_seen"] > 0 and seen["overlap"]["0:1"]["opposite"] > 0

    from research.sandbox_worker import Session

    clock.t += 31
    second = Session(run_id, app, FakeFeed(), clock=clock)
    assert second.claim()
    second.start()
    assert second.engine.bars_seen == seen["bars_seen"]
    trade(second, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))
    run = run_row(rdb, run_id)
    assert run.status == "COMPLETED"
    assert run.metrics["together"]["overlap"][0]["opposite_pct"] > 0
    assert run.metrics["avg_exposure_pct"] > 0


# --------------------------------------------------------------------------- #
# Shadow and Live sessions (master PRD Phase 9)
# --------------------------------------------------------------------------- #


def test_a_shadow_session_fills_inside_itself_and_sends_nothing_anywhere(world):
    """Critical safety test 3: a Shadow session has no door to call."""
    svc, rdb, deploy = world
    run_id = deploy(["buyer"], environment="SHADOW", slippage_bps=10)
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 999.0, "BBB": 999.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))

    run = run_row(rdb, run_id)
    assert run.status == "COMPLETED", run.message
    assert app.placed == [], "no order was sent to any venue"
    fills = rdb.LedgerFill.query.filter_by(run_id=run_id).order_by(rdb.LedgerFill.seq).all()
    assert [(f.qty, f.price) for f in fills] == [(1000, 100.0), (-1000, 100.0)], "the last tick"
    assert all(f.slippage == pytest.approx(100_000 * 0.001) for f in fills), "modelled slippage"
    sent = rdb.LedgerEvent.query.filter_by(run_id=run_id, kind="ORDER_SUBMITTED").all()
    assert sent and all(e.payload["orderid"].startswith("shadow:") for e in sent)
    assert run.provenance["execution_environment"] == "SHADOW"
    reconciles(rdb, run_id)


def test_a_live_session_sends_its_orders_through_the_live_door(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"], environment="LIVE")
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))

    assert app.door == "researchlive"
    assert len(app.placed) == 2 and run_row(rdb, run_id).status == "COMPLETED"
    assert run_row(rdb, run_id).live_state["environment"] == "LIVE"


def test_the_kill_switch_stops_a_live_session_and_closes_its_positions(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"], environment="LIVE")
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("10:00"))
    assert session.engine.books[0].qty.get("AAA") == 1000

    rdb.db_session.add(rdb.ResearchControl(name="live_kill", value={"engaged": True}))
    rdb.db_session.commit()
    assert not trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("12:00"))

    run = run_row(rdb, run_id)
    assert run.status == "COMPLETED" and "kill switch" in run.message
    assert not any(session.engine.books[0].qty.values())


def test_a_sandbox_session_ignores_the_live_kill_switch(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    rdb.db_session.add(rdb.ResearchControl(name="live_kill", value={"engaged": True}))
    rdb.db_session.commit()
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("10:00"))
    assert session.engine.books[0].qty.get("AAA") == 1000 and not session.stopping


def test_an_unknown_order_outcome_is_never_guessed_at(world):
    """The symbol is left alone, everything else is closed, and the message says what to check."""
    svc, rdb, deploy = world
    run_id = deploy(["pair"], environment="LIVE")
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    app.unknown = {"AAA"}
    session = new_session(run_id, app, clock)
    assert not trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("12:00"))

    run = run_row(rdb, run_id)
    assert run.status == "FAILED"
    assert "order" in run.message and "for AAA is unknown" in run.message
    sent = [app.orders[o] for o in app.placed]
    assert [(o["symbol"], o["quantity"]) for o in sent] == [
        ("AAA", 500),
        ("BBB", 1000),
        ("BBB", -1000),
    ]
    assert session.engine.books[0].qty.get("BBB", 0) == 0


def test_a_partly_filled_order_books_what_filled(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    app.partial = 400
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:40"))

    assert session.engine.books[0].qty.get("AAA") == 400
    (filled,) = rdb.LedgerEvent.query.filter_by(run_id=run_id, kind="ORDER_FILLED").all()
    assert filled.payload["partial"] and (filled.payload["filled"], filled.payload["requested"]) == (
        400, 1000
    )  # fmt: skip
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))
    assert run_row(rdb, run_id).status == "COMPLETED"
    reconciles(rdb, run_id)


def test_shadow_compares_with_the_sandbox_session_of_the_same_day(world):
    """PRD section 56: the same strategies on the same ticks, decision by decision."""
    from services import research_ledger_service as ledger

    svc, rdb, deploy = world
    sandbox_id = deploy(["buyer"])
    scenario_id = run_row(rdb, sandbox_id).scenario_id
    shadow_id = deploy(["buyer"], environment="SHADOW", scenario_id=scenario_id)
    for run_id in (sandbox_id, shadow_id):
        clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.5, "BBB": 50.0})
        session = new_session(run_id, app, clock)
        trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))

    report = ledger.compare_runs(sandbox_id, shadow_id)
    assert [r["environment"] for r in report["runs"]] == ["SANDBOX", "SHADOW"]
    assert report["summary"] == {"same_decisions": 2, "different_decisions": 0,
                                 "average_price_difference": pytest.approx(-0.5)}  # fmt: skip


def test_the_session_says_when_its_venue_stops_answering(world):
    """PRD 8 section 30: the session page shows the sandbox's status."""
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    app.leave_open = app.refuse_cancel = True  # an order that stays open, polled each pass
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:25"))
    assert run_row(rdb, run_id).live_state["venue"]["answering"] is True

    app.down = True
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:30"))
    venue = run_row(rdb, run_id).live_state["venue"]
    assert venue["answering"] is False and venue["error"] == "the sandbox did not answer"
    assert session.waiting, "the order is still tracked, never assumed"


def test_an_error_after_trading_never_ends_the_session_with_positions_open(world):
    """Resume, else square off: the worker exits and the app resumes the session.

    Before this, an unexpected error marked the session FAILED where it stood,
    so whatever it held, in the sandbox or at the broker, had nothing managing
    it and the supervisor, seeing it ended, never started another worker.
    """
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    first = new_session(run_id, app, clock)
    trade(first, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:40"))
    assert first.engine.books[0].qty.get("AAA") == 1000

    assert first.stopped_on(RuntimeError("disk full")) == 1
    assert run_row(rdb, run_id).status == "RUNNING"
    first.release()

    from research.sandbox_worker import Session

    last = Session(run_id, app, FakeFeed(), clock=clock)  # the app's last restart
    assert last.claim()
    last.start()
    last.close_after_errors()
    assert not trade(last, clock, {"AAA": 100.0, "BBB": 50.0}, at("12:00"))

    run = run_row(rdb, run_id)
    assert run.status == "FAILED", run.message
    assert run.message == (
        "The session stopped on repeated errors. The session log has the details. "
        "The session closed its positions and ended."
    )
    assert [s["quantity"] for s in app.orders.values()] == [1000, -1000]
    reconciles(rdb, run_id)


def test_an_error_before_any_order_ends_the_session_saying_why(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    session = new_session(run_id, FakeApp({"AAA": 100.0, "BBB": 50.0}), Clock(at("09:14")))

    assert session.stopped_on(RuntimeError("socket closed")) == 1
    run = run_row(rdb, run_id)
    assert run.status == "FAILED"
    assert run.message.startswith("The session stopped on an unexpected error before placing")
    assert "socket" not in run.message


def test_a_stop_that_cannot_close_everything_never_says_it_did(world):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"], policy="FAIL_SAFE")
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    session = new_session(run_id, app, clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:40"))
    app.reject = True  # the sandbox refuses every closing order

    while session.step():  # ticks stop arriving: fail safe
        clock.t += 30
        assert clock.t < at("12:00")

    run = run_row(rdb, run_id)
    assert run.status == "FAILED"
    assert run.message == (
        "Market data stopped arriving. Some positions could not be closed by the sandbox: AAA. "
        "Check the sandbox's positions for this session."
    )


def test_the_app_resumes_a_stopped_worker_then_closes_then_gives_up(world, monkeypatch):
    svc, rdb, deploy = world
    run_id = deploy(["buyer"], environment="LIVE")
    rdb.db_session.query(rdb.ResearchRun).update({"status": "RUNNING"})
    rdb.db_session.commit()
    spawned = []

    def spawn(run, kind, env):
        spawned.append(env)
        svc._children[run] = {}

    monkeypatch.setattr(svc, "_spawn", spawn)
    child = {"env": {"OPENALGO_API_KEY": "k"}}
    for restarts in range(svc.SANDBOX_RESTARTS):
        svc._restart_or_settle_session(rdb.db_session, run_id, {**child, "restarts": restarts}, 1)
    assert [env["RESEARCH_RESTARTED"] for env in spawned] == ["resume", "resume", "close"]
    assert all(env["OPENALGO_API_KEY"] == "k" for env in spawned)

    svc._restart_or_settle_session(
        rdb.db_session, run_id, {**child, "restarts": svc.SANDBOX_RESTARTS}, 1
    )
    run = run_row(rdb, run_id)
    assert run.status == "FAILED" and len(spawned) == svc.SANDBOX_RESTARTS
    assert "could not close its positions itself" in run.message
    assert "check your broker's positions for its symbols now" in run.message
    svc._children.pop(run_id, None)


def test_alerts_reach_telegram_only_as_flow_sends_them(monkeypatch):
    """The bot must be on, the user linked and their notifications on."""
    import database.telegram_db as telegram_db
    import services.telegram_alert_service as telegram
    from services import research_sandbox_service as sandbox

    sent = []
    monkeypatch.setattr(
        telegram,
        "alert_executor",
        type("Pool", (), {"submit": lambda self, fn, *a: sent.append(a)})(),
    )
    users = {"trader": {"telegram_id": 42, "notifications_enabled": True},
             "quiet": {"telegram_id": 7, "notifications_enabled": False}}  # fmt: skip
    monkeypatch.setattr(telegram_db, "get_telegram_user_by_username", users.get)
    active = {"on": False}
    monkeypatch.setattr(telegram.telegram_alert_service, "is_bot_active", lambda: active["on"])

    sandbox.alert("trader", "Session opened")
    active["on"] = True
    for user in ("nobody", "quiet", "trader"):
        sandbox.alert(user, "Session opened")

    ((telegram_id, text),) = sent
    assert telegram_id == 42 and "Research Alert" in text and "Session opened" in text


def test_a_session_that_ends_tells_its_owner_how(world, monkeypatch):
    svc, rdb, deploy = world
    from services import research_sandbox_service as sandbox

    told = []
    monkeypatch.setattr(sandbox, "alert", lambda user, message: told.append((user, message)))
    done = deploy(["buyer"])
    clock, app = Clock(at("09:14")), FakeApp({"AAA": 100.0, "BBB": 50.0})
    trade(new_session(done, app, clock), clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))
    failed = deploy(["buyer"], policy="FAIL_SAFE")
    clock = Clock(at("09:14"))
    session = new_session(failed, FakeApp({"AAA": 100.0, "BBB": 50.0}), clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:40"))
    while session.step():
        clock.t += 30

    sandbox.alert_session_ended(done)
    sandbox.alert_session_ended(failed)

    assert told[0][0] == "trader"
    assert told[0][1].startswith("Session completed: d (Sandbox), 2026-10-07. Net P&L Rs ")
    assert told[1][1] == (
        "Session failed: d (Sandbox), 2026-10-07. Market data stopped arriving. "
        "The session closed its positions and ended."
    )


def test_the_supervisor_alerts_when_a_session_worker_ends_it_not_when_it_resumes(
    world, monkeypatch
):
    svc, rdb, deploy = world
    from services import research_sandbox_service as sandbox

    ended = []
    monkeypatch.setattr(sandbox, "alert_session_ended", ended.append)
    monkeypatch.setattr(svc, "_spawn", lambda run, kind, env: svc._children.__setitem__(run, {}))
    finished, crashed = deploy(["buyer"]), deploy(["buyer"])
    rdb.db_session.query(rdb.ResearchRun).filter_by(id=crashed).update({"status": "RUNNING"})
    rdb.db_session.commit()
    exited = type("Exited", (), {"__init__": lambda self, code: setattr(self, "code", code),
                                 "poll": lambda self: self.code})  # fmt: skip
    svc._children[finished] = {"process": exited(0), "kind": "sandbox", "cancelled_at": None}
    svc._children[crashed] = {"process": exited(1), "kind": "sandbox", "cancelled_at": None}
    try:
        with svc._lock:
            svc._reap_locked(rdb.db_session)
    finally:
        svc._children.pop(finished, None)
        svc._children.pop(crashed, None)

    assert ended == [finished], "the crashed one was restarted to resume, not ended"


def test_session_log_lines_carry_the_ids_they_are_searched_by(world):
    """Master PRD section 42: session, experiment and scenario on every line."""
    from types import SimpleNamespace

    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    session = new_session(run_id, FakeApp({"AAA": 100.0, "BBB": 50.0}), Clock(at("09:14")))
    lines = []
    session.logger = SimpleNamespace(warning=lambda message, *args: lines.append(message % args))

    session.log("order=%s placing failed: %s", f"research:{run_id}:3", "timeout", level="warning")

    scenario = rdb.db_session.get(rdb.ResearchScenario, run_row(rdb, run_id).scenario_id)
    assert lines == [
        f"session={run_id} experiment={scenario.experiment_id} scenario={scenario.id} "
        f"order=research:{run_id}:3 placing failed: timeout"
    ]


def test_the_operations_panel_lists_each_worker_with_its_memory_and_lease(world):
    import os
    from types import SimpleNamespace

    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    new_session(run_id, FakeApp({"AAA": 100.0, "BBB": 50.0}), Clock(at("09:14")))
    svc._children[run_id] = {"process": SimpleNamespace(pid=os.getpid()), "kind": "sandbox",
                             "cancelled_at": None, "started_at": time.monotonic() - 90}  # fmt: skip
    try:
        overview = svc.operations()
    finally:
        svc._children.pop(run_id, None)

    (worker,) = overview["workers"]
    assert (worker["run_id"], worker["kind"], worker["environment"]) == (
        run_id,
        "session",
        "SANDBOX",
    )
    assert worker["memory_mb"] > 0 and worker["running_seconds"] >= 90
    assert worker["heartbeat_at"] and worker["lease_owner"]
    assert overview["limits"]["sessions"] == svc.MAX_SESSIONS


def test_stop_all_stops_every_session_of_the_user_and_nothing_else(world):
    svc, rdb, deploy = world
    from services import research_sandbox_service as sandbox

    mine, theirs = deploy(["buyer"]), deploy(["buyer"])
    session = rdb.db_session
    session.query(rdb.ResearchRun).update({"status": "RUNNING"})
    other = session.get(rdb.ResearchDeployment, session.get(rdb.ResearchRun, theirs).deployment_id)
    other.user_id = "someone else"
    session.commit()

    assert sandbox.stop_all_sessions("trader") == [mine]
    assert run_row(rdb, mine).status == "STOPPING"
    assert run_row(rdb, theirs).status == "RUNNING"


def test_a_session_keeps_what_its_strategy_recorded(world):
    """The same ctx.record panel as a backtest's, written when the session completes."""
    import pandas as pd

    svc, rdb, deploy = world
    run_id = deploy(["scorer"])
    clock = Clock(at("09:14"))
    session = new_session(run_id, FakeApp({"AAA": 100.0, "BBB": 50.0}), clock)
    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("16:00"))

    artifact = rdb.ResearchArtifact.query.filter_by(run_id=run_id, kind="records").one()
    panel = pd.read_parquet(artifact.uri)
    assert set(panel["symbol"]) == {"AAA", "BBB"} and set(panel["name"]) == {"score"}
    assert len(panel) == 2 * panel["timestamp"].nunique()


def test_a_history_call_that_times_out_is_said_plainly_and_waits_longer(monkeypatch):
    """A warm-up is one history call per symbol, weeks of minute bars each.

    It waits HISTORY_TIMEOUT_SECONDS, not an order's ten, and a timeout comes
    back as the client's own AppError: the warm-up skips that symbol on it.
    Before, the raw timeout escaped and ended the session at open.
    """
    import httpx

    from research.sandbox_worker import HISTORY_TIMEOUT_SECONDS, AppClient, AppError

    waits = []

    class Slow:
        def post(self, url, json, timeout):
            waits.append(timeout)
            raise httpx.ReadTimeout("slow")

    client = AppClient("http://openalgo", "key")
    client._client = Slow()
    with pytest.raises(AppError, match="did not answer within 60 seconds"):
        client.history("BBB", "NSE", "1m", "2026-09-01", "2026-10-07")
    with pytest.raises(AppError, match="did not answer within 10 seconds"):
        client.status(1, "research:1:1")
    assert waits == [HISTORY_TIMEOUT_SECONDS, client.timeout]


def test_a_symbol_whose_warm_up_fails_is_skipped_and_the_session_opens(world):
    from research.sandbox_worker import AppError

    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    app = FakeApp({"AAA": 100.0, "BBB": 50.0})
    yesterday = int(at("09:15")) - 86400

    def history(symbol, exchange, interval, start, end):
        if symbol == "BBB":
            raise AppError("OpenAlgo did not answer within 60 seconds.")
        return [{"timestamp": yesterday, "open": 100, "high": 101, "low": 99, "close": 100.5,
                 "volume": 10}]  # fmt: skip

    app.history = history
    session = new_session(run_id, app, Clock(at("09:14")))

    assert run_row(rdb, run_id).status == "RUNNING"
    assert int(session.engine.clock[session.engine.last_index()]) == yesterday


class Opening(Buyer):
    """Buyer that writes down the first bar of the day it decides on."""

    seen: list = []

    def decide(self, ctx):
        bars = ctx.bars("AAA")
        Opening.seen.append((bars.timestamp[-1], bars.open[-1], bars.volume[-1]))
        return super().decide(ctx)


class CandleApp(FakeApp):
    """A broker whose history has AAA's opening candle, and not yet BBB's."""

    def __init__(self, prices, candles):
        super().__init__(prices)
        self.candles = candles
        self.asked = []

    def history(self, symbol, exchange, interval, start, end):
        self.asked.append(symbol)
        return self.candles.get(symbol, [])


def test_the_days_first_bar_is_the_brokers_candle_not_the_first_ticks(world, monkeypatch):
    """The opening auction sets the open and adds volume no tick shows; a strategy
    trained on the broker's candles must decide on the broker's 09:15 candle."""
    svc, rdb, deploy = world
    specs = {
        **rs.discover(),
        "opening": rs.StrategySpec("opening", "entry_point", f"{__name__}:Opening", "test", "1"),
    }
    monkeypatch.setattr(rs, "discover", lambda: specs)
    svc.sync_registry()
    run_id = deploy(["opening"])
    opening = int(at("09:15"))
    candle = {"timestamp": opening, "open": 98.0, "high": 101.0, "low": 97.5, "close": 100.4,
              "volume": 52_000}  # fmt: skip
    app = CandleApp({"AAA": 100.0, "BBB": 50.0}, {"AAA": [candle]})
    clock = Clock(at("09:14"))
    session = new_session(run_id, app, clock)
    warm_up = len(app.asked)
    Opening.seen = []

    trade(session, clock, {"AAA": 100.0, "BBB": 50.0}, at("09:26"))

    first = {b.symbol: b for b in rdb.SessionBar.query.filter_by(run_id=run_id, ts=opening)}
    assert (first["AAA"].source, first["AAA"].open, first["AAA"].volume) == (
        "HISTORY",
        98.0,
        52_000,
    )
    assert (first["BBB"].source, first["BBB"].open) == ("TICKS", 50.0)
    assert Opening.seen[0] == (opening, 98.0, 52_000)
    # Later bars are built from ticks; only the first bar asks the broker.
    later = rdb.SessionBar.query.filter_by(run_id=run_id, ts=int(at("09:20")), symbol="AAA").one()
    assert later.source == "TICKS"
    asked = app.asked[warm_up:]
    assert asked.count("AAA") == 1 and asked.count("BBB") == 2


def test_fetching_the_first_bar_keeps_the_session_alive_and_stops_at_its_budget(world):
    """Two sessions share the broker at 09:16; a slow fetch must neither lose the
    lease nor wait past its budget."""
    from research import sandbox_worker

    svc, rdb, deploy = world
    run_id = deploy(["buyer"])
    clock = Clock(at("09:14"))

    class Slow(CandleApp):
        def history(self, symbol, exchange, interval, start, end):
            clock.t += 50  # each call takes the broker 50 seconds
            return super().history(symbol, exchange, interval, start, end)

    app = Slow({"AAA": 100.0, "BBB": 50.0}, {})
    session = new_session(run_id, app, clock)
    warm_up = len(app.asked)
    clock.t = at("09:16", 2)
    asked_at = clock.t

    bars, source = session._broker_candles(int(at("09:15")), {"AAA": (1, 1, 1, 1, 1)})

    assert source == {"AAA": "TICKS"}
    # Two calls fit in the 90 s budget; the third is never made.
    assert len(app.asked) - warm_up == 2
    assert clock.t - asked_at <= sandbox_worker.FIRST_BAR_BUDGET_SECONDS + 50
    heartbeat = run_row(rdb, run_id).heartbeat_at.replace(tzinfo=None)
    assert (heartbeat - sandbox_worker._utc(asked_at).replace(tzinfo=None)).total_seconds() >= 50

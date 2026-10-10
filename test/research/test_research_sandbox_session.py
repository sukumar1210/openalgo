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
        self.orders[order["platform_order_id"]] = status
        return {**status, "created": True}

    def status(self, run_id, platform_order_id):
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
    }
    monkeypatch.setattr(rs, "discover", lambda: specs)
    from services import research_service as svc

    svc.sync_registry()
    rdb = research_db

    def deploy(keys, mode="ISOLATED", capital=100_000, policy="PAUSE", **config):
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
             "cost_model": {"preset": "india_intraday_nse", "slippage_bps": 0},
             "instances": pairs},
        )  # fmt: skip
        session = rdb.db_session
        deployment = rdb.ResearchDeployment(
            scenario_id=scenario["id"], name="d", user_id="trader", schedule_start="09:10",
            schedule_stop="15:25", schedule_days=["wed"], stale_policy=policy,
        )  # fmt: skip
        session.add(deployment)
        session.flush()
        run = rdb.ResearchRun(
            scenario_id=scenario["id"], status="QUEUED", environment="SANDBOX",
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

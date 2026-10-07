"""
Run one Sandbox session in its own process: ``python -m research.sandbox_worker <run_id>``.

A session trades one deployed scenario for one trading day against live market
data, with every order filled by OpenAlgo's sandbox. It is the backtest's own
engine (``research.live.LiveEngine``) with a different clock and a different
place to get fills, so a strategy keeps its identity, books and accounting from
backtest to Sandbox.

What it talks to, and what it cannot
------------------------------------

The worker is a client of OpenAlgo, exactly like a hosted Python strategy: it
reads warm-up bars from ``/api/v1/history``, live ticks from the WebSocket
proxy, and places orders through ``/api/v1/researchsandbox/*``, which acts on
the sandbox only. It cannot import the broker plugins, order placement, the
sandbox itself or the auth-token store: the backtest's import guard is
installed before anything else, minus only the shared HTTP client. So the
broker boundary is structural twice over (PRD sections 15 and 16): this
process has no code that could place a broker order, and the only order door
it can reach has no path to one either.

Ownership
---------

A session is owned through a lease on its run row, renewed with every
heartbeat. A worker claims the session only when no live lease exists, and the
moment a heartbeat finds its lease taken it stops without sending anything
else. A restart therefore never leaves two workers trading one session.

Ledger
------

Fills, closed trades and net orders go to the backtest's ledger tables;
everything else (signals of intent, submissions, rejections, unfilled orders,
marks, session events) to ``ledger_events``, all on one sequence, all
append-only. An order's state is derived from its events. A restarted session
rebuilds its books from its fills, its strategy state by replaying the bars it
saw, and its in-flight orders from their events, then asks the sandbox what
became of each order it cannot account for (PRD section 24).
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from research.worker import FORBIDDEN_PACKAGES, FORBIDDEN_PREFIXES, install_guard

SANDBOX_FORBIDDEN_PACKAGES = tuple(p for p in FORBIDDEN_PACKAGES if p != "utils.httpx_client")

#: Bumped whenever how a session reaches the sandbox changes; recorded in every
#: session's provenance (PRD section 10).
ADAPTER_VERSION = "1"

INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}

HEARTBEAT_SECONDS = 5
#: A lease not renewed for this long belongs to a worker that is gone.
LEASE_SECONDS = 30
#: How long after a bar's end to wait for its last ticks before closing it.
BAR_GRACE_SECONDS = 2
#: How long a MARKET order may stay open in the sandbox before it is cancelled.
FILL_WAIT_SECONDS = 20
#: No tick from any symbol for this long during market hours means the feed is down.
STALE_SECONDS = 120
#: Calendar days of history fetched before the session, unless a strategy asks
#: for more with a ``warmup_days`` attribute.
DEFAULT_WARMUP_DAYS = 5
SESSION_OPEN_MINUTE = 9 * 60 + 15
SESSION_CLOSE_MINUTE = 15 * 60 + 30
POLL_SECONDS = 0.5

TERMINAL_ORDER_EVENTS = ("ORDER_FILLED", "ORDER_REJECTED", "ORDER_UNFILLED")


def sandbox_forbidden(name: str) -> bool:
    if any(name == p or name.startswith(p + ".") for p in SANDBOX_FORBIDDEN_PACKAGES):
        return True
    return name.startswith(FORBIDDEN_PREFIXES)


class AppError(RuntimeError):
    """OpenAlgo refused or could not answer. The message is OpenAlgo's own."""


class AppClient:
    """OpenAlgo's API, as a hosted strategy reaches it: over HTTP with an API key."""

    def __init__(self, host: str, api_key: str, timeout: float = 10.0) -> None:
        from utils.httpx_client import get_httpx_client

        self.host = host.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client = get_httpx_client()

    def _post(self, path: str, body: dict) -> dict:
        response = self._client.post(
            f"{self.host}{path}", json={**body, "apikey": self.api_key}, timeout=self.timeout
        )
        try:
            data = response.json()
        except ValueError:
            raise AppError(f"OpenAlgo answered {path} with something that is not JSON.") from None
        if data.get("status") != "success":
            raise AppError(str(data.get("message") or f"OpenAlgo refused {path}."))
        return data

    def history(self, symbol: str, exchange: str, interval: str, start: str, end: str) -> list:
        return self._post(
            "/api/v1/history",
            {
                "symbol": symbol,
                "exchange": exchange,
                "interval": interval,
                "start_date": start,
                "end_date": end,
            },
        ).get("data", [])

    def place(self, run_id: int, order: dict) -> dict:
        return self._post("/api/v1/researchsandbox/placeorder", {"run_id": run_id, **order})["data"]

    def status(self, run_id: int, platform_order_id: str) -> dict | None:
        return self._post(
            "/api/v1/researchsandbox/orderstatus",
            {"run_id": run_id, "platform_order_id": platform_order_id},
        )["data"]

    def cancel(self, run_id: int, platform_order_id: str) -> dict | None:
        return self._post(
            "/api/v1/researchsandbox/cancelorder",
            {"run_id": run_id, "platform_order_id": platform_order_id},
        )["data"]


class WebSocketFeed:
    """Quote ticks from OpenAlgo's WebSocket proxy, the platform's own transport."""

    def __init__(self, api_key: str, host: str, port: int) -> None:
        self.api_key, self.host, self.port = api_key, host, port
        self.client = None

    def start(self, symbols: list[str], exchange: str, on_tick) -> None:
        from services.websocket_client import WebSocketClient

        self.client = WebSocketClient(self.api_key, host=self.host, port=self.port)
        if not self.client.connect():
            raise AppError("The market data feed could not be reached.")

        def handle(message: dict) -> None:
            data = message.get("data") or {}
            on_tick(message.get("symbol"), data.get("ltp"), data.get("volume"), time.time())

        self.client.register_callback("market_data", handle)
        self.client.subscribe([{"symbol": s, "exchange": exchange} for s in symbols], mode="Quote")

    def stop(self) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)


class SessionStop(Exception):
    """The session must end now without sending anything else (its lease is gone)."""


class Session:
    """One Sandbox session, driven one ``step`` at a time.

    Everything external is injected (the OpenAlgo client, the tick feed, the
    clock), so tests drive a whole trading day in a fraction of a second.
    """

    def __init__(self, run_id: int, app, feed, clock=time.time) -> None:
        import threading

        from database import research_db as rdb
        from utils.logging import get_logger

        self.run_id = run_id
        self.app = app
        self.feed = feed
        self.clock = clock
        self.rdb = rdb
        self.db = rdb.db_session
        self.logger = get_logger("research.sandbox")
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.lock = threading.Lock()
        self.engine = None
        self.builder = None
        self.waiting: dict[int, dict] = {}  # intent seq -> {intent, deadline, cancel_tried}
        self.equity_rows: list[tuple[int, list, list]] = []
        self.persisted = {"orders": 0, "fills": 0, "trades": 0}
        self.last_heartbeat = 0.0
        self.last_signal = None
        self.last_fill = None
        self.market = {"status": "CONNECTING", "last_tick_at": None}
        self.stopping = False
        self.stop_started = 0.0
        self.stop_failure = None

    # ------------------------------------------------------------ log, lease

    def log(self, message: str, *args, level: str = "info") -> None:
        prefix = f"session={self.run_id} scenario={getattr(self, 'scenario_id', '-')} "
        getattr(self.logger, level)(prefix + message, *args)

    def _runs(self):
        return self.db.query(self.rdb.ResearchRun).filter_by(id=self.run_id)

    def claim(self) -> bool:
        """Take the session if nobody holds a live lease on it."""
        rdb = self.rdb
        now = _utc(self.clock())
        stale = now - timedelta(seconds=LEASE_SECONDS)
        claimed = (
            self.db.query(rdb.ResearchRun)
            .filter(
                rdb.ResearchRun.id == self.run_id,
                rdb.ResearchRun.environment == rdb.SANDBOX,
                rdb.ResearchRun.status.in_(rdb.ACTIVE_STATUSES),
                (rdb.ResearchRun.lease_owner.is_(None)) | (rdb.ResearchRun.heartbeat_at < stale),
            )
            .update(
                {
                    rdb.ResearchRun.lease_owner: self.owner,
                    rdb.ResearchRun.heartbeat_at: now,
                    rdb.ResearchRun.pid: os.getpid(),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        return bool(claimed)

    def still_active(self) -> bool:
        self.db.expire_all()
        run = self._runs().one_or_none()
        return run is not None and run.status in self.rdb.ACTIVE_STATUSES

    def release(self) -> None:
        """Give the lease up, so a successor need not wait for it to expire."""
        try:
            self._runs().filter_by(lease_owner=self.owner).update(
                {self.rdb.ResearchRun.lease_owner: None}, synchronize_session=False
            )
            self.db.commit()
        except Exception:
            self.db.rollback()

    def heartbeat(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self.last_heartbeat < HEARTBEAT_SECONDS:
            return
        alive = (
            self._runs()
            .filter_by(lease_owner=self.owner)
            .update(
                {
                    self.rdb.ResearchRun.heartbeat_at: _utc(now),
                    self.rdb.ResearchRun.live_state: self.live_state(),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        if not alive:
            raise SessionStop()
        self.last_heartbeat = now

    def set_status(self, status: str, message: str | None = None, **values) -> None:
        rdb = self.rdb
        update = {rdb.ResearchRun.status: status, rdb.ResearchRun.message: message}
        update.update({getattr(rdb.ResearchRun, k): v for k, v in values.items()})
        if (
            not self._runs()
            .filter_by(lease_owner=self.owner)
            .update(update, synchronize_session=False)
        ):
            self.db.rollback()
            raise SessionStop()
        self.db.commit()

    def control(self) -> str:
        """The status the app last set: RUNNING, PAUSED or STOPPING."""
        self.db.expire_all()
        run = self._runs().one()
        return run.status

    # ------------------------------------------------------------ ledger

    def event(self, kind: str, payload: dict, *, bar_ts=None, instance=None, order=None) -> int:
        seq = self.engine.reserve_seq()
        self.db.add(
            self.rdb.LedgerEvent(
                run_id=self.run_id,
                seq=seq,
                kind=kind,
                at=_utc(self.clock()),
                bar_ts=bar_ts,
                instance=instance,
                platform_order_id=order,
                payload=payload,
            )
        )
        return seq

    def flush_ledger(self) -> None:
        """Write the engine's new orders, fills and trades, then commit everything."""
        rdb, engine = self.rdb, self.engine
        for name, model in (
            ("orders", rdb.LedgerOrder),
            ("fills", rdb.LedgerFill),
            ("trades", rdb.LedgerTrade),
        ):
            rows = getattr(engine, name)
            for row in rows[self.persisted[name] :]:
                self.db.add(model(run_id=self.run_id, **row))
                if name == "fills":
                    self.last_fill = {
                        k: row[k] for k in ("symbol", "qty", "price", "instance", "ts")
                    }
            self.persisted[name] = len(rows)
        self.db.commit()

    # ------------------------------------------------------------ setup

    def load(self) -> None:
        """Build the engine from the scenario this session's deployment runs."""
        from portfolio.costs import schedule_for
        from research.engine import EngineError, Instance
        from research.live import LiveEngine
        from research.strategy import Config, build, discover, version_identity

        rdb = self.rdb
        run = self._runs().one()
        scenario = self.db.get(rdb.ResearchScenario, run.scenario_id)
        deployment = self.db.get(rdb.ResearchDeployment, run.deployment_id)
        self.scenario_id = scenario.id
        self.trading_date = run.trading_date
        self.stale_policy = deployment.stale_policy
        specs = discover()
        instances, provenance = [], []
        for inst in (
            self.db.query(rdb.ResearchScenarioInstance)
            .filter_by(scenario_id=scenario.id)
            .order_by(rdb.ResearchScenarioInstance.position)
        ):
            version = self.db.get(rdb.ResearchStrategyVersion, inst.strategy_version_id)
            strategy_row = self.db.get(rdb.ResearchStrategy, version.strategy_id)
            config_row = self.db.get(rdb.ResearchConfigVersion, inst.config_version_id)
            spec = specs.get(strategy_row.key)
            if spec is None:
                raise EngineError(f"The strategy {strategy_row.key} is no longer installed.")
            identity = version_identity(spec)
            if identity["version_hash"] != version.version_hash:
                raise EngineError(
                    f"The installed {strategy_row.key} is a different version from the one this "
                    "deployment uses. Install that version again, or deploy the current one."
                )
            instances.append(
                Instance(
                    position=inst.position,
                    label=f"{strategy_row.key} (config v{config_row.version_no})",
                    strategy=build(spec),
                    config=Config.from_dict(config_row.config),
                )
            )
            provenance.append(
                {
                    "instance": inst.position,
                    "strategy_id": strategy_row.id,
                    "strategy_key": strategy_row.key,
                    "strategy_version_id": version.id,
                    "version_hash": version.version_hash,
                    "dist": f"{version.dist_name} {version.dist_version}",
                    "artifact_hash": version.artifact_hash,
                    "model_version": (version.meta or {}).get("model_version"),
                    "config_version_id": config_row.id,
                    "config_hash": config_row.config_hash,
                }
            )
        spec = scenario.dataset_spec
        self.exchange = spec["exchange"]
        self.interval = spec["interval"]
        self.symbols = list(spec["symbols"])
        if self.interval not in INTERVAL_MINUTES:
            raise EngineError(f"{self.interval} bars cannot be traded in the Sandbox yet.")
        cost = scenario.cost_model
        self.engine = LiveEngine(
            scenario.mode,
            instances,
            self.symbols,
            schedule_for(cost["preset"], cost.get("overrides")),
            interval_minutes=INTERVAL_MINUTES[self.interval],
            shared_capital=scenario.shared_capital,
            max_gross_leverage=float(scenario.risk_policy.get("max_gross_leverage", 1.0)),
        )
        self.products = {i.position: i.config.product for i in instances}
        self.warmup_days = max(
            int(getattr(i.strategy, "warmup_days", DEFAULT_WARMUP_DAYS)) for i in instances
        )
        from research import ENGINE_VERSION
        from research.worker import engine_hash

        self.provenance = {
            "instances": provenance,
            "scenario_id": scenario.id,
            "deployment_id": deployment.id,
            "interaction_mode": scenario.mode,
            "market_data_source": "OpenAlgo market data (broker feed)",
            "market_data_transport": "WebSocket quote ticks built into bars; warm-up from the history API",
            "execution_environment": "SANDBOX",
            "execution_model": "MARKET order at the decision bar's close, filled by the sandbox",
            "cost_model": cost,
            "engine_version": ENGINE_VERSION,
            "engine_hash": engine_hash(),
            "adapter_version": ADAPTER_VERSION,
        }

    def _store_bar(self, ts: int, bars: dict, source: str) -> None:
        for symbol, (o, h, low, c, v) in bars.items():
            self.db.merge(
                self.rdb.SessionBar(
                    run_id=self.run_id, symbol=symbol, ts=int(ts), open=float(o), high=float(h),
                    low=float(low), close=float(c), volume=float(v), source=source,
                )
            )  # fmt: skip

    def _history_bars(self, start_date: str, end_date: str, before_ts: float) -> dict:
        """Closed history bars per timestamp, strictly before ``before_ts``."""
        by_ts: dict[int, dict] = {}
        for symbol in self.symbols:
            try:
                rows = self.app.history(symbol, self.exchange, self.interval, start_date, end_date)
            except AppError as exc:
                self.log("warm-up history for %s unavailable: %s", symbol, exc, level="warning")
                continue
            for row in rows:
                ts = int(row.get("timestamp", 0))
                if ts > 10**12:
                    ts //= 1000
                if ts <= 0 or ts + self.engine.interval_minutes * 60 > before_ts:
                    continue
                by_ts.setdefault(ts, {})[symbol] = tuple(
                    float(row.get(k) or 0.0) for k in ("open", "high", "low", "close", "volume")
                )
        return by_ts

    def _append(self, ts: int, bars: dict, source: str, trade: bool) -> list:
        """Add a bar, record it, and either trade on it or only let strategies see it."""
        if self.engine.last_index() >= 0 and ts <= int(self.engine.clock[-1]):
            return []
        i = self.engine.append_bar(ts, bars)
        self._store_bar(ts, bars, source)
        if not trade:
            self.engine.warm(i)
            return []
        intents = self.engine.close_bar(i)
        return intents

    def restore(self) -> bool:
        """Rebuild a session that ran before. Returns whether there was anything to rebuild."""
        rdb = self.rdb
        fills = [
            {
                c.name: getattr(f, c.name)
                for c in rdb.LedgerFill.__table__.columns
                if c.name != "run_id"
            }
            for f in self.db.query(rdb.LedgerFill).filter_by(run_id=self.run_id)
        ]
        events = (
            self.db.query(rdb.LedgerEvent)
            .filter_by(run_id=self.run_id)
            .order_by(rdb.LedgerEvent.seq)
            .all()
        )
        bars = (
            self.db.query(rdb.SessionBar)
            .filter_by(run_id=self.run_id)
            .order_by(rdb.SessionBar.ts)
            .all()
        )
        if not (fills or events or bars):
            return False
        last_seq = 0
        for model in (rdb.LedgerOrder, rdb.LedgerFill, rdb.LedgerTrade, rdb.LedgerEvent):
            top = (
                self.db.query(model.seq)
                .filter_by(run_id=self.run_id)
                .order_by(model.seq.desc())
                .first()
            )
            last_seq = max(last_seq, top[0] if top else 0)
        # Bars first, so the strategies' own state is rebuilt from what they saw.
        grouped: dict[int, dict] = {}
        for bar in bars:
            grouped.setdefault(int(bar.ts), {})[bar.symbol] = (
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
            )
        for ts in sorted(grouped):
            self.engine.warm(self.engine.append_bar(ts, grouped[ts]))
        self.engine.restore(fills, last_seq)
        self.persisted = {"orders": 0, "fills": 0, "trades": 0}
        for name in ("orders", "fills", "trades"):
            getattr(self.engine, name).clear()
        for event in events:
            if event.kind == "MARK":
                self.equity_rows.append(
                    (event.bar_ts, event.payload["books"], event.payload["portfolios"])
                )
        state = self._runs().one().live_state or {}
        self.engine.conflicts = int(state.get("conflicts", 0))
        for book in self.engine.books:
            book.reductions = int(
                (state.get("reductions") or {}).get(str(book.instance.position), 0)
            )
        self._recover_orders(events)
        self.event("SESSION_EVENT", {"event": "resumed", "fills": len(fills), "bars": len(grouped)})
        self.flush_ledger()
        self.log("resumed with %d fills and %d bars", len(fills), len(grouped))
        return True

    def _recover_orders(self, events) -> None:
        """Settle every order decided before the restart from the sandbox's own record."""
        from research.live import OrderIntent

        terminal = {e.platform_order_id for e in events if e.kind in TERMINAL_ORDER_EVENTS}
        filled_orders = {
            row.order_seq
            for row in self.db.query(self.rdb.LedgerFill.order_seq).filter_by(run_id=self.run_id)
            if row.order_seq is not None
        }
        clock = [int(t) for t in self.engine.clock]
        for e in events:
            if e.kind != "ORDER_INTENT" or e.platform_order_id in terminal:
                continue
            p = e.payload
            seq = int(p["seq"])
            if seq in filled_orders:
                continue
            index = clock.index(int(e.bar_ts)) if int(e.bar_ts) in clock else len(clock) - 1
            intent = OrderIntent(
                seq, int(p["portfolio"]), p["symbol"], int(p["net"]), float(p["reference_price"]), index,
                {int(b): [int(d), r] for b, (d, r) in p["entries"].items()},
            )  # fmt: skip
            self.engine._inflight[seq] = intent
            status = self.app.status(self.run_id, e.platform_order_id)
            if status is None:
                # Never reached the sandbox. It was decided before the restart,
                # on a price that is now stale, so it is not sent late.
                self.engine.drop(intent)
                self.event(
                    "ORDER_UNFILLED",
                    {"reason": "not placed before the restart"},
                    order=e.platform_order_id,
                    bar_ts=e.bar_ts,
                )
            else:
                self.waiting[seq] = {
                    "intent": intent,
                    "deadline": self.clock(),
                    "cancel_tried": False,
                }
                self._settle(seq, status)

    def warm_up(self) -> None:
        """History before the session, so strategies have their lookback."""
        today = datetime.strptime(self.trading_date, "%Y-%m-%d")
        start = (today - timedelta(days=self.warmup_days)).strftime("%Y-%m-%d")
        now = self.clock()
        by_ts = self._history_bars(start, self.trading_date, before_ts=now)
        for ts in sorted(by_ts):
            self._append(ts, by_ts[ts], "HISTORY", trade=False)
        self.db.commit()
        self.log("warmed up on %d bars from %s", len(by_ts), start)

    def catch_up(self) -> None:
        """Bars missed while the session was down, seen but never traded on."""
        if self.engine.last_index() < 0:
            return
        by_ts = self._history_bars(self.trading_date, self.trading_date, before_ts=self.clock())
        for ts in sorted(t for t in by_ts if t > int(self.engine.clock[-1])):
            self._append(ts, by_ts[ts], "HISTORY", trade=False)
        self.db.commit()

    # ------------------------------------------------------------ orders

    def submit(self, intents) -> None:
        for intent in intents:
            order_id = intent.platform_order_id(self.run_id)
            bar_ts = int(self.engine.clock[intent.bar_index])
            products = {
                self.products[self.engine.books[b].instance.position] for b in intent.entries
            }
            self.event(
                "ORDER_INTENT",
                {
                    "seq": intent.seq,
                    "portfolio": intent.portfolio,
                    "symbol": intent.symbol,
                    "net": intent.net,
                    "reference_price": intent.reference_price,
                    "entries": {str(b): v for b, v in intent.entries.items()},
                    "instances": {
                        str(self.engine.books[b].instance.position): v[0]
                        for b, v in intent.entries.items()
                    },
                },
                bar_ts=bar_ts,
                order=order_id,
            )
            self.last_signal = {"symbol": intent.symbol, "net": intent.net, "bar_ts": bar_ts}
            self.db.commit()
            try:
                result = self.app.place(
                    self.run_id,
                    {
                        "platform_order_id": order_id,
                        "symbol": intent.symbol,
                        "exchange": self.exchange,
                        "quantity": intent.net,
                        "product": products.pop() if len(products) == 1 else "MIS",
                    },
                )
            except Exception as exc:
                # Placement may or may not have happened; the sandbox's record
                # decides, so the order is tracked rather than assumed.
                self.log("placing %s failed: %s", order_id, exc, level="warning")
                result = None
            self.waiting[intent.seq] = {
                "intent": intent,
                "deadline": self.clock() + FILL_WAIT_SECONDS,
                "cancel_tried": False,
            }
            if result is not None:
                if result.get("orderid"):
                    self.event(
                        "ORDER_SUBMITTED",
                        {"orderid": result["orderid"]},
                        order=order_id,
                        bar_ts=bar_ts,
                    )
                self._settle(intent.seq, result)
        self.flush_ledger()

    def _settle(self, seq: int, status: dict | None) -> None:
        """Apply what the sandbox says about an order; keep waiting if it is still open."""
        entry = self.waiting.get(seq)
        if entry is None:
            return
        intent = entry["intent"]
        order_id = intent.platform_order_id(self.run_id)
        bar_ts = int(self.engine.clock[intent.bar_index])
        state = (status or {}).get("order_status")
        if state == "complete" and int(status.get("filled_quantity") or 0) == abs(intent.net):
            price = float(status["average_price"])
            self.engine.apply_fill(intent, price)
            self.event(
                "ORDER_FILLED",
                {
                    "orderid": status.get("orderid"),
                    "price": price,
                    "reference_price": intent.reference_price,
                    "observed_slippage": (price - intent.reference_price) * intent.net,
                },
                order=order_id,
                bar_ts=bar_ts,
            )
            del self.waiting[seq]
        elif state in ("rejected", "cancelled"):
            self.engine.drop(intent)
            kind = "ORDER_REJECTED" if state == "rejected" else "ORDER_UNFILLED"
            self.event(
                kind,
                {"reason": status.get("rejection_reason") or state},
                order=order_id,
                bar_ts=bar_ts,
            )
            del self.waiting[seq]
        elif state is None and status is not None:
            self.engine.drop(intent)
            self.event(
                "ORDER_REJECTED",
                {"reason": "the sandbox did not accept the order"},
                order=order_id,
                bar_ts=bar_ts,
            )
            del self.waiting[seq]

    def poll_orders(self) -> None:
        now = self.clock()
        for seq in sorted(self.waiting):
            entry = self.waiting[seq]
            order_id = entry["intent"].platform_order_id(self.run_id)
            try:
                status = self.app.status(self.run_id, order_id)
                if status is None:
                    status = {"order_status": None}
                elif (
                    status.get("order_status") in ("open", "trigger pending")
                    and now >= entry["deadline"]
                ):
                    if not entry["cancel_tried"]:
                        entry["cancel_tried"] = True
                        status = self.app.cancel(self.run_id, order_id) or status
                        if status.get("order_status") in ("open", "trigger pending"):
                            self.event(
                                "SESSION_EVENT",
                                {"event": "cancel_refused", "order": order_id},
                                order=order_id,
                            )
            except Exception as exc:
                self.log("could not read %s: %s", order_id, exc, level="warning")
                continue
            self._settle(seq, status)
        self.flush_ledger()

    # ------------------------------------------------------------ the day

    def mark(self, final: bool = False) -> None:
        """Record every book's equity at the latest bar.

        ``final`` marks the session's end, after its closing fills, which land
        after the last bar's mark; without it the result would leave out the
        cost of squaring off.
        """
        i = self.engine.last_index()
        marks = {s: self.engine._mark(s, i) for s in self.symbols}
        books, portfolios = self.engine.equity_at(marks)
        ts = int(self.engine.clock[i])
        if final:
            ts = max(
                self.engine.fill_ts(i), (self.equity_rows[-1][0] + 1) if self.equity_rows else 0
            )
        self.equity_rows.append((ts, books.tolist(), portfolios.tolist()))
        self.event("MARK", {"books": books.tolist(), "portfolios": portfolios.tolist()}, bar_ts=ts)

    def live_state(self) -> dict:
        engine = self.engine
        if engine is None:
            return {"market_data": self.market}
        i = engine.last_index()
        marks = {s: engine._mark(s, i) for s in self.symbols} if i >= 0 else {}
        books = []
        for book in engine.books:
            books.append(
                {
                    "instance": book.instance.position,
                    "label": book.instance.label,
                    "portfolio": book.portfolio,
                    "capital": book.capital,
                    "cash": book.cash,
                    "equity": book.equity(marks) if marks else book.cash,
                    "positions": {
                        s: {
                            "qty": q,
                            "avg": book.avg.get(s),
                            "mark": marks.get(s),
                        }
                        for s, q in sorted(book.qty.items())
                        if q
                    },
                }
            )
        return {
            "market_data": self.market,
            "accept_new": engine.accept_new,
            "books": books,
            "open_orders": [
                {
                    "platform_order_id": e["intent"].platform_order_id(self.run_id),
                    "symbol": e["intent"].symbol,
                    "net": e["intent"].net,
                }
                for e in self.waiting.values()
            ],
            "last_signal": self.last_signal,
            "last_fill": self.last_fill,
            "conflicts": engine.conflicts,
            "reductions": {str(b.instance.position): b.reductions for b in engine.books},
            "last_bar_ts": int(engine.clock[i]) if i >= 0 else None,
            "updated_at": self.clock(),
        }

    def on_tick(self, symbol, ltp, volume, received_at) -> None:
        self.builder.on_tick(symbol, ltp, volume, received_at)

    def _ist_minute(self, epoch: float) -> int:
        from research.strategy import ist_minute

        return ist_minute(int(epoch))

    def begin_stop(self, why: str, reason: str = "square_off", failure: str | None = None) -> None:
        """Stop taking new trades and flatten every book. Idempotent.

        ``failure`` marks a stop that ends the session FAILED even once flat
        (failing safe on stale data); a requested stop or the end of the day
        ends it COMPLETED.
        """
        if self.stopping:
            return
        self.stopping = True
        self.stop_started = self.clock()
        self.stop_failure = failure
        self.engine.accept_new = False
        self.event("SESSION_EVENT", {"event": why})
        self.submit(self.engine.square_off(max(self.engine.last_index(), 0), reason=reason))

    def step(self) -> bool:
        """One pass of the session loop. Returns False once the session has ended."""
        now = self.clock()
        self.heartbeat()
        status = self.control()
        if status in self.rdb.FINAL_STATUSES:
            return False
        if status == self.rdb.STOPPING:
            self.begin_stop("stop requested")
        minute = self._ist_minute(now)
        in_market = SESSION_OPEN_MINUTE <= minute <= SESSION_CLOSE_MINUTE
        last_tick = max(self.builder.last_tick_at.values(), default=None)
        stale = (
            in_market
            and minute >= SESSION_OPEN_MINUTE + 2
            and (last_tick is None or now - last_tick > STALE_SECONDS)
        )
        self.market = {
            "status": "STALE" if stale else ("LIVE" if last_tick else "WAITING"),
            "last_tick_at": last_tick,
        }
        self.engine.accept_new = status == self.rdb.RUNNING and not stale and not self.stopping
        if stale and self.stale_policy == "FAIL_SAFE":
            self.begin_stop(
                "fail safe: market data stopped",
                reason="stale_data",
                failure="Market data stopped arriving, so the session closed its positions and ended.",
            )
        for start, bars in self.builder.closed(now, BAR_GRACE_SECONDS):
            intents = self._append(start, bars, "TICKS", trade=not self.stopping)
            if not self.stopping:
                self.mark()
                self.submit(intents)
        self.poll_orders()

        if minute > SESSION_CLOSE_MINUTE:
            self.begin_stop("the trading day ended", reason="eod")
        if not self.stopping:
            return True
        flat = all(not any(book.qty.values()) for book in self.engine.books)
        if flat and not self.waiting:
            self.complete(failure=self.stop_failure)
            return False
        if now - self.stop_started > FILL_WAIT_SECONDS * 3:
            held = sorted({s for b in self.engine.books for s, q in b.qty.items() if q})
            self.complete(
                failure="Some positions could not be closed in the sandbox: "
                + ", ".join(held or ["orders still open"])
                + ". Check the sandbox positions for this session."
            )
            return False
        return True

    def complete(self, failure: str | None = None) -> None:
        """Finalise the session: metrics, attribution, equity. COMPLETED unless ``failure``."""
        from research.worker import summarize_result, write_equity

        rdb = self.rdb
        self.mark(final=True)
        trades = [
            {
                c.name: getattr(t, c.name)
                for c in rdb.LedgerTrade.__table__.columns
                if c.name != "run_id"
            }
            for t in self.db.query(rdb.LedgerTrade)
            .filter_by(run_id=self.run_id)
            .order_by(rdb.LedgerTrade.seq)
        ]
        import numpy as np

        result = SimpleNamespace(
            timestamps=np.array([r[0] for r in self.equity_rows], dtype=np.int64),
            book_equity=np.array([r[1] for r in self.equity_rows]).T,
            portfolio_equity=np.array([r[2] for r in self.equity_rows]).T,
            trades=trades,
            books=self.engine.books,
            portfolio_capital=self.engine.portfolio_capital,
            conflicts=self.engine.conflicts,
        )
        metrics, attribution = summarize_result(result)
        from research.worker import data_root

        path = data_root() / "runs" / str(self.run_id) / "equity.parquet"
        size, digest = write_equity(result, path)
        self.db.add(
            rdb.ResearchArtifact(
                run_id=self.run_id, kind="equity", uri=str(path), sha256=digest, size_bytes=size
            )
        )
        self.event("SESSION_EVENT", {"event": "failed" if failure else "completed"})
        self.db.commit()
        self.set_status(
            rdb.FAILED if failure else rdb.COMPLETED,
            failure,
            metrics=metrics,
            attribution=attribution,
            progress=1.0,
            finished_at=_utc(self.clock()),
            live_state=self.live_state(),
        )
        self.log("completed: net %.2f over %d trades", metrics["net_pnl"], metrics["trades"])

    def fail(self, message: str) -> None:
        try:
            self.event("SESSION_EVENT", {"event": "failed", "message": message})
            self.db.commit()
        except Exception:
            self.db.rollback()
        self.set_status(
            self.rdb.FAILED, message, finished_at=_utc(self.clock()), live_state=self.live_state()
        )
        self.log("failed: %s", message, level="warning")

    # ------------------------------------------------------------ main

    def start(self) -> None:
        import threading

        from research.live import TickBarBuilder
        from research.strategy import ist_minute

        self.load()
        self.builder = TickBarBuilder(self.symbols, self.engine.interval_minutes, threading.Lock())
        resumed = self.restore()
        if resumed:
            self.catch_up()
        else:
            self.warm_up()
        exit_minutes = [b.instance.config.exit_minute for b in self.engine.books]
        latest_exit = max((m for m in exit_minutes if m is not None), default=SESSION_CLOSE_MINUTE)
        if resumed and ist_minute(int(self.clock())) >= latest_exit:
            # Past the exit time there is nothing to resume: close what is held.
            self.begin_stop("resumed after the exit time; squaring off", reason="eod")
        self.set_status(
            self.rdb.RUNNING,
            None,
            started_at=_utc(self.clock()),
            provenance=self.provenance,
            engine_version=self.provenance["engine_version"],
            engine_hash=self.provenance["engine_hash"],
        )
        self.feed.start(self.symbols, self.exchange, self.on_tick)
        self.heartbeat(force=True)


def run(run_id: int) -> int:
    """Claim and run one session to its end. Returns the process exit code."""
    from research.engine import EngineError
    from utils.logging import get_logger

    logger = get_logger("research.sandbox")
    api_key = os.getenv("OPENALGO_API_KEY", "")
    host = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
    feed = WebSocketFeed(
        api_key, os.getenv("WEBSOCKET_HOST", "127.0.0.1"), int(os.getenv("WEBSOCKET_PORT", "8765"))
    )
    session = Session(run_id, AppClient(host, api_key), feed)

    def terminate(signum, frame):
        raise SessionStop()

    signal.signal(signal.SIGTERM, terminate)
    # A previous worker's lease runs for LEASE_SECONDS after its last heartbeat.
    # After a restart that worker is usually gone already, so wait out its lease
    # rather than leave the session with no worker at all.
    deadline = time.monotonic() + LEASE_SECONDS + HEARTBEAT_SECONDS
    while not session.claim():
        if time.monotonic() > deadline or not session.still_active():
            logger.info("session=%s is owned by another worker or has ended; exiting", run_id)
            session.db.remove()
            return 0
        time.sleep(HEARTBEAT_SECONDS)
    try:
        session.set_status(session.rdb.STARTING, "Starting")
        session.start()
        while session.step():
            time.sleep(POLL_SECONDS)
        return 0
    except SessionStop:
        logger.warning("session=%s lost its lease; stopped without sending anything else", run_id)
        return 0
    except (EngineError, AppError) as exc:
        logger.exception("session=%s could not continue", run_id)
        session.db.rollback()
        session.fail(str(exc))
        return 1
    except Exception:
        logger.exception("session=%s stopped on an unexpected error", run_id)
        session.db.rollback()
        try:
            session.fail(
                "The Sandbox session stopped because of an unexpected error. The session log has the details."
            )
        except SessionStop:
            pass
        return 1
    finally:
        feed.stop()
        session.release()
        session.db.remove()


def main(argv: list[str]) -> int:
    install_guard(sandbox_forbidden, where="a Sandbox session")
    if len(argv) != 2 or not argv[1].isdigit():
        return 2
    return run(int(argv[1]))


if __name__ == "__main__":
    sys.exit(main(sys.argv))

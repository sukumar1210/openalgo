"""
Run one research session in its own process: ``python -m research.sandbox_worker <run_id>``.

A session trades one deployed scenario for one trading day against live market
data. It is the backtest's own engine (``research.live.LiveEngine``) with a
different clock and a different place to get fills, so a strategy keeps its
identity, books and accounting from backtest to Sandbox, Shadow and Live. The
only thing the environment changes is the execution adapter (PRD section 25):

* ``SANDBOX``: ``/api/v1/researchsandbox/*``, OpenAlgo's sandbox fills it
* ``SHADOW``: ``ShadowExecution``, filled inside this process at the latest
  tick; no order leaves it, so a Shadow session has no door to any venue
* ``LIVE``: ``/api/v1/researchlive/*``, the Live door, which places real broker
  orders through OpenAlgo's own live order path under its own limits

The module keeps its Sandbox-era name because every environment runs here.

What it talks to, and what it cannot
------------------------------------

The worker is a client of OpenAlgo, exactly like a hosted Python strategy: it
reads warm-up bars from ``/api/v1/history``, live ticks from the WebSocket
proxy, and sends orders, if its environment sends any, through its door. It
cannot import the broker plugins, order placement, the sandbox itself or the
auth-token store: the backtest's import guard is installed before anything
else, minus only the shared HTTP client. Each door refuses a session of any
other environment.

The API key it needs is a key that could also place a live order through
``/api/v1/placeorder``. So the key leaves the environment before any strategy
is imported, and while strategy code runs it can open no connection or
database and start no process or thread (``research.strategy.strategy_code``).
The broker boundary (PRD sections 15 and 16) is therefore: this process loads
no code that could place a broker order, strategy code cannot reach OpenAlgo's
order API, and a Sandbox or Shadow session's door has no path to a broker.

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
saw, and its in-flight orders from their events, then asks its venue what
became of each order it cannot account for (PRD section 24). An order whose
outcome even the venue's record cannot tell (a Live order caught mid-request by
a crash) is never guessed at: the session stops trading that symbol, closes
everything else and says what to check.
"""

from __future__ import annotations

import copy
import os
import signal
import socket
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from research.worker import (
    FORBIDDEN_PACKAGES,
    FORBIDDEN_PREFIXES,
    RecordWriter,
    event_row,
    install_guard,
    seed_globals,
)

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
#: An order or a status read is answered in seconds; one history call carries a
#: strategy's whole warm-up (weeks of minute bars for one symbol), so it waits longer.
HISTORY_TIMEOUT_SECONDS = 60.0

TERMINAL_ORDER_EVENTS = ("ORDER_FILLED", "ORDER_REJECTED", "ORDER_UNFILLED")
#: What fills a session's orders, in the words its messages use.
VENUE = {"SANDBOX": "the sandbox", "SHADOW": "the Shadow simulation", "LIVE": "the broker"}
#: Set by the app on a worker it restarted: "resume" for a restart, "close" for
#: the last one, which resumes only to close what the session holds.
RESTARTED = "RESEARCH_RESTARTED"
#: The research_controls row whose ``engaged`` stops every Live session.
KILL_SWITCH = "live_kill"
#: The engine's ledger lists the session writes out, oldest first, as they grow.
LEDGER_ROWS = ("orders", "fills", "trades", "events", "positions")


def sandbox_forbidden(name: str) -> bool:
    if any(name == p or name.startswith(p + ".") for p in SANDBOX_FORBIDDEN_PACKAGES):
        return True
    return name.startswith(FORBIDDEN_PREFIXES)


class AppError(RuntimeError):
    """OpenAlgo refused, or did not answer. The message is OpenAlgo's own, or says so."""


class AppClient:
    """OpenAlgo's API, as a hosted strategy reaches it: over HTTP with an API key."""

    def __init__(
        self, host: str, api_key: str, timeout: float = 10.0, door: str = "researchsandbox"
    ) -> None:
        from utils.httpx_client import get_httpx_client

        self.host = host.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.door = door
        self._client = get_httpx_client()

    def with_door(self, door: str) -> AppClient:
        """The same client, sending orders through another execution door."""
        other = copy.copy(self)
        other.door = door
        return other

    def _post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        import httpx

        wait = timeout or self.timeout
        try:
            response = self._client.post(
                f"{self.host}{path}", json={**body, "apikey": self.api_key}, timeout=wait
            )
        except httpx.TimeoutException:
            # Said as itself, so a caller that can do without this one answer
            # (one symbol's warm-up) carries on instead of the session ending.
            raise AppError(f"OpenAlgo did not answer within {wait:g} seconds.") from None
        except httpx.TransportError:
            raise AppError("OpenAlgo could not be reached.") from None
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
            timeout=HISTORY_TIMEOUT_SECONDS,
        ).get("data", [])

    def place(self, run_id: int, order: dict) -> dict:
        return self._post(f"/api/v1/{self.door}/placeorder", {"run_id": run_id, **order})["data"]

    def status(self, run_id: int, platform_order_id: str) -> dict | None:
        return self._post(
            f"/api/v1/{self.door}/orderstatus",
            {"run_id": run_id, "platform_order_id": platform_order_id},
        )["data"]

    def cancel(self, run_id: int, platform_order_id: str) -> dict | None:
        return self._post(
            f"/api/v1/{self.door}/cancelorder",
            {"run_id": run_id, "platform_order_id": platform_order_id},
        )["data"]


class ShadowExecution:
    """Fills each order at once at its symbol's latest tick, sending it nowhere.

    Shadow is observation (PRD section 30): the same session, books and ledger,
    and no order leaves the process, so there is no door to call and nothing to
    refuse. Costs and the scenario's modelled slippage are charged by the engine
    the way a backtest charges them. A symbol with no tick yet has no price, and
    its order is refused rather than filled at a guess.
    """

    def __init__(self, last_price) -> None:
        self.last_price = last_price
        self.orders: dict[str, dict] = {}

    def place(self, run_id: int, order: dict) -> dict:
        order_id = order["platform_order_id"]
        if order_id in self.orders:
            return {**self.orders[order_id], "created": False}
        price = self.last_price(order["symbol"])
        status = {"orderid": None, "platform_order_id": order_id, "order_status": "rejected",
                  "filled_quantity": 0, "average_price": 0.0,
                  "rejection_reason": f"No live price for {order['symbol']} yet"}  # fmt: skip
        if price:
            status.update(
                orderid=f"shadow:{order_id.rsplit(':', 1)[-1]}",
                order_status="complete",
                filled_quantity=abs(int(order["quantity"])),
                average_price=float(price),
                rejection_reason=None,
            )
        self.orders[order_id] = status
        return {**status, "created": True}

    def status(self, run_id: int, platform_order_id: str) -> dict | None:
        return self.orders.get(platform_order_id)

    def cancel(self, run_id: int, platform_order_id: str) -> dict | None:
        return self.orders.get(platform_order_id)


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


def _ist_day_of(trading_date: str) -> int:
    """The IST day number (``research.strategy.ist_day``) of a YYYY-MM-DD date."""
    from datetime import date

    return (date.fromisoformat(trading_date) - date(1970, 1, 1)).days


class SessionStop(Exception):
    """The session must end now without sending anything else (its lease is gone)."""


class Session:
    """One Sandbox session, driven one ``step`` at a time.

    Everything external is injected (the OpenAlgo client, the tick feed, the
    clock), so tests drive a whole trading day in a fraction of a second.
    """

    def __init__(self, run_id: int, app, feed, clock=time.time, execution=None) -> None:
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
        self.records = None  # the day's ctx.record panels, set up in load()
        self.builder = None
        self.waiting: dict[int, dict] = {}  # intent seq -> {intent, deadline, cancel_tried}
        # (bar time, book equity, portfolio equity, book gross exposure,
        #  portfolio gross exposure, portfolio net exposure), one per mark.
        self.equity_rows: list[tuple] = []
        self.persisted = dict.fromkeys(LEDGER_ROWS, 0)
        self.last_heartbeat = 0.0
        self.last_signal = None
        self.last_order = None
        self.last_fill = None
        self.market = {"status": "CONNECTING", "last_tick_at": None}
        self.stopping = False
        self.stop_started = 0.0
        self.stop_failure = None
        self.stop_note = None
        self.strategy_failure = None
        #: Where orders go; chosen by the session's environment in ``load``
        #: unless a test passes its own.
        self.execution = execution
        self.environment = "SANDBOX"
        self.venue = VENUE["SANDBOX"]
        #: Symbols with an order whose outcome is unknown: never ordered again.
        self.unknown_symbols: set[str] = set()
        #: Set when an order's outcome turns out unknown; the next pass of the
        #: loop stops the session with it, outside the order loop that found it.
        self.halt_failure = None
        self.stop_reason = "square_off"
        self.last_square_off = 0.0
        #: Whether the venue last answered (PRD 8 section 30): set by every order call.
        self.venue_status = {"answering": None, "since": None, "error": None}

    # ------------------------------------------------------------ log, lease

    def log(self, message: str, *args, level: str = "info") -> None:
        # The ids master PRD section 42 makes every line searchable by.
        prefix = (
            f"session={self.run_id} experiment={getattr(self, 'experiment_id', '-')} "
            f"scenario={getattr(self, 'scenario_id', '-')} "
        )
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
                rdb.ResearchRun.environment.in_(rdb.SESSION_ENVIRONMENTS),
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
        """The status the app last set: RUNNING, PAUSED or STOPPING.

        A Live session also reads the kill switch on every pass, so engaging it
        stops the session even if the app's own request to stop never arrives.
        """
        self.db.expire_all()
        run = self._runs().one()
        if self.environment == self.rdb.LIVE and self.engine is not None and not self.stopping:
            switch = self.db.get(self.rdb.ResearchControl, KILL_SWITCH)
            if switch is not None and (switch.value or {}).get("engaged"):
                self.stop_note = "Stopped by the kill switch. Its positions were closed."
                self.begin_stop("the kill switch was engaged")
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
        """Write the engine's new ledger rows, then commit everything."""
        rdb, engine = self.rdb, self.engine
        models = {
            "orders": rdb.LedgerOrder,
            "fills": rdb.LedgerFill,
            "trades": rdb.LedgerTrade,
            "positions": rdb.LedgerPosition,
        }
        for name in LEDGER_ROWS:
            rows = getattr(engine, name)
            for row in rows[self.persisted[name] :]:
                if name == "events":
                    self.db.add(event_row(rdb, self.run_id, row, _utc(self.clock())))
                    if row["kind"] == "SIGNAL":
                        self.last_signal = {
                            "instance": row["instance"],
                            "weights": row["payload"]["weights"],
                            "bar_ts": row["bar_ts"],
                        }
                    continue
                self.db.add(models[name](run_id=self.run_id, **row))
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
        from research.strategy import Config, build, discover, instance_seed, version_identity

        rdb = self.rdb
        run = self._runs().one()
        scenario = self.db.get(rdb.ResearchScenario, run.scenario_id)
        deployment = self.db.get(rdb.ResearchDeployment, run.deployment_id)
        self.scenario_id = scenario.id
        self.experiment_id = scenario.experiment_id
        self.trading_date = run.trading_date
        self.stale_policy = deployment.stale_policy
        self.environment = run.environment
        self.venue = VENUE.get(run.environment, "the venue")
        if self.execution is None:
            if run.environment == rdb.SHADOW:
                self.execution = ShadowExecution(self.last_price)
            elif run.environment == rdb.LIVE:
                self.execution = self.app.with_door("researchlive")
            else:
                self.execution = self.app
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
            seed = instance_seed(version.version_hash, config_row.config_hash, inst.position)
            instances.append(
                Instance(
                    position=inst.position,
                    label=f"{strategy_row.key} (config v{config_row.version_no})",
                    strategy=build(spec),
                    config=Config.from_dict(config_row.config),
                    seed=seed,
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
                    "model_hash": version.model_hash,
                    "adapter_version": version.adapter_version,
                    "model_version": (version.meta or {}).get("model_version"),
                    "config_version_id": config_row.id,
                    "config_hash": config_row.config_hash,
                    "seed": seed,
                    # As recorded when it ran: the training period this run is judged
                    # in-sample against, and the operator's notes.
                    "properties": rdb.version_properties(self.db, version),
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
            portfolio_limits=scenario.risk_policy,
            # Modelled only where nothing real fills the order.
            slippage_bps=float(cost.get("slippage_bps", 0.0))
            if run.environment == rdb.SHADOW
            else 0.0,
        )
        # The day's recorded panels. A resumed worker writes the file afresh:
        # replaying the day asks each strategy again, so it records them again.
        from research.worker import data_root

        self.records = RecordWriter(data_root() / "runs" / str(self.run_id) / "records.parquet")
        self.engine.record_sink = self.records
        self.products = {i.position: i.config.product for i in instances}
        self.warmup_days = max(
            int(getattr(i.strategy, "warmup_days", DEFAULT_WARMUP_DAYS)) for i in instances
        )
        from research import ENGINE_VERSION
        from research.worker import engine_hash

        global_seed = seed_globals(provenance)
        self.provenance = {
            "instances": provenance,
            "global_seed": global_seed,
            "experiment_id": scenario.experiment_id,
            "scenario_id": scenario.id,
            "deployment_id": deployment.id,
            "interaction_mode": scenario.mode,
            "market_data_source": "OpenAlgo market data (broker feed)",
            "market_data_transport": "WebSocket quote ticks built into bars; warm-up from the history API",
            "execution_environment": run.environment,
            "execution_model": {
                "SANDBOX": "MARKET order at the decision bar's close, filled by the sandbox",
                "SHADOW": "filled at the latest tick after the decision bar's close, with the "
                "scenario's modelled slippage; no order sent",
                "LIVE": "MARKET order at the decision bar's close, filled by the broker",
            }.get(run.environment),
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
        return self._consult(i, trade)

    def _consult(self, i: int, trade: bool) -> list:
        """Ask the strategies at bar ``i``, or only show it to them.

        A strategy that raises, or answers with something the engine refuses,
        ends the session safely: ``step`` squares off and the session ends
        FAILED. Once a session is stopping no strategy is asked again, so a
        failed one cannot fail the square-off too.
        """
        if self.stopping or self.strategy_failure:
            return []
        try:
            if trade:
                return self.engine.close_bar(i)
            self.engine.warm(i)
        except Exception as exc:
            self._strategy_failed(exc)
        return []

    def _notify(self) -> None:
        """Tell each strategy of the fills settled on its book since the last call."""
        if self.strategy_failure:
            self.engine.discard_notifications()
            return
        try:
            self.engine.notify_fills()
        except Exception as exc:
            self._strategy_failed(exc)

    def _strategy_failed(self, exc: Exception) -> None:
        from research.engine import EngineError

        self.log("a strategy failed; the session will square off", level="exception")
        cause = (
            str(exc)
            if isinstance(exc, EngineError)
            else "A strategy stopped with an error while deciding."
        )
        self.strategy_failure = f"{cause} The session log has the details."

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
        for model in (
            rdb.LedgerOrder,
            rdb.LedgerFill,
            rdb.LedgerTrade,
            rdb.LedgerEvent,
            rdb.LedgerPosition,
        ):
            top = (
                self.db.query(model.seq)
                .filter_by(run_id=self.run_id)
                .order_by(model.seq.desc())
                .first()
            )
            last_seq = max(last_seq, top[0] if top else 0)
        # The day replayed in order: each bar, after the fills that settled before
        # it, so a strategy rebuilding its state sees the positions it held then.
        grouped: dict[int, dict] = {}
        for bar in bars:
            grouped.setdefault(int(bar.ts), {})[bar.symbol] = (
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
            )
        queue = sorted(fills, key=lambda f: f["seq"])
        for ts in sorted(grouped):
            while queue and queue[0]["ts"] <= ts:
                self.engine.replay_fill(queue.pop(0))
            self._notify()
            self._consult(self.engine.append_bar(ts, grouped[ts]), trade=False)
        for fill in queue:
            self.engine.replay_fill(fill)
        self._notify()
        self.engine.finish_restore(last_seq)
        self.persisted = dict.fromkeys(LEDGER_ROWS, 0)
        for name in LEDGER_ROWS:
            getattr(self.engine, name).clear()
        books, portfolios = len(self.engine.books), len(self.engine.portfolio_capital)
        for event in events:
            if event.kind == "MARK":
                p = event.payload
                self.equity_rows.append(
                    (
                        event.bar_ts,
                        p["books"],
                        p["portfolios"],
                        p.get("book_exposure", [0.0] * books),
                        p.get("portfolio_exposure", [0.0] * portfolios),
                        p.get("portfolio_net_exposure", [0.0] * portfolios),
                    )
                )
        state = self._runs().one().live_state or {}
        self.engine.conflicts = int(state.get("conflicts", 0))
        analytics = state.get("analytics") or {}
        self.engine.order_groups = int(analytics.get("order_groups", 0))
        self.engine.bars_seen = int(analytics.get("bars_seen", 0))
        self.engine.netting.update(analytics.get("netting") or {})
        for (a, b), counts in self.engine.overlap.items():
            counts.update((analytics.get("overlap") or {}).get(f"{a}:{b}", {}))
        # A portfolio stopped for the day stays stopped across a restart.
        for portfolio, reason in ((state.get("limits") or {}).get("halted") or {}).items():
            self.engine._halted[int(portfolio)] = reason
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
        """Settle every order decided before the restart from its venue's own record."""
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
            status = self.execution.status(self.run_id, e.platform_order_id)
            if status is None:
                # Never reached the venue. It was decided before the restart,
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
        # The engine recorded each ORDER_INTENT when it decided. They are
        # committed before anything is sent, so a crash after sending still
        # leaves the decision on record.
        self.flush_ledger()
        for intent in intents:
            order_id = intent.platform_order_id(self.run_id)
            bar_ts = int(self.engine.clock[intent.bar_index])
            products = {
                self.products[self.engine.books[b].instance.position] for b in intent.entries
            }
            self.last_order = {"symbol": intent.symbol, "net": intent.net, "bar_ts": bar_ts}
            try:
                result = self.execution.place(
                    self.run_id,
                    {
                        "platform_order_id": order_id,
                        "symbol": intent.symbol,
                        "exchange": self.exchange,
                        "quantity": intent.net,
                        "product": products.pop() if len(products) == 1 else "MIS",
                        # The Live door sizes an order against its limit with it.
                        "reference_price": intent.reference_price,
                    },
                )
            except Exception as exc:
                # Placement may or may not have happened; the venue's record
                # decides, so the order is tracked rather than assumed.
                self.log("order=%s placing failed: %s", order_id, exc, level="warning")
                self._venue(False, exc)
                result = None
            else:
                self._venue(True)
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
        """Apply what the venue says about an order; keep waiting if it is still open."""
        entry = self.waiting.get(seq)
        if entry is None:
            return
        intent = entry["intent"]
        order_id = intent.platform_order_id(self.run_id)
        bar_ts = int(self.engine.clock[intent.bar_index])
        state = (status or {}).get("order_status")
        filled = int((status or {}).get("filled_quantity") or 0)
        if state in ("rejected", "cancelled") and 0 < filled < abs(intent.net):
            # Part filled, the rest never traded: the books take what filled.
            price = float(status.get("average_price") or 0.0)
            self.engine.apply_partial_fill(intent, filled, price)
            sign = 1 if intent.net > 0 else -1
            self.event(
                "ORDER_FILLED",
                {
                    "orderid": status.get("orderid"),
                    "price": price,
                    "reference_price": intent.reference_price,
                    "filled": filled,
                    "requested": abs(intent.net),
                    "partial": True,
                    "observed_slippage": (price - intent.reference_price) * filled * sign,
                },
                order=order_id,
                bar_ts=bar_ts,
            )
            del self.waiting[seq]
            return
        if state == "unknown":
            self._unknown_outcome(seq, intent, order_id)
            return
        if state == "complete" and filled == abs(intent.net):
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
                {"reason": f"{self.venue} did not accept the order"},
                order=order_id,
                bar_ts=bar_ts,
            )
            del self.waiting[seq]

    def _venue(self, answering: bool, error: Exception | None = None) -> None:
        """Track whether the venue answers; the session page shows it."""
        if self.venue_status["answering"] is not answering:
            self.venue_status = {
                "answering": answering,
                "since": self.clock(),
                "error": None if answering else f"{self.venue} did not answer",
            }
        if error is not None:
            self.log("%s did not answer: %s", self.venue, error, level="warning")

    def _unknown_outcome(self, seq: int, intent, order_id: str) -> None:
        """An order the venue may or may not have taken. Nothing more is traded on it.

        Applying it could book a position that does not exist; dropping it could
        leave a real one unmanaged; ordering its symbol again could double one.
        So the session closes everything else, leaves that symbol to the
        operator, and ends FAILED saying exactly what to check. The intent stays
        in flight in the engine, unsettled, and the ledger's reconciliation
        lists it as an order with no final state.
        """
        del self.waiting[seq]
        self.unknown_symbols.add(intent.symbol)
        message = (
            f"The outcome of order {order_id} for {intent.symbol} is unknown: it was being "
            f"sent to {self.venue} when the platform stopped. Check {self.venue}'s order book "
            f"and positions for {intent.symbol} and settle it there."
        )
        self.event(
            "SESSION_EVENT",
            {"event": "order outcome unknown", "symbol": intent.symbol},
            order=order_id,
        )
        self.log("order=%s outcome unknown; stopping", order_id, level="warning")
        if self.stopping:
            self.stop_failure = self.stop_failure or message
        else:
            self.halt_failure = self.halt_failure or message

    def poll_orders(self) -> None:
        now = self.clock()
        for seq in sorted(self.waiting):
            entry = self.waiting[seq]
            order_id = entry["intent"].platform_order_id(self.run_id)
            try:
                status = self.execution.status(self.run_id, order_id)
                if status is None:
                    status = {"order_status": None}
                elif (
                    status.get("order_status") in ("open", "trigger pending")
                    and now >= entry["deadline"]
                ):
                    if not entry["cancel_tried"]:
                        entry["cancel_tried"] = True
                        status = self.execution.cancel(self.run_id, order_id) or status
                        if status.get("order_status") in ("open", "trigger pending"):
                            self.event(
                                "SESSION_EVENT",
                                {"event": "cancel_refused", "order": order_id},
                                order=order_id,
                            )
            except Exception as exc:
                self.log("order=%s could not be read: %s", order_id, exc, level="warning")
                self._venue(False, exc)
                continue
            self._venue(True)
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
        book_gross, port_gross, port_net = self.engine.exposure_at(marks)
        ts = int(self.engine.clock[i])
        if final:
            ts = max(
                self.engine.fill_ts(i), (self.equity_rows[-1][0] + 1) if self.equity_rows else 0
            )
        row = (
            ts,
            books.tolist(),
            portfolios.tolist(),
            book_gross.tolist(),
            port_gross.tolist(),
            port_net.tolist(),
        )
        self.equity_rows.append(row)
        self.event(
            "MARK",
            {
                "books": row[1],
                "portfolios": row[2],
                "book_exposure": row[3],
                "portfolio_exposure": row[4],
                "portfolio_net_exposure": row[5],
            },
            bar_ts=ts,
        )
        if not final:
            # A session's unrealized P&L at each bar it holds something, and
            # what its strategies held together.
            self.engine.snapshot_positions(i)
            self.engine.count_overlap()

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
            "environment": self.environment,
            "market_data": self.market,
            "venue": self.venue_status,
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
            "last_order": self.last_order,
            "last_fill": self.last_fill,
            "conflicts": engine.conflicts,
            "reductions": {str(b.instance.position): b.reductions for b in engine.books},
            # Carried across a restart, so a resumed session's analytics cover its day.
            "analytics": {
                "order_groups": engine.order_groups,
                "bars_seen": engine.bars_seen,
                "netting": engine.netting,
                "overlap": {f"{a}:{b}": c for (a, b), c in engine.overlap.items()},
            },
            "limits": {"halted": {str(p): r for p, r in engine._halted.items()}},
            "last_bar_ts": int(engine.clock[i]) if i >= 0 else None,
            "updated_at": self.clock(),
        }

    def on_tick(self, symbol, ltp, volume, received_at) -> None:
        self.builder.on_tick(symbol, ltp, volume, received_at)

    def last_price(self, symbol: str) -> float | None:
        """The latest tick's price for ``symbol``; a Shadow session fills at it."""
        if self.builder is not None:
            price = self.builder.last_price.get(symbol)
            if price:
                return price
        if self.engine is not None and self.engine.last_index() >= 0:
            mark = self.engine._mark(symbol, self.engine.last_index())
            return mark or None
        return None

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
        self.stop_reason = reason
        self.engine.accept_new = False
        self.event("SESSION_EVENT", {"event": why})
        self._square_off()

    def _square_off(self) -> None:
        """Order every book flat, except what is already being closed or is unknown."""
        self.last_square_off = self.clock()
        self.submit(
            self.engine.square_off(
                max(self.engine.last_index(), 0),
                reason=self.stop_reason,
                exclude=self.unknown_symbols,
            )
        )

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
                failure="Market data stopped arriving.",
            )
        for start, bars in self.builder.closed(now, BAR_GRACE_SECONDS):
            intents = self._append(start, bars, "TICKS", trade=not self.stopping)
            if not self.stopping:
                self.mark()
                self.submit(intents)
        if self.strategy_failure:
            self.begin_stop("a strategy failed; squaring off", failure=self.strategy_failure)
        self.poll_orders()
        if self.halt_failure and not self.stopping:
            self.begin_stop("an order's outcome is unknown", failure=self.halt_failure)
        elif self.stopping and now - self.last_square_off >= FILL_WAIT_SECONDS / 2:
            # An order still in flight when the stop began may have filled since,
            # and a closing order may have been refused: square off again. What
            # an order in flight is already closing is never ordered twice.
            self._square_off()
        self._notify()

        if minute > SESSION_CLOSE_MINUTE:
            self.begin_stop("the trading day ended", reason="eod")
        if not self.stopping:
            return True
        flat = all(not any(book.qty.values()) for book in self.engine.books)
        if flat and not self.waiting:
            self.complete(failure=self._ended(self.stop_failure))
            return False
        if now - self.stop_started > FILL_WAIT_SECONDS * 3:
            held = sorted({s for b in self.engine.books for s, q in b.qty.items() if q})
            self.complete(failure=self._ended(self.stop_failure, held or ["orders still open"]))
            return False
        return True

    def _ended(self, cause: str | None, held: list[str] | None = None) -> str | None:
        """A stopped session's closing message: why it stopped, then what it left.

        Written from what is true at the end, never claimed in advance: a stop
        that could not close everything must not say it did.
        """
        if held:
            left = (
                f"Some positions could not be closed by {self.venue}: {', '.join(held)}. "
                f"Check {self.venue}'s positions for this session."
            )
            return f"{cause} {left}" if cause else left
        if not cause:
            return None
        closed = "everything else it held" if self.unknown_symbols else "its positions"
        return f"{cause} The session closed {closed} and ended."

    def has_traded(self) -> bool:
        """Whether this session ever sent an order, so may hold a position. True if unsure."""
        if self.last_order is not None or self.waiting:
            return True
        try:
            orders = self.db.query(self.rdb.LedgerOrder.seq).filter_by(run_id=self.run_id)
            return orders.first() is not None
        except Exception:
            self.db.rollback()
            return True

    def stopped_on(self, exc: Exception, restarted: str = "") -> int:
        """An error escaped the session loop. Returns the worker's exit code.

        A session that may hold positions never ends here: its worker exits and
        the app starts another, which resumes from the ledger and, on its last
        restart, only closes what is held (resume, else square off). One that
        never sent an order holds nothing, so it ends FAILED saying why.
        """
        from research.engine import EngineError
        from research.strategy import StrategyRefused

        self.db.rollback()
        if restarted or self.has_traded():
            self.log("stopped on an error; the app will resume it", level="exception")
            return 1
        self.log("stopped before placing any order", level="exception")
        known = isinstance(exc, (EngineError, AppError, StrategyRefused))
        try:
            self.fail(
                str(exc)
                if known
                else "The session stopped on an unexpected error before placing any order. "
                "The session log has the details."
            )
        except SessionStop:
            pass
        return 1

    def close_after_errors(self) -> None:
        """The app's last restart: resume only to close what is held, then end FAILED."""
        cause = "The session stopped on repeated errors. The session log has the details."
        self.begin_stop("closing after repeated errors", failure=cause)
        self.stop_failure = self.stop_failure or cause

    def complete(self, failure: str | None = None) -> None:
        """Finalise the session: metrics, attribution, equity. COMPLETED unless ``failure``."""
        from research.worker import summarize_result, write_equity

        rdb = self.rdb
        # The day is over for every strategy: its last fills, then its day's end.
        self._notify()
        if not self.strategy_failure:
            try:
                self.engine.end_day(self.engine.last_index())
            except Exception as exc:
                self._strategy_failed(exc)
        failure = failure or self._ended(self.strategy_failure)
        self.flush_ledger()
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

        rows = self.equity_rows
        result = SimpleNamespace(
            timestamps=np.array([r[0] for r in rows], dtype=np.int64),
            book_equity=np.array([r[1] for r in rows]).T,
            portfolio_equity=np.array([r[2] for r in rows]).T,
            book_gross=np.array([r[3] for r in rows]).T,
            portfolio_gross=np.array([r[4] for r in rows]).T,
            portfolio_net=np.array([r[5] for r in rows]).T,
            buying_power=self.engine.buying_power,
            trades=trades,
            books=self.engine.books,
            portfolio_capital=self.engine.portfolio_capital,
            conflicts=self.engine.conflicts,
            order_groups=self.engine.order_groups,
            netting=self.engine.netting,
            overlap=self.engine.overlap_rows(),
            breaches=self.engine.breaches,
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
        recorded = self.records.close() if self.records is not None else None
        if recorded:
            self.db.add(
                rdb.ResearchArtifact(
                    run_id=self.run_id, kind="records", uri=str(self.records.path),
                    size_bytes=recorded[0], sha256=recorded[1],
                )
            )  # fmt: skip
        self.event("SESSION_EVENT", {"event": "failed" if failure else "completed"})
        self.db.commit()
        self.set_status(
            rdb.FAILED if failure else rdb.COMPLETED,
            failure or self.stop_note,
            metrics=metrics,
            attribution=attribution,
            progress=1.0,
            finished_at=_utc(self.clock()),
            live_state=self.live_state(),
        )
        self.log("completed: net %.2f over %d trades", metrics["net_pnl"], metrics["trades"])

    def fail(self, message: str) -> None:
        if self.records is not None:
            self.records.discard()
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
        self.engine.session_day = _ist_day_of(self.trading_date)
        try:
            self.engine.initialize()
        except Exception as exc:
            self._strategy_failed(exc)
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
    from utils.logging import get_logger

    logger = get_logger("research.sandbox")
    # Out of the environment before any strategy is imported: a strategy written
    # for the Python strategy host reads this name and places orders with it.
    api_key = os.environ.pop("OPENALGO_API_KEY", "")
    host = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
    feed = WebSocketFeed(
        api_key, os.getenv("WEBSOCKET_HOST", "127.0.0.1"), int(os.getenv("WEBSOCKET_PORT", "8765"))
    )
    session = Session(run_id, AppClient(host, api_key), feed)
    restarted = os.getenv(RESTARTED, "")

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
        if restarted == "close":
            session.close_after_errors()
        while session.step():
            time.sleep(POLL_SECONDS)
        return 0
    except SessionStop:
        logger.warning("session=%s lost its lease; stopped without sending anything else", run_id)
        return 0
    except Exception as exc:
        return session.stopped_on(exc, restarted)
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

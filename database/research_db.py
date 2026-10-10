# database/research_db.py
"""
Research: strategy versions, config versions, experiments, runs and the ledger.

Its own database file, ``research.db``, for the reason sandbox.db is separate:
a backtest writes its whole ledger in bursts, and those writes must never
compete for the live trading database's write lock. One file rather than a
research file plus a ledger file, because a run's ledger rows and its COMPLETED
status are committed in one transaction; split across two files a crash between
the commits would leave a completed run with no ledger. The ledger tables carry
a ``ledger_`` prefix so moving them out later is mechanical.

Immutability is enforced here, not by convention. A strategy version, a config
version and a completed run are research records: results are only meaningful
next to the exact inputs that produced them, so nothing that identifies an input
may change after it is written. The ORM refuses updates to the version tables
outright (``_refuse_update``), and run status moves only through compare-and-set
writes so a cancelled or failed run can never be turned into a completed one.

Mirrors database/health_db.py:
- its own URL, ``RESEARCH_DATABASE_URL``, with a default so no deploy script
  needs editing
- SQLite via the canonical engine factory (NullPool, closed immediately)
- scoped_session registered in utils/db_sessions.py for FD hygiene
"""

import os
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from database.engine_factory import create_db_engine
from utils.logging import get_logger

logger = get_logger(__name__)

RESEARCH_DATABASE_URL = os.getenv("RESEARCH_DATABASE_URL", "sqlite:///db/research.db")

engine = create_db_engine(RESEARCH_DATABASE_URL)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


def utc_now():
    """Now in UTC, naive, because SQLite stores no timezone of its own."""
    return datetime.now(UTC).replace(tzinfo=None)


class ImmutableRecordError(RuntimeError):
    """Raised when code tries to change a record that identifies a research input."""


class ResearchStrategy(Base):
    """A logical strategy, keyed by its registry key. Has no mutable state."""

    __tablename__ = "research_strategies"

    id = Column(Integer, primary_key=True)
    key = Column(String(120), nullable=False, unique=True)
    source = Column(String(20), nullable=False)  # builtin | entry_point
    created_at = Column(DateTime, nullable=False, default=utc_now)


class ResearchStrategyVersion(Base):
    """One immutable implementation of a strategy, identified by version_hash."""

    __tablename__ = "research_strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version_hash", name="uq_research_version_hash"),
    )

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("research_strategies.id"), nullable=False)
    version_hash = Column(String(64), nullable=False)
    dist_name = Column(String(200), nullable=False, default="")
    dist_version = Column(String(80), nullable=False, default="")
    code_hash = Column(String(64), nullable=False)
    artifact_hash = Column(String(128), nullable=True)
    #: Declared by the strategy when it carries a model, and its adapter's own
    #: version (PRD section 2). Null for a strategy that declares neither.
    model_hash = Column(String(128), nullable=True)
    adapter_version = Column(String(80), nullable=True)
    interval = Column(String(10), nullable=False)
    meta = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=utc_now)


class ResearchConfigVersion(Base):
    """One immutable deployment configuration of a strategy."""

    __tablename__ = "research_config_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version_no", name="uq_research_config_no"),
        UniqueConstraint("strategy_id", "config_hash", name="uq_research_config_hash"),
    )

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("research_strategies.id"), nullable=False)
    parent_id = Column(Integer, ForeignKey("research_config_versions.id"), nullable=True)
    version_no = Column(Integer, nullable=False)
    config = Column(JSON, nullable=False)
    config_hash = Column(String(64), nullable=False)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)


# --------------------------------------------------------------------------- #
# Experiments, scenarios and runs
# --------------------------------------------------------------------------- #

MODES = ("ISOLATED", "SHARED")

QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED = (
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)
# Sandbox sessions only: a session moves QUEUED -> STARTING -> RUNNING
# (<-> PAUSED) -> STOPPING -> COMPLETED, or ends FAILED or CANCELLED.
STARTING, PAUSED, STOPPING = "STARTING", "PAUSED", "STOPPING"
ACTIVE_STATUSES = (QUEUED, STARTING, RUNNING, PAUSED, STOPPING)
FINAL_STATUSES = (COMPLETED, FAILED, CANCELLED)

#: Where a run executes. A backtest replays a frozen snapshot on a simulated
#: clock; a sandbox session trades live market data with simulated fills from
#: OpenAlgo's sandbox, and can never reach a broker.
BACKTEST, SANDBOX = "BACKTEST", "SANDBOX"
ENVIRONMENTS = (BACKTEST, SANDBOX)


class ResearchExperiment(Base):
    """A set of scenarios evaluated together against the same market data."""

    __tablename__ = "research_experiments"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)


class ResearchScenario(Base):
    """One complete simulation environment. Never edited once created.

    The symbols a scenario loads are the union of its instances' universes, so
    the data request cannot disagree with what the strategies trade.
    """

    __tablename__ = "research_scenarios"

    id = Column(Integer, primary_key=True)
    experiment_id = Column(Integer, ForeignKey("research_experiments.id"), nullable=False)
    name = Column(String(200), nullable=False)
    mode = Column(String(10), nullable=False)
    dataset_spec = Column(JSON, nullable=False)
    dataset_id = Column(Integer, ForeignKey("research_datasets.id"), nullable=True)
    cost_model = Column(JSON, nullable=False)
    execution_model = Column(JSON, nullable=False)
    risk_policy = Column(JSON, nullable=False)
    shared_capital = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)


class ResearchScenarioInstance(Base):
    """One strategy version with one config version inside a scenario."""

    __tablename__ = "research_scenario_instances"
    __table_args__ = (
        UniqueConstraint("scenario_id", "position", name="uq_research_instance_position"),
    )

    id = Column(Integer, primary_key=True)
    scenario_id = Column(Integer, ForeignKey("research_scenarios.id"), nullable=False)
    position = Column(Integer, nullable=False)
    strategy_version_id = Column(
        Integer, ForeignKey("research_strategy_versions.id"), nullable=False
    )
    config_version_id = Column(Integer, ForeignKey("research_config_versions.id"), nullable=False)


class ResearchDataset(Base):
    """A frozen market-data snapshot, identified by the hash of its bar values."""

    __tablename__ = "research_datasets"

    id = Column(Integer, primary_key=True)
    dataset_hash = Column(String(64), nullable=False, unique=True)
    spec = Column(JSON, nullable=False)
    bar_count = Column(Integer, nullable=False)
    uri = Column(String(500), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)


class ResearchDeployment(Base):
    """A scenario deployed to the Sandbox on a schedule.

    Modelled on the Python Strategy Host's schedule: a start and a stop time in
    IST on chosen days, skipped when the exchange is closed. Each scheduled day
    opens one session (a ``research_runs`` row with ``environment = SANDBOX``).
    The scenario is the same immutable object a backtest runs, so a strategy
    keeps its identity from backtest to Sandbox. Mutable on purpose: status and
    schedule are operator settings, not research inputs.
    """

    __tablename__ = "research_deployments"

    id = Column(Integer, primary_key=True)
    scenario_id = Column(Integer, ForeignKey("research_scenarios.id"), nullable=False)
    name = Column(String(200), nullable=False)
    user_id = Column(String(100), nullable=False)
    status = Column(String(10), nullable=False, default="ACTIVE")  # ACTIVE | STOPPED
    schedule_start = Column(String(5), nullable=False)  # HH:MM IST
    schedule_stop = Column(String(5), nullable=False)
    schedule_days = Column(JSON, nullable=False)  # ["mon", ...]
    #: What a session does when its market data goes stale: PAUSE stops new
    #: entries until data returns; FAIL_SAFE squares off and ends the session.
    stale_policy = Column(String(10), nullable=False, default="PAUSE")
    created_at = Column(DateTime, nullable=False, default=utc_now)
    stopped_at = Column(DateTime, nullable=True)


class ResearchRun(Base):
    """One execution of one scenario.

    Failed and cancelled runs are kept: a result that was attempted and did not
    complete is part of the record (PRD section 35). At most one COMPLETED run
    may carry a given run_hash, enforced by a partial unique index, so a
    reproduction can never be stored as a second, independent result.
    """

    __tablename__ = "research_runs"
    __table_args__ = (
        Index(
            "uq_research_run_hash_completed",
            "run_hash",
            unique=True,
            sqlite_where=text("status = 'COMPLETED'"),
            postgresql_where=text("status = 'COMPLETED'"),
        ),
        Index("ix_research_runs_scenario", "scenario_id", "id"),
        # One session per deployment per trading day, so a scheduler that fires
        # twice, or a restart that re-runs the start job, cannot open a second.
        Index(
            "uq_research_session_day",
            "deployment_id",
            "trading_date",
            unique=True,
            sqlite_where=text("deployment_id IS NOT NULL"),
            postgresql_where=text("deployment_id IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True)
    scenario_id = Column(Integer, ForeignKey("research_scenarios.id"), nullable=False)
    run_hash = Column(String(64), nullable=True)
    status = Column(String(12), nullable=False, default=QUEUED)
    progress = Column(Float, nullable=False, default=0.0)
    message = Column(Text, nullable=True)
    pid = Column(Integer, nullable=True)
    engine_version = Column(String(20), nullable=True)
    engine_hash = Column(String(64), nullable=True)
    metrics = Column(JSON, nullable=True)
    attribution = Column(JSON, nullable=True)
    queued_at = Column(DateTime, nullable=False, default=utc_now)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    # Sandbox sessions. A backtest leaves every one of these at its default.
    environment = Column(String(10), nullable=False, default=BACKTEST, server_default=BACKTEST)
    deployment_id = Column(Integer, ForeignKey("research_deployments.id"), nullable=True)
    trading_date = Column(String(10), nullable=True)  # YYYY-MM-DD, IST
    #: Which worker process owns the session, renewed with every heartbeat. A
    #: worker that loses the lease stops trading at once, so a restart can never
    #: leave two workers sending orders for one session.
    lease_owner = Column(String(64), nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    #: The session as of its last heartbeat: books, positions, open orders,
    #: market-data status. What the live session page reads.
    live_state = Column(JSON, nullable=True)
    #: What a sandbox session ran with (PRD section 34). Backtests carry the
    #: same facts in run_hash and the scenario instead.
    provenance = Column(JSON, nullable=True)


class ResearchArtifact(Base):
    """A file a run produced, recorded by location, content hash and size."""

    __tablename__ = "research_artifacts"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("research_runs.id"), nullable=True)
    kind = Column(String(20), nullable=False)
    uri = Column(String(500), nullable=False)
    sha256 = Column(String(64), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)


# --------------------------------------------------------------------------- #
# Ledger. Keyed by (run_id, seq): seq is the order the deterministic engine
# emitted the event in, so a rerun writes byte-identical rows and a retried
# write of the same run collides instead of duplicating.
# --------------------------------------------------------------------------- #


class LedgerOrder(Base):
    """A net market order a portfolio sent. Absent when intents crossed internally."""

    __tablename__ = "ledger_orders"

    run_id = Column(Integer, ForeignKey("research_runs.id"), primary_key=True)
    seq = Column(Integer, primary_key=True)
    ts = Column(BigInteger, nullable=False)
    portfolio = Column(Integer, nullable=False)
    symbol = Column(String(40), nullable=False)
    qty = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    fees = Column(Float, nullable=False)
    slippage = Column(Float, nullable=False)


class LedgerFill(Base):
    """One strategy book's fill. order_seq is null when it crossed internally."""

    __tablename__ = "ledger_fills"
    __table_args__ = (Index("ix_ledger_fills_instance", "run_id", "instance"),)

    run_id = Column(Integer, ForeignKey("research_runs.id"), primary_key=True)
    seq = Column(Integer, primary_key=True)
    order_seq = Column(Integer, nullable=True)
    instance = Column(Integer, nullable=False)
    ts = Column(BigInteger, nullable=False)
    symbol = Column(String(40), nullable=False)
    qty = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    fees = Column(Float, nullable=False)
    slippage = Column(Float, nullable=False)
    reason = Column(String(12), nullable=False)


class LedgerEvent(Base):
    """Everything a sandbox session did that is not a fill or a closed trade.

    SIGNAL, ORDER_INTENT, ORDER_SUBMITTED, ORDER_REJECTED, ORDER_UNFILLED,
    SQUARE_OFF, MARK and SESSION_EVENT, append-only. An order's state is derived
    from its events, never stored and edited, which is what makes a retried
    write collide instead of duplicating (PRD section 37). ``seq`` shares the
    run's sequence with orders, fills and trades, so the four tables interleave
    into one ordered history.
    """

    __tablename__ = "ledger_events"
    __table_args__ = (
        Index("ix_ledger_events_kind", "run_id", "kind"),
        Index("ix_ledger_events_order", "run_id", "platform_order_id"),
    )

    run_id = Column(Integer, ForeignKey("research_runs.id"), primary_key=True)
    seq = Column(Integer, primary_key=True)
    kind = Column(String(20), nullable=False)
    at = Column(DateTime, nullable=False, default=utc_now)  # wall clock, UTC
    bar_ts = Column(BigInteger, nullable=True)  # the bar it belongs to, epoch s
    instance = Column(Integer, nullable=True)
    platform_order_id = Column(String(60), nullable=True)
    payload = Column(JSON, nullable=False, default=dict)


class SessionBar(Base):
    """Every bar a sandbox session saw, warm-up and live, exactly as it saw it.

    Kept so a session can be audited against the data it traded on, resumed
    after a restart with the strategy's state rebuilt from the same bars, and
    replayed as a backtest for the backtest/Sandbox parity check.
    """

    __tablename__ = "research_session_bars"

    run_id = Column(Integer, ForeignKey("research_runs.id"), primary_key=True)
    symbol = Column(String(40), primary_key=True)
    ts = Column(BigInteger, primary_key=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    source = Column(String(10), nullable=False)  # HISTORY | TICKS


class LedgerTrade(Base):
    """A round trip, flat to flat, for one book and one symbol."""

    __tablename__ = "ledger_trades"
    __table_args__ = (Index("ix_ledger_trades_instance", "run_id", "instance"),)

    run_id = Column(Integer, ForeignKey("research_runs.id"), primary_key=True)
    seq = Column(Integer, primary_key=True)
    instance = Column(Integer, nullable=False)
    symbol = Column(String(40), nullable=False)
    side = Column(String(5), nullable=False)
    qty = Column(Integer, nullable=False)
    entry_ts = Column(BigInteger, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_ts = Column(BigInteger, nullable=False)
    exit_price = Column(Float, nullable=False)
    gross_pnl = Column(Float, nullable=False)
    fees = Column(Float, nullable=False)
    slippage = Column(Float, nullable=False)
    net_pnl = Column(Float, nullable=False)


@event.listens_for(ResearchStrategyVersion, "before_update")
@event.listens_for(ResearchConfigVersion, "before_update")
@event.listens_for(ResearchScenario, "before_update")
@event.listens_for(ResearchScenarioInstance, "before_update")
@event.listens_for(ResearchDataset, "before_update")
@event.listens_for(LedgerOrder, "before_update")
@event.listens_for(LedgerFill, "before_update")
@event.listens_for(LedgerTrade, "before_update")
@event.listens_for(LedgerEvent, "before_update")
@event.listens_for(SessionBar, "before_update")
def _refuse_update(mapper, connection, target):
    raise ImmutableRecordError(
        f"{type(target).__name__} {target.id} is immutable; save a new version instead"
    )


def init_db():
    """Create any missing research tables, then settle runs a restart orphaned."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Research DB", logger)
    fail_orphaned_runs()


def fail_orphaned_runs() -> int:
    """Mark backtests left QUEUED or RUNNING by a previous process as FAILED.

    Sandbox sessions are not swept here: an interrupted session is resumed or
    squared off by ``services.research_sandbox_service.recover_sessions``, which
    needs the session left as it was to decide which.

    Called once at startup, before anything can enqueue, so every active run it
    finds belongs to a process that is gone. The worker writes its final status
    with a compare-and-set on RUNNING, so a worker that somehow outlived the
    restart cannot turn one of these back into COMPLETED.
    """
    session = db_session
    try:
        count = (
            session.query(ResearchRun)
            .filter(
                ResearchRun.status.in_(ACTIVE_STATUSES),
                ResearchRun.environment == BACKTEST,
            )
            .update(
                {
                    ResearchRun.status: FAILED,
                    ResearchRun.message: "Interrupted by a restart. Run it again.",
                    ResearchRun.finished_at: utc_now(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Research DB: could not settle interrupted runs")
        return 0
    finally:
        session.remove()
    if count:
        logger.info("Research DB: marked %d interrupted run(s) as failed", count)
    return count


ensure_research_tables_exists = init_db


# --------------------------------------------------------------------------- #
# Serialisers. Plain dicts so blueprints never hold an ORM object past the
# session that loaded it.
# --------------------------------------------------------------------------- #


def _iso(value):
    return value.isoformat() + "Z" if value else None


def strategy_to_dict(row: ResearchStrategy) -> dict:
    return {"id": row.id, "key": row.key, "source": row.source, "created_at": _iso(row.created_at)}


def version_to_dict(row: ResearchStrategyVersion) -> dict:
    return {
        "id": row.id,
        "strategy_id": row.strategy_id,
        "version_hash": row.version_hash,
        "dist_name": row.dist_name,
        "dist_version": row.dist_version,
        "code_hash": row.code_hash,
        "artifact_hash": row.artifact_hash,
        "model_hash": row.model_hash,
        "adapter_version": row.adapter_version,
        "interval": row.interval,
        "meta": row.meta or {},
        "created_at": _iso(row.created_at),
    }


def config_to_dict(row: ResearchConfigVersion) -> dict:
    return {
        "id": row.id,
        "strategy_id": row.strategy_id,
        "parent_id": row.parent_id,
        "version_no": row.version_no,
        "config": row.config,
        "config_hash": row.config_hash,
        "note": row.note,
        "created_at": _iso(row.created_at),
    }


def experiment_to_dict(row: ResearchExperiment) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "created_at": _iso(row.created_at),
    }


def scenario_to_dict(row: ResearchScenario) -> dict:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "name": row.name,
        "mode": row.mode,
        "dataset_spec": row.dataset_spec,
        "dataset_id": row.dataset_id,
        "cost_model": row.cost_model,
        "execution_model": row.execution_model,
        "risk_policy": row.risk_policy,
        "shared_capital": row.shared_capital,
        "created_at": _iso(row.created_at),
    }


def run_to_dict(row: ResearchRun) -> dict:
    return {
        "id": row.id,
        "scenario_id": row.scenario_id,
        "run_hash": row.run_hash,
        "status": row.status,
        "progress": row.progress,
        "message": row.message,
        "engine_version": row.engine_version,
        "engine_hash": row.engine_hash,
        "metrics": row.metrics,
        "attribution": row.attribution,
        "queued_at": _iso(row.queued_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "environment": row.environment or BACKTEST,
        "deployment_id": row.deployment_id,
        "trading_date": row.trading_date,
        "heartbeat_at": _iso(row.heartbeat_at),
    }


def deployment_to_dict(row: ResearchDeployment) -> dict:
    return {
        "id": row.id,
        "scenario_id": row.scenario_id,
        "name": row.name,
        "status": row.status,
        "schedule_start": row.schedule_start,
        "schedule_stop": row.schedule_stop,
        "schedule_days": row.schedule_days,
        "stale_policy": row.stale_policy,
        "created_at": _iso(row.created_at),
        "stopped_at": _iso(row.stopped_at),
    }

"""
Run one scenario in its own process: ``python -m research.worker <run_id>``.

Why a subprocess
----------------

The app serves every request from one eventlet worker. A backtest is minutes of
pure CPU, and CPU work on a green thread never yields, so running one in-process
would freeze every page and every order for its whole duration. A subprocess
also gives each scenario its own memory limit and its own failure: one scenario
running out of memory or hitting a bug ends its own process and marks its own
run FAILED, and cannot touch another scenario's state (PRD UAT phase 6).

The broker boundary
-------------------

Before anything else runs, ``install_guard`` puts an import hook at the front of
``sys.meta_path`` that refuses the order path outright: the broker plugins, the
sandbox, order placement, the broker history service and the auth-token store.
That is the architectural barrier PRD section 65 asks for. It is not a setting:
there is no environment variable or UI switch that turns it off, and it applies
to strategy plugin code as well as to ours, because the plugin is imported after
the hook is in place.

An import hook cannot see a strategy reaching OpenAlgo's own order API over
HTTP, with the ``openalgo`` SDK or any client and a key of its own, and that API
routes on the analyzer switch. So the guard also installs an audit hook: while
strategy code runs (importing a plugin, building it, ``decide``), it cannot open
a network connection or start a process or a thread. What it still cannot stop
is code written to get around it, such as a socket opened through ``ctypes``;
that needs OS-level sandboxing, which this release does not attempt.

Status discipline
-----------------

Every status write is a compare-and-set on the status the worker expects. A run
the operator cancelled, or one the startup sweep failed, therefore can never be
turned into COMPLETED by a worker that finishes late, and the worker stops as
soon as a progress write discovers it has been cancelled.
"""

from __future__ import annotations

import importlib.abc
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

FORBIDDEN_PACKAGES = (
    "broker",
    "sandbox",
    "blueprints",
    "restx_api",
    "websocket_proxy",
    "services.history_service",
    "database.auth_db",
    "utils.httpx_client",
)
FORBIDDEN_PREFIXES = ("services.place_", "services.sandbox")


def is_forbidden(name: str) -> bool:
    if any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PACKAGES):
        return True
    return name.startswith(FORBIDDEN_PREFIXES)


class _RefuseOrderPath(importlib.abc.MetaPathFinder):
    def __init__(self, forbidden=None, where: str = "a backtest"):
        self.forbidden = forbidden or is_forbidden
        self.where = where

    def find_spec(self, fullname, path=None, target=None):
        if self.forbidden(fullname):
            raise ImportError(
                f"{fullname} cannot be used inside {self.where}: it cannot reach a broker"
            )
        return None


def install_guard(forbidden=None, where: str = "a backtest") -> None:
    """Refuse the order path for the rest of this process.

    Args:
        forbidden: Predicate naming the modules to refuse; the backtest set by
            default. The Sandbox worker passes its own, which also allows the
            shared HTTP client it uses to talk to OpenAlgo.
        where: How the refusal names this process.
    """
    forbidden = forbidden or is_forbidden
    if not any(isinstance(f, _RefuseOrderPath) for f in sys.meta_path):
        sys.meta_path.insert(0, _RefuseOrderPath(forbidden, where))
        # Imported only now, under the guard. It lives in research.strategy,
        # never in this module, because this module also runs as __main__ and
        # a second copy of the scope would never see strategy code running.
        from research.strategy import refuse_to_strategies

        # An audit hook cannot be removed, which is the point.
        sys.addaudithook(refuse_to_strategies)
    already = sorted(name for name in sys.modules if forbidden(name))
    if already:
        raise RuntimeError(f"order-path modules were loaded before the guard: {already}")


# Everything below is imported lazily, after the guard, by main().


def data_root() -> Path:
    """Where snapshots and run artifacts live, anchored to the project root.

    Absolute, because the app and the worker both read these paths back and
    must agree on them whatever directory either was started from.
    """
    root = Path(os.getenv("RESEARCH_DATA_DIR", "db/research"))
    return root if root.is_absolute() else Path(__file__).resolve().parents[1] / root


def engine_hash() -> str:
    """Hash of this package's own sources, recorded on every run."""
    from research.strategy import hash_sources

    return hash_sources(Path(__file__).resolve().parent)


def event_row(rdb, run_id: int, row: dict, at):
    """An engine event as its ledger row. ``at`` is when it was recorded."""
    order_seq = row["order_seq"]
    return rdb.LedgerEvent(
        run_id=run_id,
        seq=row["seq"],
        kind=row["kind"],
        at=at,
        bar_ts=row["bar_ts"],
        instance=row["instance"],
        platform_order_id=None if order_seq is None else f"research:{run_id}:{order_seq}",
        payload=row["payload"],
    )


def seed_globals(instances: list[dict]) -> int:
    """Seed the process's shared generators from a run's instance seeds.

    A strategy should draw from ``ctx.rng``; one that reaches for ``random`` or
    ``numpy.random`` instead still gets the same numbers on every rerun.

    Returns:
        The seed, for the run's provenance.
    """
    import random

    import numpy as np

    from research.strategy import instance_seed

    seed = instance_seed("|".join(str(i["seed"]) for i in instances), "global", len(instances))
    random.seed(seed)
    np.random.seed(seed % 2**32)
    return seed


def scenario_identity(session, scenario, dataset_hash: str) -> tuple[str, dict]:
    """The run_hash of a scenario on a given dataset, and what went into it."""
    from database import research_db as rdb
    from research import CONFLICT_POLICY, ENGINE_VERSION
    from research.strategy import canonical_json, sha256_hex

    instances = []
    for inst in (
        session.query(rdb.ResearchScenarioInstance)
        .filter_by(scenario_id=scenario.id)
        .order_by(rdb.ResearchScenarioInstance.position)
    ):
        version = session.get(rdb.ResearchStrategyVersion, inst.strategy_version_id)
        config = session.get(rdb.ResearchConfigVersion, inst.config_version_id)
        instances.append(
            {
                "position": inst.position,
                "version_hash": version.version_hash,
                "config_hash": config.config_hash,
            }
        )
    identity = {
        "instances": instances,
        "dataset_hash": dataset_hash,
        "mode": scenario.mode,
        "cost_model": scenario.cost_model,
        "execution_model": scenario.execution_model,
        "risk_policy": scenario.risk_policy,
        "shared_capital": scenario.shared_capital,
        "engine_version": ENGINE_VERSION,
        "conflict_policy": CONFLICT_POLICY,
    }
    return sha256_hex(canonical_json(identity)), identity


class Stop(Exception):
    """The run was cancelled or settled elsewhere; stop without writing a result."""


def execute(run_id: int) -> str:
    """Run one scenario to a final status. Returns that status."""
    from database import research_db as rdb
    from research import ENGINE_VERSION
    from research.dataset import (
        DatasetError,
        DatasetSpec,
        bar_count,
        dataset_facts,
        dataset_hash,
        load,
        read_snapshot,
        write_snapshot,
    )
    from research.engine import Engine, EngineError, Instance
    from research.strategy import (
        Config,
        StrategyRefused,
        build,
        discover,
        instance_seed,
        version_identity,
    )
    from utils.logging import get_logger

    logger = get_logger("research.worker")
    session = rdb.db_session
    runs = session.query(rdb.ResearchRun).filter_by(id=run_id)

    claimed = (
        session.query(rdb.ResearchRun)
        .filter_by(id=run_id, status=rdb.QUEUED)
        .update(
            {
                rdb.ResearchRun.status: rdb.RUNNING,
                rdb.ResearchRun.started_at: rdb.utc_now(),
                rdb.ResearchRun.pid: os.getpid(),
                rdb.ResearchRun.message: "Starting",
            },
            synchronize_session=False,
        )
    )
    session.commit()
    if not claimed:
        return "SKIPPED"

    def progress(fraction: float, message: str | None = None) -> None:
        values = {rdb.ResearchRun.progress: round(fraction, 4)}
        if message:
            values[rdb.ResearchRun.message] = message
        alive = runs.filter_by(status=rdb.RUNNING).update(values, synchronize_session=False)
        session.commit()
        if not alive:
            raise Stop()

    def finish(status: str, message: str, extra: dict | None = None) -> str:
        values = dict(extra or {})
        values.update(
            {
                rdb.ResearchRun.status: status,
                rdb.ResearchRun.message: message,
                rdb.ResearchRun.finished_at: rdb.utc_now(),
            }
        )
        runs.filter_by(status=rdb.RUNNING).update(values, synchronize_session=False)
        session.commit()
        return status

    ids = f"run={run_id}"
    records = None  # the run's recorded panels; discarded unless the run completes
    try:
        run = runs.one()
        scenario = session.get(rdb.ResearchScenario, run.scenario_id)
        # The ids master PRD section 42 makes every line searchable by.
        ids = f"run={run_id} experiment={scenario.experiment_id} scenario={scenario.id}"
        specs = discover()

        progress(0.01, "Loading strategies")
        instances, provenance = [], []
        for inst in (
            session.query(rdb.ResearchScenarioInstance)
            .filter_by(scenario_id=scenario.id)
            .order_by(rdb.ResearchScenarioInstance.position)
        ):
            version = session.get(rdb.ResearchStrategyVersion, inst.strategy_version_id)
            strategy_row = session.get(rdb.ResearchStrategy, version.strategy_id)
            config_row = session.get(rdb.ResearchConfigVersion, inst.config_version_id)
            spec = specs.get(strategy_row.key)
            if spec is None:
                raise EngineError(
                    f"The strategy {strategy_row.key} is no longer installed on this server."
                )
            if version_identity(spec)["version_hash"] != version.version_hash:
                raise EngineError(
                    f"The installed {strategy_row.key} is a different version from the one this "
                    "scenario uses. Install that version again, or add a new scenario that uses "
                    "the current one."
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
                    "config_version_id": config_row.id,
                    "config_hash": config_row.config_hash,
                    "seed": seed,
                    # As recorded when it ran: the training period this run is judged
                    # in-sample against, and the operator's notes.
                    "properties": rdb.version_properties(session, version),
                }
            )

        progress(0.03, "Loading market data")
        if scenario.dataset_id:
            dataset = session.get(rdb.ResearchDataset, scenario.dataset_id)
            frames = read_snapshot(Path(dataset.uri))
            if dataset_hash(frames) != dataset.dataset_hash:
                raise DatasetError(
                    "The stored market data for this scenario has changed on disk, so its "
                    "results could not be reproduced. Add a new scenario to load fresh data."
                )
        else:
            spec = DatasetSpec(**scenario.dataset_spec)
            frames = load(spec)
            digest = dataset_hash(frames)
            dataset = session.query(rdb.ResearchDataset).filter_by(dataset_hash=digest).first()
            if dataset is None:
                path = data_root() / "datasets" / f"{digest}.parquet"
                size = write_snapshot(frames, path)
                dataset = rdb.ResearchDataset(
                    dataset_hash=digest,
                    spec=spec.to_dict(),
                    bar_count=bar_count(frames),
                    uri=str(path),
                    size_bytes=size,
                    source=spec.source,
                    **dataset_facts(frames),
                )
                session.add(dataset)
                session.flush()
            # Pin the snapshot to the scenario once. A bulk update, because the
            # ORM refuses edits to a scenario; the NULL guard makes it set-once.
            session.query(rdb.ResearchScenario).filter_by(id=scenario.id, dataset_id=None).update(
                {rdb.ResearchScenario.dataset_id: dataset.id}, synchronize_session=False
            )
            session.commit()

        run_hash, _identity = scenario_identity(session, scenario, dataset.dataset_hash)
        twin = (
            session.query(rdb.ResearchRun.id)
            .filter_by(run_hash=run_hash, status=rdb.COMPLETED)
            .first()
        )
        if twin:
            return finish(
                rdb.CANCELLED,
                f"Not run again: run #{twin.id} already has the result for exactly these inputs.",
                {rdb.ResearchRun.run_hash: run_hash},
            )

        from portfolio.costs import schedule_for
        from research import CONFLICT_POLICY

        global_seed = seed_globals(provenance)
        run_provenance = {
            "instances": provenance,
            "global_seed": global_seed,
            "experiment_id": scenario.experiment_id,
            "scenario_id": scenario.id,
            "dataset_id": dataset.id,
            "dataset_hash": dataset.dataset_hash,
            "interaction_mode": scenario.mode,
            "execution_environment": "BACKTEST",
            "execution_model": scenario.execution_model,
            "cost_model": scenario.cost_model,
            "risk_policy": scenario.risk_policy,
            "engine_version": ENGINE_VERSION,
            "engine_hash": engine_hash(),
            "conflict_policy": CONFLICT_POLICY,
            "run_hash": run_hash,
        }
        cost_model = scenario.cost_model
        engine = Engine(
            scenario.mode,
            instances,
            frames,
            schedule_for(cost_model["preset"], cost_model.get("overrides")),
            slippage_bps=float(cost_model.get("slippage_bps", 0.0)),
            shared_capital=scenario.shared_capital,
            max_gross_leverage=float(scenario.risk_policy.get("max_gross_leverage", 1.0)),
            portfolio_limits=scenario.risk_policy,
        )
        records = RecordWriter(data_root() / "runs" / str(run_id) / "records.parquet")
        engine.record_sink = records
        progress(0.05, "Replaying bars")
        result = engine.run(on_progress=lambda f: progress(0.05 + 0.9 * f))
        recorded = records.close()

        progress(0.96, "Saving results")
        metrics, attribution = summarize_result(result)
        equity_path = data_root() / "runs" / str(run_id) / "equity.parquet"
        size, digest = write_equity(result, equity_path)
        for row in result.orders:
            session.add(rdb.LedgerOrder(run_id=run_id, **row))
        for row in result.fills:
            session.add(rdb.LedgerFill(run_id=run_id, **row))
        for row in result.trades:
            session.add(rdb.LedgerTrade(run_id=run_id, **row))
        for row in result.positions:
            session.add(rdb.LedgerPosition(run_id=run_id, **row))
        for row in result.events:
            # A backtest's events happen in market time, so that is their time.
            at = datetime.fromtimestamp(row["bar_ts"], UTC).replace(tzinfo=None)
            session.add(event_row(rdb, run_id, row, at))
        session.add(
            rdb.ResearchArtifact(
                run_id=run_id, kind="equity", uri=str(equity_path), sha256=digest, size_bytes=size
            )
        )
        if recorded:
            session.add(
                rdb.ResearchArtifact(
                    run_id=run_id, kind="records", uri=str(records.path),
                    size_bytes=recorded[0], sha256=recorded[1],
                )
            )  # fmt: skip
        completed = runs.filter_by(status=rdb.RUNNING).update(
            {
                rdb.ResearchRun.status: rdb.COMPLETED,
                rdb.ResearchRun.message: None,
                rdb.ResearchRun.progress: 1.0,
                rdb.ResearchRun.run_hash: run_hash,
                rdb.ResearchRun.engine_version: ENGINE_VERSION,
                rdb.ResearchRun.engine_hash: engine_hash(),
                rdb.ResearchRun.metrics: metrics,
                rdb.ResearchRun.attribution: attribution,
                rdb.ResearchRun.provenance: run_provenance,
                rdb.ResearchRun.finished_at: rdb.utc_now(),
            },
            synchronize_session=False,
        )
        if not completed:
            session.rollback()
            return "STOPPED"
        session.commit()
        records = None  # committed with its artifact row: kept
        return rdb.COMPLETED
    except Stop:
        session.rollback()
        return "STOPPED"
    except MemoryError:
        session.rollback()
        logger.exception("%s ran out of memory", ids)
        return finish(
            rdb.FAILED,
            "The backtest ran out of memory. Try a shorter window or fewer symbols.",
        )
    except (EngineError, DatasetError, StrategyRefused) as exc:
        session.rollback()
        logger.exception("%s failed", ids)
        return finish(rdb.FAILED, str(exc))
    except Exception:
        session.rollback()
        logger.exception("%s failed", ids)
        return finish(
            rdb.FAILED,
            "The backtest stopped because of an unexpected error. The run log has the details.",
        )
    finally:
        if records is not None:
            records.discard()
        session.remove()


def summarize_result(result) -> tuple[dict, list[dict]]:
    """Portfolio metrics plus one attribution row per strategy book."""
    from research.metrics import return_correlation, summarize

    capital = sum(result.portfolio_capital)
    total = result.portfolio_equity.sum(axis=0)
    gross = getattr(result, "portfolio_gross", None)
    portfolio = summarize(
        result.timestamps,
        total,
        capital,
        result.trades,
        sum(b.traded_value for b in result.books),
        exposure=None if gross is None else gross.sum(axis=0),
        buying_power=sum(getattr(result, "buying_power", None) or []) or None,
    )
    portfolio["conflicts"] = result.conflicts
    book_gross = getattr(result, "book_gross", None)
    attribution = []
    for n, book in enumerate(result.books):
        position = book.instance.position
        trades = [t for t in result.trades if t["instance"] == position]
        row = summarize(
            result.timestamps,
            result.book_equity[n],
            book.capital,
            trades,
            book.traded_value,
            exposure=None if book_gross is None else book_gross[n],
            buying_power=book.instance.config.sizing_capital,
        )
        row.update(
            {
                "instance": position,
                "label": book.instance.label,
                "portfolio": book.portfolio,
                "reduced_by_limit": book.reductions,
                "contribution_pct": (
                    row["net_pnl"] / portfolio["net_pnl"] * 100 if portfolio["net_pnl"] else None
                ),
            }
        )
        attribution.append(row)
    portfolio["limit_breaches"] = list(getattr(result, "breaches", None) or [])
    if len(result.books) > 1:
        groups = getattr(result, "order_groups", 0)
        portfolio["together"] = {
            "order_groups": groups,
            "conflict_rate_pct": result.conflicts / groups * 100 if groups else None,
            "netting": dict(getattr(result, "netting", None) or {}),
            "overlap": list(getattr(result, "overlap", None) or []),
            "correlation": return_correlation(
                result.timestamps,
                {b.instance.position: result.book_equity[n] for n, b in enumerate(result.books)},
            ),
            "reductions": sum(b.reductions for b in result.books),
        }
    return portfolio, attribution


class RecordWriter:
    """A run's recorded panels (``ctx.record``), streamed to parquet in chunks.

    Long backtests record millions of values, so rows go to the file every
    CHUNK rows instead of collecting in memory. The file is opened on the first
    row: a run that records nothing has no file and no artifact.
    """

    CHUNK = 100_000

    def __init__(self, path: Path) -> None:
        self.path = path
        self._writer = None
        self._rows: dict[str, list] = {
            k: [] for k in ("timestamp", "instance", "name", "symbol", "value")
        }

    def __call__(self, ts: int, instance: int, name: str, values) -> None:
        rows = self._rows
        for symbol, value in values.items():
            rows["timestamp"].append(ts)
            rows["instance"].append(instance)
            rows["name"].append(name)
            rows["symbol"].append(symbol)
            rows["value"].append(value)
        if len(rows["timestamp"]) >= self.CHUNK:
            self._flush()

    def _flush(self) -> None:
        if not self._rows["timestamp"]:
            return
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.table(
            {
                "timestamp": pa.array(self._rows["timestamp"], pa.int64()),
                "instance": pa.array(self._rows["instance"], pa.int32()),
                "name": pa.array(self._rows["name"], pa.string()),
                "symbol": pa.array(self._rows["symbol"], pa.string()),
                "value": pa.array(self._rows["value"], pa.float64()),
            }
        )
        if self._writer is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._writer = pq.ParquetWriter(self.path, table.schema)
        self._writer.write_table(table)
        for column in self._rows.values():
            column.clear()

    def close(self) -> tuple[int, str] | None:
        """Finish the file. Returns (size in bytes, sha256), or None if nothing was recorded."""
        import hashlib

        self._flush()
        if self._writer is None:
            return None
        self._writer.close()
        self._writer = None
        data = self.path.read_bytes()
        return len(data), hashlib.sha256(data).hexdigest()

    def discard(self) -> None:
        """Close and delete a file a run that did not complete left behind."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self.path.unlink(missing_ok=True)


def write_equity(result, path: Path) -> tuple[int, str]:
    """The equity curves as parquet. Returns (size in bytes, sha256 of the file)."""
    import hashlib

    import pandas as pd

    columns = {"timestamp": result.timestamps.astype("int64")}
    columns["portfolio"] = result.portfolio_equity.sum(axis=0)
    for n, book in enumerate(result.books):
        columns[f"instance_{book.instance.position}"] = result.book_equity[n]
    gross = getattr(result, "portfolio_gross", None)
    if gross is not None and gross.size:
        columns["exposure_portfolio"] = gross.sum(axis=0)
        columns["net_exposure_portfolio"] = result.portfolio_net.sum(axis=0)
        for n, book in enumerate(result.books):
            columns[f"exposure_instance_{book.instance.position}"] = result.book_gross[n]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns).to_parquet(path, index=False)
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def main(argv: list[str]) -> int:
    install_guard()
    # A backtest never needs the network, so for the whole life of this process,
    # not only while strategy code runs, nothing may reach it.
    from research.strategy import refuse_network

    sys.addaudithook(refuse_network)
    if len(argv) != 2 or not argv[1].isdigit():
        return 2
    status = execute(int(argv[1]))
    return 0 if status in ("COMPLETED", "CANCELLED", "SKIPPED", "STOPPED") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

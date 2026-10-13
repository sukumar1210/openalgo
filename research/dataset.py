"""
Market-data snapshots: what a backtest replays, frozen and hashed.

A backtest is only reproducible if its bars cannot change underneath it.
Historify is a live store: a re-download, a backfill or a corrected session
rewrites rows in place, so a scenario that read Historify on every run would
quietly replay different bars on its second run. Instead the first run of a
scenario reads Historify once, writes the bars to a parquet snapshot, and every
later run of that scenario reads the snapshot and nothing else.

The snapshot is identified by a hash of its bar **values**, not of the file:
parquet is not byte-stable across library versions, so a file hash would report
a change where none exists. Two snapshots with the same hash replay the same
bars, whatever produced them.

Where bars come from is a ``DatasetProvider`` (master PRD section 6). The one
provider is Historify's DuckDB store, OpenAlgo's own historical data; another
store would be a second provider registered in ``PROVIDERS``, chosen by the
spec's ``source``, and nothing downstream of ``load`` would change. A
provider never fills a gap: a symbol with no bars is refused by name, and a bar
missing inside the window stays missing (the engine treats it as no price, not
as zero).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from research.strategy import IST_OFFSET_SECONDS

COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close", "volume")


def epoch_window(start: str, end: str) -> tuple[int, int]:
    """IST calendar dates ``start`` to ``end`` as epoch seconds, both ends inclusive."""
    first = datetime.fromisoformat(start)
    after = datetime.fromisoformat(end) + timedelta(days=1)
    epoch = datetime(1970, 1, 1)
    return (
        int((first - epoch).total_seconds()) - IST_OFFSET_SECONDS,
        int((after - epoch).total_seconds()) - IST_OFFSET_SECONDS - 1,
    )


#: A stored range that starts or ends this many days inside a window still
#: covers it: a window may begin or end on a weekend or a holiday, which has no
#: bars, and asking the broker again for those days would never fill them.
COVERAGE_SLACK_DAYS = 5


class DatasetError(ValueError):
    """The requested bars cannot be provided. The message is for the trader."""


@dataclass(frozen=True)
class DatasetSpec:
    symbols: tuple[str, ...]
    exchange: str
    interval: str
    start: str  # YYYY-MM-DD, IST, inclusive
    end: str  # YYYY-MM-DD, IST, inclusive
    source: str = "historify"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(sorted(set(self.symbols))))
        if self.source not in PROVIDERS:
            raise DatasetError("That market data source is not available on this server.")
        try:
            start = date.fromisoformat(self.start)
            end = date.fromisoformat(self.end)
        except ValueError as exc:
            raise DatasetError("Dates must look like 2024-01-31.") from exc
        if end < start:
            raise DatasetError("The end date must be on or after the start date.")
        if not self.symbols:
            raise DatasetError("The scenario has no symbols to load.")

    def to_dict(self) -> dict:
        return {
            "symbols": list(self.symbols),
            "exchange": self.exchange,
            "interval": self.interval,
            "start": self.start,
            "end": self.end,
            "source": self.source,
        }

    def epoch_window(self) -> tuple[int, int]:
        """The IST calendar window as epoch seconds, both ends inclusive."""
        return epoch_window(self.start, self.end)


Frames = dict[str, pd.DataFrame]


def normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """The columns the engine uses, typed, sorted, one row per timestamp."""
    out = frame.loc[:, list(COLUMNS)].copy()
    out["timestamp"] = out["timestamp"].astype("int64")
    for column in PRICE_COLUMNS:
        out[column] = out[column].astype("float64")
    out = out.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    return out.reset_index(drop=True)


def dataset_hash(frames: Frames) -> str:
    """sha256 over symbols in sorted order, each as its name then its arrays."""
    digest = hashlib.sha256()
    for symbol in sorted(frames):
        frame = frames[symbol]
        digest.update(symbol.encode("utf-8") + b"\0")
        digest.update(np.ascontiguousarray(frame["timestamp"].to_numpy("<i8")).tobytes())
        for column in PRICE_COLUMNS:
            digest.update(np.ascontiguousarray(frame[column].to_numpy("<f8")).tobytes())
    return digest.hexdigest()


def load_from_historify(spec: DatasetSpec) -> Frames:
    """Read every symbol's bars once from Historify.

    Historify answers both "no rows" and "could not read the store" with an
    empty frame, so a missing symbol is reported with both possibilities, the
    one an operator can check first.
    """
    from database import historify_db

    start_ts, end_ts = spec.epoch_window()
    frames: Frames = {}
    for symbol in spec.symbols:
        raw = historify_db.get_ohlcv(symbol, spec.exchange, spec.interval, start_ts, end_ts)
        if raw is None or raw.empty:
            raise DatasetError(
                f"No stored {spec.interval} bars for {symbol} ({spec.exchange}) between "
                f"{spec.start} and {spec.end}. Download them in Historify, then run again. "
                "If they are already there, the run log has the details."
            )
        frames[symbol] = normalise(raw)
    return frames


class DatasetProvider(Protocol):
    """Where a scenario's bars come from, before they are frozen in a snapshot."""

    source: str

    def load(self, spec: DatasetSpec) -> Frames: ...


class HistorifyDuckDBProvider:
    """OpenAlgo's own historical store (``db/historify.duckdb``)."""

    source = "historify"

    def load(self, spec: DatasetSpec) -> Frames:
        return load_from_historify(spec)


PROVIDERS: dict[str, DatasetProvider] = {"historify": HistorifyDuckDBProvider()}


def load(spec: DatasetSpec) -> Frames:
    """The spec's bars, from the provider its ``source`` names."""
    return PROVIDERS[spec.source].load(spec)


def dataset_facts(frames: Frames) -> dict:
    """What a dataset row records about its bars beyond their hash."""
    stamps = [f["timestamp"] for f in frames.values() if len(f)]
    return {
        "symbol_count": len(frames),
        "first_ts": int(min(s.iloc[0] for s in stamps)) if stamps else None,
        "last_ts": int(max(s.iloc[-1] for s in stamps)) if stamps else None,
    }


def write_snapshot(frames: Frames, path: Path) -> int:
    """Write the snapshot as one long parquet table. Returns its size in bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [frames[s].assign(symbol=s) for s in sorted(frames)]
    table = pd.concat(parts, ignore_index=True)[["symbol", *COLUMNS]]
    tmp = path.with_suffix(".tmp")
    table.to_parquet(tmp, index=False)
    tmp.replace(path)
    return path.stat().st_size


def read_snapshot(path: Path, since: int | None = None, until: int | None = None) -> Frames:
    """A snapshot's bars by symbol; only those from ``since`` to ``until`` (epoch seconds) if given.

    The bounds are applied as the file is read, so bars outside them never take memory.
    """
    filters = [("timestamp", ">=", int(since))] if since is not None else []
    if until is not None:
        filters.append(("timestamp", "<=", int(until)))
    table = pd.read_parquet(path, filters=filters or None)
    return {symbol: normalise(group) for symbol, group in table.groupby("symbol", sort=True)}


def bar_count(frames: Frames) -> int:
    return int(sum(len(f) for f in frames.values()))

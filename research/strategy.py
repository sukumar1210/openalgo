"""
The strategy plugin contract, discovery, and version identity.

One interface for every mode
----------------------------

A strategy is an object with an ``interval`` and a ``decide(ctx)`` method. The
engine calls ``decide`` once per closed bar inside the configured entry window
and the strategy answers with the book it wants to hold: a mapping of symbol to
signed weight, where 1.0 means the whole of the instance's sized capital long
and -1.0 the same short. A symbol left out of the mapping is a target of zero.
``None`` means "no change, keep what I hold".

Weights rather than quantities, because capital, allocation and leverage are
deployment knobs that belong to the configuration, not to the strategy. The
same strategy version run on Rs 5 lakh and on Rs 2 lakh is the same research
object; only the config version differs. Research and model parameters are the
opposite: they live inside the strategy's own versioned code or artifact and are
never exposed as configuration (PRD section 6).

The strategy sees a :class:`Context` and nothing else. It cannot see a broker,
it cannot see the future (``bars()`` is sliced up to the current bar and the
arrays are read-only), and it cannot see another strategy's book. That is the
same contract in single, isolated and shared mode, which is what makes the three
comparable.

Lifecycle hooks
---------------

``decide`` is the only method a strategy must have. It may also define, and
the engine calls only those it finds:

* ``metadata`` (a mapping, or a method returning one), recorded on the version
* ``initialize(setup)`` once, before the first bar of a run or session, with a
  :class:`SetupContext`
* ``on_fill(fill)`` after every fill applied to its own book, with a
  :class:`FillView`, before its next decision
* ``on_session_end()`` after the last bar of each trading day, in a backtest
  and in a session alike

Every hook runs as strategy code (:func:`strategy_code`). Import and the
factory run inside the app process when the registry is read, so both must be
cheap: a strategy loads a model or a frozen artifact in ``initialize``, not at
import.

Each instance has a seed derived from its strategy version, its configuration
and its place in the scenario (:func:`instance_seed`), so the same inputs give
the same seed in every environment. ``ctx.rng`` and ``setup.rng`` are one NumPy
generator seeded with it; a strategy that draws random numbers draws them there.

Discovery
---------

Installed packages register a strategy under the ``openalgo.strategies`` entry
point group, the standard Python plugin mechanism, so a strategy ships as an
ordinary wheel (the way algo-trading already ships Alpha A) and needs no file
copied into this repository. Built-in strategies are listed in ``BUILTINS``
instead, because this project is not itself an installed distribution and its
own entry points would never register.

Version identity
----------------

A version is identified by what the package already says about itself rather
than by a scheme invented here: the distribution name and version, a hash of the
package's Python sources, and the strategy's declared ``artifact_hash`` when it
carries a frozen model. ``model_hash`` and ``adapter_version``, when a strategy
declares them, join the identity too; a strategy that declares neither keeps
the version hash it had before they existed. Any change to any of those is a
new version; nothing about an existing version is ever rewritten. The source hash is taken over the
files on disk rather than a wheel's RECORD so an editable install, which has a
RECORD that never changes, still gets a new version when its code does.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import math
import re
import socket
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

import numpy as np

ENTRY_POINT_GROUP = "openalgo.strategies"

#: Built-in strategies, ``key -> "module:attribute"``. Kept to the one reference
#: strategy the tests and UAT need; real strategies arrive as installed packages.
BUILTINS = {
    "first_bar_leader": "research.reference:FirstBarLeader",
}

#: Bar intervals the engine accepts. They are the ones Historify can serve.
INTERVALS = ("1m", "5m", "15m", "30m", "1h", "D")

PRODUCTS = ("MIS", "CNC")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9&_.-]{1,40}$")


#: IST has no daylight saving, so a fixed offset is exact for every date.
IST_OFFSET_SECONDS = 19_800


def ist_day(epoch: int) -> int:
    """Days since 1970-01-01 in IST, the unit a trading day is counted in."""
    return (int(epoch) + IST_OFFSET_SECONDS) // 86_400


def ist_minute(epoch: int) -> int:
    """Minute of the IST day, 0-1439."""
    return ((int(epoch) + IST_OFFSET_SECONDS) % 86_400) // 60


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, so equal values hash equally."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_sources(root: Path, pattern: str = "*.py") -> str:
    """Hash every matching file under ``root`` by relative path and content.

    Sorted, so the hash does not depend on directory listing order, and keyed
    by relative path, so renaming a file is a change even when its bytes are not.
    """
    digest = hashlib.sha256()
    files = [root] if root.is_file() else sorted(root.rglob(pattern))
    for path in files:
        if "__pycache__" in path.parts:
            continue
        rel = path.name if root.is_file() else path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass(frozen=True)
class Config:
    """Deployment configuration for one strategy instance.

    Frozen, and hashed by value: two configs with equal fields are the same
    config version. Every field is something an operator decides when deploying
    a strategy; nothing here changes what the strategy believes.
    """

    capital: float
    allocation: float = 1.0
    leverage: float = 1.0
    product: str = "MIS"
    entry_time: str = "09:15"
    exit_time: str | None = "15:10"
    stop_pct: float | None = None
    target_pct: float | None = None
    universe: tuple[str, ...] = ()
    exchange: str = "NSE"
    execution_mode: str = "backtest"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "universe", tuple(sorted({s.strip().upper() for s in self.universe if s}))
        )
        object.__setattr__(self, "exchange", self.exchange.strip().upper())
        object.__setattr__(self, "product", self.product.strip().upper())
        for name in ("capital", "allocation", "leverage"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name.capitalize()} must be a positive number.")
        if self.allocation > 1:
            raise ValueError("Allocation is a share of capital, so it cannot be above 100%.")
        if self.product not in PRODUCTS:
            raise ValueError("Product must be MIS (intraday) or CNC (delivery).")
        for name in ("entry_time", "exit_time"):
            value = getattr(self, name)
            if value is not None and not _TIME_RE.match(value):
                raise ValueError(f"{name.replace('_', ' ').capitalize()} must look like 09:15.")
        if self.product == "MIS" and not self.exit_time:
            raise ValueError("An intraday (MIS) strategy needs an exit time.")
        if self.exit_time and _minutes(self.exit_time) <= _minutes(self.entry_time):
            raise ValueError("Exit time must be after entry time.")
        for name in ("stop_pct", "target_pct"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or not 0 < value < 100):
                raise ValueError(f"{name.split('_')[0].capitalize()} must be between 0% and 100%.")
        if not self.universe:
            raise ValueError("Add at least one symbol to the universe.")
        bad = [s for s in self.universe if not _SYMBOL_RE.match(s)]
        if bad:
            raise ValueError(f"These symbols are not valid: {', '.join(bad[:5])}.")
        if self.execution_mode != "backtest":
            # The field predates deployments and is kept for config hashes. Where a
            # configuration runs (backtest, Sandbox, Shadow, Live) is chosen when it
            # is run or deployed, so one configuration keeps one identity in all of
            # them (PRD section 65).
            raise ValueError(
                "The environment is chosen when you run or deploy a scenario, not in "
                "the configuration."
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Config:
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(unknown)}.")
        values = dict(data)
        values["universe"] = tuple(values.get("universe") or ())
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["universe"] = list(self.universe)
        return out

    @property
    def config_hash(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    @property
    def entry_minute(self) -> int:
        return _minutes(self.entry_time)

    @property
    def exit_minute(self) -> int | None:
        return _minutes(self.exit_time) if self.exit_time else None

    @property
    def sizing_capital(self) -> float:
        """The notional a weight of 1.0 stands for."""
        return self.capital * self.allocation * self.leverage


class Bars:
    """Read-only OHLCV arrays for one symbol, up to and including the current bar."""

    __slots__ = ("timestamp", "open", "high", "low", "close", "volume")

    def __init__(self, columns: Mapping[str, np.ndarray], end: int) -> None:
        for name in self.__slots__:
            setattr(self, name, columns[name][:end])

    def __len__(self) -> int:
        return len(self.timestamp)


class Context:
    """What a strategy may see at one decision point.

    ``time`` is the epoch second the current bar opened; ``day`` and ``minute``
    are the same instant as an IST day number and minute of that day, which is
    what a schedule-driven Indian strategy actually reasons in. ``rng`` is the
    instance's seeded generator.
    """

    __slots__ = (
        "_columns",
        "_index",
        "_positions",
        "_recorder",
        "config",
        "day",
        "equity",
        "minute",
        "rng",
        "time",
    )

    def __init__(
        self,
        columns: Mapping[str, Mapping[str, np.ndarray]],
        index: int,
        time: int,
        day: int,
        minute: int,
        positions: Mapping[str, int],
        equity: float,
        config: Config,
        rng: np.random.Generator | None = None,
        recorder: Callable[[int, str, Mapping[str, float]], None] | None = None,
    ) -> None:
        self._columns = columns
        self._index = index
        self._positions = positions
        self._recorder = recorder
        self.time = time
        self.day = day
        self.minute = minute
        self.equity = equity
        self.config = MappingProxyType(config.to_dict())
        self.rng = rng

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self.config["universe"])

    def bars(self, symbol: str) -> Bars:
        return Bars(self._columns[symbol], self._index + 1)

    def position(self, symbol: str) -> int:
        return int(self._positions.get(symbol, 0))

    def record(self, name: str, values: Mapping[str, float]) -> None:
        """Keep a per-symbol panel with the run: scores, predictions, features.

        Each call is one row per symbol at this bar, written to the run's
        ``records`` artifact with its hash, so a model's output stays auditable
        beside the trades it led to (PRD section 19). Recording changes nothing
        about the decision, and a run that records nothing has no artifact.
        """
        if self._recorder is None:
            return
        name = str(name).strip()
        if not name or len(name) > 60:
            raise ValueError("A record needs a name of up to 60 characters.")
        self._recorder(self.time, name, {str(s): float(v) for s, v in values.items()})


@dataclass(frozen=True)
class SetupContext:
    """What ``initialize`` is given: the run's shape, never its data or its venue."""

    symbols: tuple[str, ...]
    interval: str
    config: Mapping[str, Any]
    seed: int
    rng: np.random.Generator


@dataclass(frozen=True)
class FillView:
    """One fill on a strategy's own book, as ``on_fill`` sees it.

    ``qty`` is signed: positive bought, negative sold. ``reason`` is ``signal``
    for the strategy's own decision, otherwise the platform's (``stop``,
    ``target``, ``exit_time``, ``eod``, ``end``, ``square_off``).
    """

    symbol: str
    qty: int
    price: float
    fees: float
    slippage: float
    reason: str
    time: int


def instance_seed(version_hash: str, config_hash: str, position: int) -> int:
    """A strategy instance's seed: the same inputs give it in every environment."""
    digest = hashlib.sha256(f"{version_hash}:{config_hash}:{position}".encode()).hexdigest()
    return int(digest[:15], 16)


@runtime_checkable
class Strategy(Protocol):
    interval: str

    def decide(self, ctx: Context) -> Mapping[str, float] | None: ...


@dataclass(frozen=True)
class StrategySpec:
    """Where a strategy comes from, before anything is loaded."""

    key: str
    source: str  # "builtin" | "entry_point"
    target: str  # "module:attribute"
    dist_name: str
    dist_version: str


def discover() -> dict[str, StrategySpec]:
    """Every strategy available to this process, built-ins first.

    An installed package cannot shadow a built-in: a key collision keeps the
    built-in, because silently swapping the reference strategy for a package's
    would change what every existing test and UAT run means.
    """
    from utils.version import get_version

    specs = {
        key: StrategySpec(key, "builtin", target, "openalgo", get_version())
        for key, target in BUILTINS.items()
    }
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if ep.name in specs:
            continue
        dist = ep.dist
        specs[ep.name] = StrategySpec(
            key=ep.name,
            source="entry_point",
            target=ep.value,
            dist_name=dist.name if dist else "",
            dist_version=dist.version if dist else "",
        )
    return specs


def load_factory(spec: StrategySpec):
    module_name, _, attr = spec.target.partition(":")
    obj = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


#: What strategy code may not do inside a research process, by the audit event
#: that announces it (why: ``research.worker``, "The broker boundary"). A thread
#: is on the list because work handed to one would run outside the scope below.
REFUSED_TO_STRATEGIES = {
    "socket.connect": "open a network connection",
    "socket.getaddrinfo": "open a network connection",
    "socket.sendto": "send over the network",
    "socket.sendmsg": "send over the network",
    "sqlite3.connect": "open a database",
    "subprocess.Popen": "start a process",
    "os.system": "start a process",
    "os.exec": "start a process",
    "os.posix_spawn": "start a process",
    "os.spawn": "start a process",
    "os.fork": "start a process",
    "os.forkpty": "start a process",
    "_thread.start_new_thread": "start a thread",
    "_thread.start_joinable_thread": "start a thread",
}

_strategy = threading.local()


class StrategyRefused(RuntimeError):
    """Strategy code tried something a research process does not allow.

    Not an ``OSError``, so an HTTP library does not mistake it for a dropped
    connection and retry.
    """

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(
            f"A strategy tried to {action}. A strategy gets its market data from the "
            "platform and only the platform places its orders, so this is refused."
        )


@contextmanager
def strategy_code():
    """Run the block as strategy code: importing a plugin, building it, ``decide``.

    It marks only the current thread, and refuses nothing until
    ``research.worker.install_guard`` has installed the audit hook, so in the
    app process it changes nothing. A refusal is raised when the outermost block
    ends, even if the strategy or a library it called caught the original
    exception.
    """
    outer = getattr(_strategy, "depth", 0)
    if not outer:
        _strategy.refused = None
    _strategy.depth = outer + 1
    try:
        yield
    finally:
        _strategy.depth = outer
        if not outer and _strategy.refused:
            raise StrategyRefused(_strategy.refused)


def refuse_to_strategies(event: str, args) -> None:
    """The audit hook: refuse a ``REFUSED_TO_STRATEGIES`` event inside ``strategy_code``."""
    if event in REFUSED_TO_STRATEGIES and getattr(_strategy, "depth", 0):
        _strategy.refused = REFUSED_TO_STRATEGIES[event]
        raise StrategyRefused(_strategy.refused)


#: The network, as a backtest worker must never reach it: no connection and no
#: datagram from anything in the process, and no name lookup either, since a
#: lookup alone carries data out. A Unix socket is a local file and is allowed.
NETWORK_EVENTS = frozenset(
    {"socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo"}
)


class NetworkRefused(PermissionError):
    """A backtest worker tried to reach the network."""


def refuse_network(event: str, args) -> None:
    """The backtest worker's second audit hook: the network is refused for its whole life.

    A backtest reads its snapshot and writes its results to files; it never
    needs the network. ``refuse_to_strategies`` runs first and names a strategy
    that tries; this one also stops a library or the platform's own code.
    """
    if event not in NETWORK_EVENTS:
        return
    sock = args[0] if args and event != "socket.getaddrinfo" else None
    if sock is not None and getattr(sock, "family", None) == getattr(socket, "AF_UNIX", -1):
        return
    raise NetworkRefused("A backtest never uses the network, so this process refuses it.")


def build(spec: StrategySpec) -> Strategy:
    """A fresh strategy object. One per instance per run, so no state leaks."""
    with strategy_code():
        strategy = load_factory(spec)()
        if not isinstance(strategy, Strategy):
            raise TypeError(f"{spec.key} does not provide interval and decide().")
    if strategy.interval not in INTERVALS:
        raise ValueError(f"{spec.key} asks for an unsupported bar interval {strategy.interval!r}.")
    return strategy


def _code_hash(spec: StrategySpec) -> str:
    module_name = spec.target.partition(":")[0]
    module = importlib.import_module(module_name)
    module_file = Path(module.__file__).resolve()
    if spec.source == "builtin":
        return hash_sources(module_file)
    root_name = module_name.split(".")[0]
    root = importlib.import_module(root_name)
    root_path = Path(root.__file__).resolve()
    return hash_sources(root_path.parent if root_path.name == "__init__.py" else root_path)


def version_identity(spec: StrategySpec) -> dict[str, Any]:
    """The fields that make a strategy version, plus its hash.

    Building the strategy is part of this on purpose: a strategy with a frozen
    artifact declares the artifact's hash on the object it builds, and that is
    the only place it can be read without knowing the strategy's internals.
    """
    strategy = build(spec)
    identity = {
        "key": spec.key,
        "source": spec.source,
        "dist_name": spec.dist_name,
        "dist_version": spec.dist_version,
        "code_hash": _code_hash(spec),
        "artifact_hash": getattr(strategy, "artifact_hash", None),
        "interval": strategy.interval,
    }
    # Only when declared, so a strategy that declares neither keeps the version
    # hash it had before these fields existed.
    for name in ("model_hash", "adapter_version"):
        value = getattr(strategy, name, None)
        if value is not None:
            identity[name] = str(value)
    identity["version_hash"] = sha256_hex(canonical_json(identity))
    identity.setdefault("model_hash", None)
    identity.setdefault("adapter_version", None)
    metadata = getattr(strategy, "metadata", None)
    with strategy_code():
        metadata = metadata() if callable(metadata) else metadata
    identity["meta"] = dict(metadata or {})
    return identity

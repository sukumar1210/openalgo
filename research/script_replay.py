"""
Backtest a Python strategy hosted at /python by replaying history to the unchanged file.

The backtest worker calls ``replay`` instead of running the engine when a
scenario's one strategy is a followed script. For every trading day in the
snapshot it starts ``research.script_session`` as the strategy host would start
the script: a fresh process at the configuration's start time, ended at its
stop time. The processes share one state file, so cash, positions and fills
carry from day to day as they would in the sandbox. When the days are done,
the fills go through ``research.fills`` and become an ordinary ``Result``,
saved and shown like any other run.

A day that does not end cleanly does not end the backtest, because the host
would start the script again the next morning too. It is listed on the run
with what happened: the script raised or exited, or it was still going after
``DAY_TIMEOUT_SECONDS`` of real time (it waits on something other than
``time.sleep``, or it runs out of CPU or memory). A day whose process was
ended from outside keeps nothing from that day, since the state is written
only when a day closes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from research.dataset import Frames
from research.script_sim import MARKET_CLOSE, at_time, new_state
from research.strategy import IST_OFFSET_SECONDS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: Times a replay starts over because the script asked for symbols it lacked.
DISCOVERY_ROUNDS = 3


@dataclass(frozen=True)
class ScriptSettings:
    """Everything a replay needs, and all of it the platform's own settings.

    The schedule, weekdays and exchange are the script's own in Python
    Strategies; the capital and the intraday leverage are the sandbox's, the
    account the script trades in analyzer mode. A script has no settings of
    its own beyond those.
    """

    capital: float
    leverage: float
    entry_time: str
    exit_time: str | None
    exchange: str
    days: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")

    @property
    def sizing_capital(self) -> float:
        return self.capital * self.leverage

    @classmethod
    def from_dict(cls, data: dict) -> ScriptSettings:
        return cls(
            capital=float(data["capital"]),
            leverage=float(data.get("leverage") or 1.0),
            entry_time=str(data["entry_time"]),
            exit_time=data.get("exit_time"),
            exchange=str(data.get("exchange") or "NSE"),
            days=tuple(data.get("days") or ("mon", "tue", "wed", "thu", "fri")),
        )

    def to_dict(self) -> dict:
        return {
            "capital": self.capital,
            "leverage": self.leverage,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "exchange": self.exchange,
            "days": list(self.days),
        }


#: Real time one replayed day may take. A script paced by time.sleep replays a
#: day in seconds, so this is reached only by one that waits on something else.
DAY_TIMEOUT_SECONDS = 300


def trading_days(frames: Frames) -> list[int]:
    """Every IST day the snapshot has a bar on, in order."""
    days: set[int] = set()
    for frame in frames.values():
        days.update(((frame["timestamp"] + IST_OFFSET_SECONDS) // 86_400).unique().tolist())
    return sorted(int(d) for d in days)


def day_env(folder: Path, script_id: str, name: str, exchange: str) -> dict:
    """What a replayed day runs with: the worker's own environment, already free of
    credentials, plus what the strategy host sets, with every cache in its folder."""
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                p for p in (str(PROJECT_ROOT), env.get("PYTHONPATH", "")) if p
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "TMPDIR": str(folder / "tmp"),
            "NUMBA_CACHE_DIR": str(folder / "cache" / "numba"),
            "MPLCONFIGDIR": str(folder / "cache" / "matplotlib"),
            "XDG_CACHE_HOME": str(folder / "cache"),
            "STRATEGY_ID": script_id,
            "STRATEGY_NAME": name,
            "OPENALGO_STRATEGY_EXCHANGE": exchange,
        }
    )
    return env


def replay(
    script: Path,
    snapshot: Path,
    frames: Frames,
    config: ScriptSettings,
    folder: Path,
    script_id: str,
    name: str,
    on_progress=None,
    trade_from: int | None = None,
    weekdays: list[str] | None = None,
    end_day: int | None = None,
) -> tuple[list, dict]:
    """Run the script over every trading day of the snapshot.

    Args:
        script: The file of the version being tested (a kept copy).
        snapshot: The scenario's frozen 1m bars, which the script reads.
        frames: The same bars, already loaded here, for the list of days.
        config: Start and stop times, capital and buying power.
        folder: An empty folder for this run; each day works inside it.
        script_id: The script's id in the strategy host, as STRATEGY_ID.
        name: Its name there, as STRATEGY_NAME.
        on_progress: Called with the fraction of days done; may raise to stop.
        trade_from: The first day the script is run, as epoch seconds; the
            days before it are history it reads, never runs.
        weekdays: The days the strategy host runs it ("mon".."sun"); the
            others are skipped, as the host skips them.
        end_day: The last day to run (an IST day number). When the snapshot
            holds no bars yet, the scheduled weekdays from ``trade_from`` to it
            are run instead, so the script can say which symbols it reads.

    Returns:
        ``(fills, report)``: every fill as ``research.fills.Fill``, and what
        the run should say about how the days went.
    """
    from research.fills import Fill

    folder.mkdir(parents=True, exist_ok=True)
    for sub in ("tmp", "cache"):
        (folder / sub).mkdir(exist_ok=True)
    state_path = folder / "state.json"
    state_path.write_text(json.dumps(new_state(config.capital)), encoding="utf-8")
    settings = json.dumps({"buying_power": config.sizing_capital, "exchange": config.exchange})
    days = trading_days(frames)
    if not days and trade_from is not None and end_day is not None:
        first = (trade_from + IST_OFFSET_SECONDS) // 86_400
        days = list(range(first, end_day + 1))
    if trade_from is not None:
        days = [day for day in days if day * 86_400 - IST_OFFSET_SECONDS >= trade_from]
    if weekdays:
        allowed = {d.lower()[:3] for d in weekdays}
        names = ("thu", "fri", "sat", "sun", "mon", "tue", "wed")  # 1970-01-01 was a Thursday
        days = [day for day in days if names[day % 7] in allowed]
    stop_time = config.exit_time or MARKET_CLOSE.get(config.exchange, "15:30")
    env = day_env(folder, script_id, name, config.exchange)
    killed: list[dict] = []
    for n, day in enumerate(days):
        start, stop = at_time(day, config.entry_time), at_time(day, stop_time)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "research.script_session",
                str(snapshot),
                str(state_path),
                str(script),
                str(start),
                str(stop),
                settings,
            ],
            cwd=str(folder),
            env=env,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            code = process.wait(timeout=DAY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if code not in (0, 3):
            killed.append(
                {
                    "day": _iso(day),
                    "at": None,
                    "message": f"The script was still running after {DAY_TIMEOUT_SECONDS} "
                    "seconds of real time and was stopped; that day's trades are not counted. "
                    "Pace the script with time.sleep."
                    if code is None
                    else "The script's process ended unexpectedly, possibly out of memory or "
                    "CPU time; that day's trades are not counted.",
                }
            )
        if on_progress:
            on_progress((n + 1) / len(days))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    fills = [
        Fill(int(ts), symbol, int(qty), float(price), reason)
        for ts, symbol, qty, price, reason in state["fills"]
    ]
    stopped = sorted(state["stopped_early"] + killed, key=lambda item: item["day"])
    report = {
        "days": len(days),
        "stopped_early": stopped,
        "unsupported": state["unsupported"],
        # Symbols the script asked for that the snapshot did not hold.
        "asked_for": state.get("asked_for", []),
        # The Landlock ABI the days ran behind; 0 means the audit hook alone.
        "os_wall": state.get("os_wall", 0),
    }
    return fills, report


def _iso(day: int) -> str:
    from datetime import date, timedelta

    return (date(1970, 1, 1) + timedelta(days=day)).isoformat()


def discover_and_replay(
    script: Path,
    settings: ScriptSettings,
    frames: Frames,
    load,
    folder: Path,
    script_id: str,
    name: str,
    trade_from: int,
    end_day: int,
    on_progress=None,
) -> tuple[Frames, list, dict, list[str]]:
    """Replay the script, learning which symbols it reads by watching what it asks for.

    A script decides its own symbols, so no setting lists them. The replay
    starts from whatever bars are known, notes each symbol the script asks
    for and does not get, has ``load`` fetch those, and starts over from the
    first day, so every day sees every symbol. ``load(symbols)`` returns
    ``(frames, missing)``: the bars it found, and the symbols it has none for.

    Returns:
        ``(frames, fills, report, missing)``: the bars served, the fills, how
        the days went, and the symbols the script asked for that no stored
        data holds.
    """
    from research.dataset import write_snapshot

    frames = dict(frames)
    missing: list[str] = []
    for round_no in range(DISCOVERY_ROUNDS):
        snapshot = folder / "snapshot.parquet"
        if frames:
            write_snapshot(frames, snapshot)
        else:
            import pandas as pd

            from research.dataset import COLUMNS

            snapshot.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(columns=["symbol", *COLUMNS]).to_parquet(snapshot, index=False)
        fills, report = replay(
            script,
            snapshot,
            frames,
            settings,
            folder / f"round{round_no}",
            script_id,
            name,
            on_progress=on_progress,
            trade_from=trade_from,
            weekdays=list(settings.days),
            end_day=end_day,
        )
        asked = [s for s in report.get("asked_for", []) if s not in frames and s not in missing]
        if not asked:
            break
        found, absent = load(asked)
        frames.update(found)
        missing += [s for s in absent if s not in missing]
        if not found:
            break
    return frames, fills, report, missing

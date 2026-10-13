"""
Backtesting a Python strategy by replaying history to the unchanged script.

Three things have to hold for the result to mean anything. The script must see
only what had happened by its virtual time (no bar it has not reached, a price
that is the market's at that moment). Its orders must fill as the sandbox fills
them, so a backtest and a hosted day compare. And it must be walled in: the
script is a trader's own code running unchanged, so a backtest that could reach
the real OpenAlgo, a broker, or the live installation's files would be a way to
trade or to damage data by running a backtest.
"""

import json
import textwrap
from pathlib import Path

import pandas as pd
import pytest
from test_research_runs import wait_final

from research.dataset import normalise, write_snapshot
from research.script_replay import ScriptSettings
from research.script_sim import Market, Refused, Simulator, at_time, new_state
from research.strategy import IST_OFFSET_SECONDS

DAY = 20370  # 2025-10-09, a Thursday


@pytest.fixture(autouse=True)
def ist(monkeypatch):
    """Replayed days read local time as the host does; the official image runs in IST."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")


def minute_frames(days=(DAY, DAY + 1), symbols=(("SBIN", 800.0), ("INFY", 1500.0))):
    """1m bars from 09:15: each minute opens 0.1 higher, high/low 0.5 either side."""
    frames = {}
    for symbol, base in symbols:
        rows = []
        for day in days:
            start = at_time(day, "09:15")
            for m in range(375):
                price = base + m * 0.1
                rows.append((start + m * 60, price, price + 0.5, price - 0.5, price + 0.05, 100.0))
        frames[symbol] = normalise(
            pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        )
    return frames


def sim(capital=500000.0, power=500000.0):
    simulator = Simulator(Market(minute_frames()), new_state(capital), power, "NSE")
    simulator.start_day(at_time(DAY, "09:00"))
    return simulator


# --------------------------------------------------------------------------- #
# What the script sees, and how its orders fill
# --------------------------------------------------------------------------- #


def test_a_script_sees_only_what_had_happened_by_its_time():
    market = Market(minute_frames())
    at = at_time(DAY, "09:20") + 30  # half way through the 09:20 bar

    assert market.ltp("SBIN", at) == pytest.approx(800.5)  # that bar's open, not its close
    bars = market.history("SBIN", "1m", "2025-10-09", "2025-10-09", at)
    assert bars[-1]["timestamp"] == at_time(DAY, "09:20")
    assert bars[-1]["close"] == bars[-1]["open"] == pytest.approx(800.5)  # forming, flat
    assert bars[-2]["close"] == pytest.approx(800.45)  # the 09:19 bar, closed
    five = market.history("SBIN", "5m", "2025-10-09", "2025-10-09", at)
    assert [b["timestamp"] for b in five] == [at_time(DAY, "09:15"), at_time(DAY, "09:20")]
    assert five[0]["high"] == pytest.approx(800.4 + 0.5)


def test_market_orders_fill_at_once_and_resting_orders_on_a_later_bar_that_reaches_them():
    s = sim()
    t = at_time(DAY, "09:30")
    market = s.place({"symbol": "SBIN", "action": "BUY", "quantity": 10, "pricetype": "MARKET"}, t)
    assert (market["order_status"], market["average_price"]) == ("complete", pytest.approx(801.5))
    # A sell limit 2.9 up: the 09:54 bar (open 803.9, high 804.4) is the first to reach it,
    # and it is known once that bar has closed, at 09:55.
    limit = s.place(
        {"symbol": "SBIN", "action": "SELL", "quantity": 10, "pricetype": "LIMIT", "price": 804.4},
        t,
    )
    assert limit["order_status"] == "open"
    s.advance(at_time(DAY, "09:54"))
    assert limit["order_status"] == "open"
    s.advance(at_time(DAY, "09:55"))
    assert (limit["order_status"], limit["average_price"]) == ("complete", pytest.approx(804.4))
    assert [f[2] for f in s.state["fills"]] == [10, -10]


def test_intraday_positions_square_off_at_the_sandboxs_time_and_later_entries_are_refused():
    s = sim()
    s.place(
        {"symbol": "SBIN", "action": "BUY", "quantity": 5, "pricetype": "MARKET"},
        at_time(DAY, "10:00"),
    )
    s.advance(at_time(DAY, "15:20"))

    last = s.state["fills"][-1]
    assert (last[0], last[2], last[4]) == (at_time(DAY, "15:15"), -5, "square_off")
    with pytest.raises(Refused, match="after the intraday square-off"):
        s.place({"symbol": "SBIN", "action": "BUY", "quantity": 1}, at_time(DAY, "15:21"))


def test_a_smart_order_moves_the_position_to_its_size_as_the_sandbox_does():
    s = sim()
    t = at_time(DAY, "10:00")
    smart = {"symbol": "SBIN", "action": "BUY", "pricetype": "MARKET", "product": "MIS"}
    assert s.api_placesmartorder({**smart, "quantity": 10, "position_size": 10}, t)["orderid"]
    matched = s.api_placesmartorder({**smart, "quantity": 10, "position_size": 10}, t)
    assert "Already Matched" in matched["message"]
    s.api_placesmartorder({**smart, "action": "SELL", "quantity": 10, "position_size": -5}, t)
    assert s.api_openposition({"symbol": "SBIN", "product": "MIS"}, t)["quantity"] == -5


def test_an_order_past_the_buying_power_is_refused_but_an_exit_never_is():
    s = sim(power=10000.0)
    t = at_time(DAY, "10:00")
    s.place({"symbol": "SBIN", "action": "BUY", "quantity": 12}, t)
    with pytest.raises(Refused, match="Insufficient funds"):
        s.place({"symbol": "SBIN", "action": "BUY", "quantity": 1}, t)
    s.place({"symbol": "SBIN", "action": "SELL", "quantity": 12}, t)


def test_an_endpoint_a_backtest_cannot_serve_is_refused_by_name_and_counted():
    s = sim()
    status, body = s.handle("optionchain", {}, at_time(DAY, "10:00"))
    assert status == 404 and "optionchain is not available in a backtest" in body["message"]
    assert s.state["unsupported"] == {"optionchain": 1}


# --------------------------------------------------------------------------- #
# Replaying a script, a process a day
# --------------------------------------------------------------------------- #


SDK_SCRIPT = """
import os, time
from datetime import datetime
from openalgo import api

client = api(api_key=os.getenv("OPENALGO_API_KEY"), host=os.getenv("HOST_SERVER"))
bought = sold = False
print("started", datetime.now().isoformat(), flush=True)
while True:
    now = datetime.now()
    if not bought and (now.hour, now.minute) >= (9, 20):
        r = client.placesmartorder(strategy="t", symbol="SBIN", action="BUY", exchange="NSE",
                                   price_type="MARKET", product="MIS", quantity=10, position_size=10)
        bought = r["status"] == "success"
    if bought and not sold and (now.hour, now.minute) >= (11, 0):
        client.placesmartorder(strategy="t", symbol="SBIN", action="SELL", exchange="NSE",
                               price_type="MARKET", product="MIS", quantity=10, position_size=0)
        sold = True
    time.sleep(30)
"""


def run_replay(tmp_path, source, frames=None, **kw):
    from research.script_replay import replay

    frames = frames or minute_frames()
    script = tmp_path / "script.py"
    script.write_text(textwrap.dedent(source))
    snapshot = tmp_path / "snap.parquet"
    write_snapshot(frames, snapshot)
    settings = ScriptSettings(
        capital=500000, leverage=1.0, entry_time="09:00", exit_time="15:30", exchange="NSE", **kw
    )
    return replay(script, snapshot, frames, settings, tmp_path / "run", "s1", "Script")


def test_an_unchanged_sdk_script_trades_each_day_as_the_host_would_run_it(tmp_path):
    fills, report = run_replay(tmp_path, SDK_SCRIPT)

    assert (report["days"], report["stopped_early"], report["unsupported"]) == (2, [], {})
    assert [(f.ts, f.qty, f.price) for f in fills] == [
        (at_time(DAY, "09:20"), 10, pytest.approx(800.5)),
        (at_time(DAY, "11:00"), -10, pytest.approx(810.5)),
        (at_time(DAY + 1, "09:20"), 10, pytest.approx(800.5)),
        (at_time(DAY + 1, "11:00"), -10, pytest.approx(810.5)),
    ]


WALLED = """
import json, os, socket, subprocess, time
tried = {}
def attempt(name, fn):
    try:
        fn()
        tried[name] = "allowed"
    except Exception as exc:
        tried[name] = type(exc).__name__
attempt("real_openalgo", lambda: socket.create_connection(("127.0.0.1", 5000), timeout=1))
attempt("internet", lambda: socket.getaddrinfo("example.com", 443))
attempt("process", lambda: subprocess.run(["true"]))
attempt("write_inside", lambda: open("notes.txt", "w").write("ok"))
# The script's own files outside its folder: written, read back, listed, deleted.
audit, other = os.environ["AUDIT"], os.environ["OTHER"]
attempt("append_outside", lambda: open(audit, "a").write("+replay"))
tried["read_back"] = open(audit).read()
tried["listed"] = sorted(os.listdir(os.path.dirname(audit)))
attempt("delete_outside", lambda: os.remove(other))
tried["other_exists"] = os.path.exists(other)
tried["listed_after"] = sorted(os.listdir(os.path.dirname(audit)))
def compiled():
    import pyarrow as pa, pyarrow.parquet as pq
    pq.write_table(pa.table({"a": [1]}), os.environ["COMPILED"])
attempt("compiled_write_outside", compiled)
json.dump(tried, open("tried.json", "w"))
time.sleep(10 ** 6)
"""


def test_a_replayed_script_writes_its_own_copy_and_cannot_reach_out(tmp_path, monkeypatch):
    from research import landlock

    live = tmp_path / "live_data"
    live.mkdir()
    (live / "audit.jsonl").write_text("live")
    (live / "other.txt").write_text("keep me")
    monkeypatch.setenv("AUDIT", str(live / "audit.jsonl"))
    monkeypatch.setenv("OTHER", str(live / "other.txt"))
    monkeypatch.setenv("COMPILED", str(live / "made_by_pyarrow.parquet"))
    fills, report = run_replay(tmp_path, WALLED, frames=minute_frames(days=(DAY,)))

    tried = json.loads((tmp_path / "run" / "tried.json").read_text())
    assert {k: tried[k] for k in ("real_openalgo", "internet", "process", "write_inside")} == {
        "real_openalgo": "Refused",
        "internet": "Refused",
        "process": "Refused",
        "write_inside": "allowed",
    }
    # The script saw its writes as if they were in place...
    assert tried["append_outside"] == tried["delete_outside"] == "allowed"
    assert tried["read_back"] == "live+replay"
    assert tried["listed"] == ["audit.jsonl", "other.txt"]
    assert tried["listed_after"] == ["audit.jsonl"]
    assert tried["other_exists"] is False
    # ...and the live files are exactly as they were.
    assert (live / "audit.jsonl").read_text() == "live"
    assert (live / "other.txt").read_text() == "keep me"
    copy = tmp_path / "run" / "outside" / str(live / "audit.jsonl").lstrip("/")
    assert copy.read_text() == "live+replay"
    if landlock.abi() >= 1:
        # Compiled code bypasses Python's file functions; the kernel refuses it.
        assert tried["compiled_write_outside"] != "allowed"
        assert report["os_wall"] >= 1
    assert not (live / "made_by_pyarrow.parquet").exists()
    assert report["stopped_early"] == []


FAILS_ON_THE_FIRST_DAY = """
import time
from datetime import date
if date.today().isoformat() == "2025-10-09":
    time.sleep(3600)
    raise RuntimeError("no model file")
while True:
    time.sleep(60)
"""


def test_a_day_the_script_fails_is_reported_and_the_next_day_still_runs(tmp_path):
    fills, report = run_replay(tmp_path, FAILS_ON_THE_FIRST_DAY)

    assert report["days"] == 2
    assert report["stopped_early"] == [
        {
            "day": "2025-10-09",
            "at": "10:00",
            "message": "The script stopped with an error: no model file",
        }
    ]


def test_a_day_that_waits_on_the_real_clock_is_stopped_and_said_so(tmp_path, monkeypatch):
    from research import script_replay

    monkeypatch.setattr(script_replay, "DAY_TIMEOUT_SECONDS", 2)
    fills, report = run_replay(
        tmp_path, "import threading\nthreading.Event().wait()\n", frames=minute_frames(days=(DAY,))
    )
    [stopped] = report["stopped_early"]
    assert "Pace the script with time.sleep" in stopped["message"]


# --------------------------------------------------------------------------- #
# End to end: a followed script backtested from the registry
# --------------------------------------------------------------------------- #


#: A day inside a backtest's 45-day warm-up, so stored bars cover the window.
WARM_UP_DAY = DAY - 44  # Tuesday 2025-08-26


@pytest.fixture
def historify(tmp_path, monkeypatch):
    """Historify on its own DuckDB file; ``store(days)`` puts SBIN's and INFY's bars in it."""
    import importlib

    from database import historify_db

    monkeypatch.setenv("HISTORIFY_DATABASE_PATH", str(tmp_path / "historify.duckdb"))
    module = importlib.reload(historify_db)
    module.init_database()

    def store(days):
        for symbol, frame in minute_frames(days=days).items():
            module.upsert_market_data(frame.copy(), symbol, "NSE", "1m")

    yield store
    monkeypatch.undo()
    importlib.reload(historify_db)


@pytest.fixture
def backtest_sdk(research_db, tmp_path, monkeypatch):
    """Runs SDK_SCRIPT as a hosted script backtest over DAY and DAY + 1: ``(final run, downloads asked for)``."""
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "log"))
    from services import research_hosted_service as hosted
    from services import research_service as svc

    scripts = tmp_path / "strategies" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "sdk_1.py").write_text(textwrap.dedent(SDK_SCRIPT))
    monkeypatch.setattr(hosted, "SCRIPT_DIR", scripts)
    monkeypatch.setattr(hosted, "LOG_DIR", tmp_path / "log" / "strategies")
    host = {"sdk_1": {"name": "SDK", "exchange": "NSE", "schedule_start": "09:00",
                      "schedule_stop": "15:30", "schedule_days": ["mon", "tue", "wed", "thu", "fri"]}}  # fmt: skip
    monkeypatch.setattr(hosted, "_host_configs", lambda: host)
    monkeypatch.setattr(hosted, "sandbox_money", lambda: (500000.0, 1.0))
    # A download is what the app does next; here it only records what was asked.
    downloads = []

    def download(run, symbols, spec, user_id, what):
        downloads.append(sorted(symbols))
        return False

    monkeypatch.setattr(svc, "_download", download)

    def run_it():
        run = hosted.backtest("trader", "sdk_1", "2025-10-09", "2025-10-10")
        # Editing the script afterwards changes nothing about this backtest's version.
        (scripts / "sdk_1.py").write_text("raise SystemExit('edited')\n")
        row = wait_final(research_db, [run["id"]], timeout=55)[run["id"]]
        assert row.status == "COMPLETED", row.message
        return row, downloads

    try:
        yield run_it
    finally:
        svc.terminate_all()
        with svc._lock:
            for child in svc._children.values():
                child["process"].wait(timeout=10)
            svc._children.clear()
        svc._shutting_down = False


def test_a_python_strategy_backtests_unchanged_learning_its_symbols_by_running(
    research_db, historify, backtest_sdk
):
    """No setting names its symbols: the worker learns SBIN from what the script
    asks for, loads it, and replays from the first day again."""
    historify((WARM_UP_DAY, DAY, DAY + 1))

    row, downloads = backtest_sdk()

    trades = research_db.LedgerTrade.query.filter_by(run_id=row.id).all()
    assert [(t.side, t.qty, t.entry_price, t.exit_price) for t in trades] == [
        ("LONG", 10, pytest.approx(800.5), pytest.approx(810.5))
    ] * 2
    replay = row.provenance["replay"]
    assert replay["days"] == 2 and replay["missing_symbols"] == []
    assert replay["settings"]["capital"] == 500000.0
    dataset = research_db.db_session.get(research_db.ResearchDataset, row.provenance["dataset_id"])
    assert dataset.spec["symbols"] == ["SBIN"]  # learned, never configured
    assert downloads == []  # the stored bars already span the window


def test_a_symbol_stored_for_part_of_the_window_is_downloaded_before_the_backtest_trusts_it(
    research_db, historify, backtest_sdk
):
    """SBIN has bars for the traded days but not the warm-up: the first attempt asks
    for the rest, and after the download the backtest runs on what there is."""
    historify((DAY, DAY + 1))

    row, downloads = backtest_sdk()

    assert downloads == [["SBIN"]]  # then it ran again, once
    assert row.provenance["replay"]["missing_symbols"] == []  # served, if not in full
    assert research_db.LedgerTrade.query.filter_by(run_id=row.id).count() == 2


def test_the_virtual_clock_is_what_the_script_reads(tmp_path):
    source = """
    import json, time
    from datetime import date, datetime
    json.dump({"now": datetime.now().strftime("%H:%M"), "today": date.today().isoformat(),
               "time": int(time.time()), "strftime": time.strftime("%H:%M"),
               "is_datetime": isinstance(datetime(2025, 1, 1), datetime)}, open("clock.json", "w"))
    """
    run_replay(tmp_path, source, frames=minute_frames(days=(DAY,)))
    seen = json.loads((Path(tmp_path) / "run" / "clock.json").read_text())
    assert seen == {
        "now": "09:00",
        "today": "2025-10-09",
        "time": at_time(DAY, "09:00"),
        "strftime": "09:00",
        "is_datetime": True,
    }


def test_offset_is_ist():
    assert at_time(DAY, "09:15") == DAY * 86400 - IST_OFFSET_SECONDS + 9 * 3600 + 15 * 60


def test_a_replayed_script_reads_an_analyzer_switch_that_is_on(tmp_path):
    """algo-trading's interlock reads OpenAlgo's settings row before every order."""
    import sqlite3
    from contextlib import closing

    from research.script_replay import day_env

    env = day_env(tmp_path, "alpha_1", "Alpha", "NSE")

    db = Path(env["OPENALGO_DB"])
    assert db.is_relative_to(tmp_path) and env["OPENALGO_STRATEGY_MODE"] == "sandbox"
    with closing(
        sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ) as con:  # as the interlock opens it
        assert con.execute("SELECT analyze_mode FROM settings LIMIT 1").fetchone() == (1,)


def discover(tmp_path, frames, load, **kw):
    """Discovery over four trading days, counting the days actually run."""
    from research.script_replay import discover_and_replay

    script = tmp_path / "script.py"
    script.write_text(textwrap.dedent(SDK_SCRIPT))
    settings = ScriptSettings(
        capital=500000, leverage=1.0, entry_time="09:00", exit_time="15:30", exchange="NSE"
    )
    ran = []
    result = discover_and_replay(
        script, settings, frames, load, tmp_path / "run", "s1", "Script",
        trade_from=at_time(DAY, "00:00"), end_day=DAY + 5, on_progress=ran.append, **kw,
    )  # fmt: skip
    return result, ran


FOUR_DAYS = (DAY, DAY + 1, DAY + 4, DAY + 5)  # Thursday, Friday, Monday, Tuesday


def test_a_discovery_pass_stops_at_the_first_day_missing_bars(tmp_path):
    """Every later day would run without SBIN too: the pass is cut, not the result."""
    every = minute_frames(days=FOUR_DAYS)
    infy = {"INFY": every["INFY"]}  # the script trades SBIN, which is not loaded yet

    (frames, fills, report, missing), ran = discover(
        tmp_path, infy, lambda asked: ({s: every[s] for s in asked}, [])
    )

    assert len(ran) == 1 + 4  # one day of the first pass, then every day
    assert sorted(frames) == ["INFY", "SBIN"] and missing == []
    assert report["days"] == 4 and len(fills) == 8  # the returned pass ran every day


def test_a_symbol_still_to_download_ends_discovery_at_once(tmp_path):
    """The run goes back for a download: replaying a year without it first is wasted."""
    every = minute_frames(days=FOUR_DAYS)

    (_frames, _fills, _report, missing), ran = discover(
        tmp_path, {"INFY": every["INFY"]}, lambda asked: ({}, list(asked)), stop_on_missing=True
    )

    assert missing == ["SBIN"] and len(ran) == 1


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="reads Linux /proc")
def test_a_replayed_day_leaves_the_workers_address_space_to_the_script(tmp_path):
    """The worker caps address space at 2 GB. The replay's own allocators used to
    reserve almost all of it (pyarrow 1 GB up front, glibc 64 MB per thread), so
    Alpha A failed with MemoryError while using a few hundred MB."""
    import os
    import threading
    import time

    peak = [0]
    done = threading.Event()

    def watch():
        while not done.is_set():
            for pid in os.listdir("/proc"):
                try:
                    if (
                        pid.isdigit()
                        and b"research.script_session" in Path(f"/proc/{pid}/cmdline").read_bytes()
                    ):
                        status = Path(f"/proc/{pid}/status").read_text()
                        kb = int(status.split("VmPeak:")[1].split()[0])
                        peak[0] = max(peak[0], kb)
                except (OSError, IndexError, ValueError):
                    pass
            time.sleep(0.02)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        fills, report = run_replay(tmp_path, SDK_SCRIPT)
    finally:
        done.set()
        watcher.join(timeout=5)

    assert len(fills) == 4 and report["stopped_early"] == []
    assert 0 < peak[0] < 1200 * 1024  # about 550 MB; 2 GB without the two settings


def test_a_replayed_day_holds_only_its_own_bars_and_the_warm_ups_history(tmp_path):
    """Memory stays flat however long the window: a day is never handed bars it
    cannot serve (after it), nor more history than the first day had."""
    from research.dataset import read_snapshot

    frames = minute_frames(days=FOUR_DAYS)
    snapshot = tmp_path / "snap.parquet"
    write_snapshot(frames, snapshot)

    monday = read_snapshot(snapshot, at_time(DAY + 4 - 3, "00:00"), at_time(DAY + 4, "15:30"))

    days = sorted({int(ts + IST_OFFSET_SECONDS) // 86_400 for ts in monday["SBIN"]["timestamp"]})
    assert days == [DAY + 1, DAY + 4]  # Friday's history and Monday, not Thursday or Tuesday
    assert read_snapshot(snapshot)["SBIN"].shape == frames["SBIN"].shape  # unbounded as before


def test_a_replayed_strategy_is_handed_its_symbol_list_as_the_host_hands_it(tmp_path):
    from research.script_replay import day_env

    settings = ScriptSettings.from_dict(
        {"capital": 1, "entry_time": "09:15", "exit_time": None, "symbols": ["SBIN", "INFY"]}
    )

    assert day_env(tmp_path, "s1", "S", "NSE", settings.symbols)["OPENALGO_SYMBOLS"] == "SBIN,INFY"
    assert "OPENALGO_SYMBOLS" not in day_env(tmp_path / "b", "s1", "S", "NSE")
    assert settings.to_dict()["symbols"] == ["SBIN", "INFY"]
    assert (
        "symbols" not in ScriptSettings.from_dict({"capital": 1, "entry_time": "09:15"}).to_dict()
    )

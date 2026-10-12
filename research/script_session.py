"""
One trading day of a Python strategy, replayed: ``python -m research.script_session``.

The strategy host starts a script each trading day at its schedule start and
stops it at its schedule stop by ending the process. A backtest does the same,
one process per day (``research.script_replay``), so a script's module state
starts fresh each morning exactly as it does live. Inside that process:

**A virtual clock.** ``time.time``, ``time.sleep``, the ``localtime`` family and ``datetime.datetime.now``/``date.today`` read the
replay's clock, which starts at the schedule start. ``time.sleep`` returns at
once and moves the clock on; reaching the schedule stop ends the day the way
the host does, by ending the process, after the market has run to the close.
A script that paces itself with ``time.sleep`` (the strategy template's loop)
replays a day in seconds. pandas and pyarrow are imported before the clock, because their compiled
datetime code hangs if it first meets the stand-in types; another compiled
library that checks them at import may need adding to that list. What still
reads the real clock: ``time.monotonic``,
waits served by C code (``threading.Event.wait``, ``queue.get`` with a timeout,
``select``) and pandas' ``Timestamp.now()``. Sleeping in several threads at
once moves the one clock by every sleep, so such a script runs ahead of itself.

**The API on loopback.** ``research.script_sim`` answers the OpenAlgo API from
the snapshot on a port of this process's own, and ``HOST_SERVER`` points there.
The script needs no change: the ``openalgo`` SDK, or any HTTP client, reaches
it as it would reach OpenAlgo.

**Its own copy of the files it writes.** A write to a path outside the day's
folder, through Python's file functions, goes to a copy of that path inside
it, made from the live file first (``research.overlay``). A script that keeps
an audit log under a data directory runs unchanged, reads back what it wrote,
and never changes the live file.

**Two walls around it.** First, before anything else runs, Landlock
(``research.landlock``): from then on the kernel lets this process write only
inside its folder and connect over TCP only to its own port, whatever code
asks, compiled libraries included. Then an audit hook that cannot be removed
refuses any other connection, name lookup or datagram, any write or database
outside the folder that reached Python's lowest level unredirected, and any new
process. A backtest therefore cannot reach a broker or the real OpenAlgo and
cannot change the live installation's files. On a kernel without Landlock only
the audit hook stands, and code written to get around it (``ctypes``) is not
stopped; the run records which it had.

Arguments: ``<snapshot> <state.json> <script> <start epoch> <stop epoch> <settings json>``.
The state file carries cash, positions and every fill from day to day; it is
written once, when the day ends.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import socket
import sys
import threading
import time as _time
from pathlib import Path

_real_datetime = _datetime.datetime
_real_date = _datetime.date
_real = {
    name: getattr(_time, name)
    for name in (
        "time",
        "time_ns",
        "sleep",
        "localtime",
        "gmtime",
        "ctime",
        "strftime",
        "asctime",
    )
}


# ponytail: one clock shared by every thread; a per-thread sleeper queue if a
# threaded script needs faithful timing.
class VirtualClock:
    """The replay's clock: it moves only when the script sleeps."""

    def __init__(self, start: float, stop: float) -> None:
        self.now = float(start)
        self.stop = float(stop)
        self._lock = threading.Lock()
        self.on_sleep = lambda now: None
        self.on_stop = lambda: None

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        with self._lock:
            self.now += float(seconds)
            now = self.now
        if now >= self.stop:
            self.on_stop()
        self.on_sleep(now)

    def install(self) -> None:
        """Point the stdlib's clocks at this one. Done before anything else is imported."""
        clock = self

        def _default(fn):
            return lambda secs=None: fn(clock.now if secs is None else secs)

        _time.time = clock.time
        _time.time_ns = lambda: int(clock.now * 1e9)
        _time.sleep = clock.sleep
        # time.monotonic stays real on purpose: queue and threading time their
        # waits with it, and a frozen monotonic clock turns every timed wait
        # into one that never ends.
        _time.localtime = _default(_real["localtime"])
        _time.gmtime = _default(_real["gmtime"])
        _time.ctime = _default(_real["ctime"])
        _time.asctime = lambda t=None: _real["asctime"](
            _real["localtime"](clock.now) if t is None else t
        )
        _time.strftime = lambda fmt, t=None: _real["strftime"](
            fmt, _real["localtime"](clock.now) if t is None else t
        )

        class _Meta(type(_real_datetime)):
            # A real datetime (from pandas, or made before the patch) is still
            # one, so isinstance checks in a script keep working.
            def __instancecheck__(cls, obj):
                return isinstance(obj, cls._real)

        class FakeDate(_real_date, metaclass=_Meta):
            _real = _real_date

            @classmethod
            def today(cls):
                return _real_datetime.fromtimestamp(clock.now).date()

        class FakeDatetime(_real_datetime, metaclass=_Meta):
            _real = _real_datetime

            @classmethod
            def now(cls, tz=None):
                return _real_datetime.fromtimestamp(clock.now, tz)

            @classmethod
            def today(cls):
                return _real_datetime.fromtimestamp(clock.now)

            @classmethod
            def utcnow(cls):
                return _real_datetime.fromtimestamp(clock.now, _datetime.UTC).replace(tzinfo=None)

        _datetime.date = FakeDate
        _datetime.datetime = FakeDatetime


class Refused(PermissionError):
    """A replayed script tried something a backtest does not allow."""


#: Events that start a process: a child would run outside the wall.
PROCESS_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
    }
)
#: Events that change a path, with the argument holding it.
PATH_EVENTS = {
    "os.remove": 0,
    "os.rename": 0,
    "os.replace": 0,
    "os.rmdir": 0,
    "os.mkdir": 0,
    "os.truncate": 0,
    "os.chmod": 0,
    "os.chown": 0,
    "os.symlink": 1,
    "os.link": 1,
    "shutil.rmtree": 0,
    "shutil.copyfile": 1,
    "shutil.move": 1,
}
_WRITE = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def wall(folder: Path, port: int):
    """The audit hook that keeps a replayed script inside its day."""
    root = os.path.realpath(folder)

    def inside(path) -> bool:
        if isinstance(path, int):
            return True  # an already-open descriptor: its opening was checked
        path = os.fsdecode(path)
        if path in (os.devnull, ":memory:", ""):
            return True
        full = os.path.realpath(os.path.join(os.getcwd(), path))
        return full == root or full.startswith(root + os.sep)

    def hook(event: str, args) -> None:
        if event == "socket.connect":
            sock, address = args[0], args[1]
            if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", -1):
                return
            if isinstance(address, tuple) and address[:2] in ((host, port) for host in _LOOPBACK):
                return
            raise Refused("A backtest reaches only its own simulated OpenAlgo, never the network.")
        if event in ("socket.sendto", "socket.sendmsg"):
            if getattr(args[0], "family", None) != getattr(socket, "AF_UNIX", -1):
                raise Refused("A backtest never sends anything over the network.")
        elif event == "socket.getaddrinfo":
            if args[0] not in _LOOPBACK and args[0] is not None:
                raise Refused("A backtest reaches only its own simulated OpenAlgo.")
        elif event == "open":
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
            writes = flags & _WRITE or any(c in mode for c in "wax+")
            if writes and not inside(args[0]):
                raise Refused(f"A backtest writes only inside its own folder, not to {args[0]}.")
        elif event == "sqlite3.connect":
            if not inside(args[0]):
                raise Refused("A backtest opens only databases inside its own folder.")
        elif event in PATH_EVENTS:
            if not inside(args[PATH_EVENTS[event]]):
                raise Refused("A backtest changes only files inside its own folder.")
        elif event in PROCESS_EVENTS:
            raise Refused("A backtest does not start other processes.")

    return hook


def bind(handle):
    """A loopback HTTP server on a port of its own, answering ``handle(endpoint, body)``.

    Bound now, served by ``start``: the port has to be known before the kernel
    wall is raised, and the thread serving it is better started after.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Headers and body go out as two small writes; with Nagle's algorithm
        # the second waits for a delayed ACK, 40 ms a request on loopback. A
        # script polling all day made that most of a replay's time.
        disable_nagle_algorithm = True

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            path = self.path.split("?", 1)[0]
            endpoint = path.split("/api/v1/", 1)[-1].strip("/")
            status, payload = handle(endpoint, body if isinstance(body, dict) else {})
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_POST = do_GET = _answer

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server


def start(server) -> None:
    threading.Thread(target=server.serve_forever, name="backtest-api", daemon=True).start()


def main(argv: list[str]) -> int:
    snapshot, state_path, script = Path(argv[1]), Path(argv[2]), Path(argv[3])
    begin, stop, settings = float(argv[4]), float(argv[5]), json.loads(argv[6])
    sys.dont_write_bytecode = True
    folder = os.getcwd()
    target: dict = {}
    server = bind(lambda endpoint, body: target["handle"](endpoint, body))
    port = server.server_address[1]
    # First, while this process has one thread: Landlock binds the thread that
    # asks and the threads it starts afterwards, so nothing may start before it.
    from research import landlock

    os_wall = landlock.restrict([folder, os.devnull, "/dev/shm"], port)

    # Before the clock: compiled datetime code (pandas' tslibs, pyarrow) checks
    # the datetime types when it is first imported, and hangs if it finds the
    # replay's stand-ins. Imported now, they keep the real types, and a script
    # importing them later gets these copies.
    import pandas  # noqa: F401

    for optional in ("pyarrow", "pyarrow.parquet"):
        try:
            __import__(optional)
        except ImportError:
            pass
    from research.dataset import read_snapshot
    from research.overlay import Overlay
    from research.script_sim import Market, Simulator
    from research.strategy import ist_day, ist_minute
    from research.worker import install_guard

    clock = VirtualClock(begin, stop)
    clock.install()

    install_guard(where="a backtest of a Python strategy")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["os_wall"] = os_wall
    sim = Simulator(
        Market(read_snapshot(snapshot)), state, settings["buying_power"], settings["exchange"]
    )
    sim.start_day(begin)
    lock = threading.Lock()
    overlay = Overlay(folder)

    def handle(endpoint: str, body: dict):
        with lock:
            return sim.handle(endpoint, body, clock.now)

    def advance(now: float) -> None:
        with lock:
            sim.advance(now)

    def finish(code: int = 0, message: str | None = None) -> None:
        with lock:
            if message:
                at = int(clock.now)
                day = _real_date(1970, 1, 1) + _datetime.timedelta(days=ist_day(at))
                state["stopped_early"].append(
                    {
                        "day": day.isoformat(),
                        "at": f"{ist_minute(at) // 60:02d}:{ist_minute(at) % 60:02d}",
                        "message": message,
                    }
                )
            sim.end_day(ist_day(int(begin)))
            overlay.save()
            temp = state_path.with_suffix(".partial")
            temp.write_text(json.dumps(state), encoding="utf-8")
            temp.replace(state_path)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)

    target["handle"] = handle
    clock.on_sleep = advance
    clock.on_stop = finish
    start(server)
    host = f"http://127.0.0.1:{port}"
    os.environ.update(
        {
            "HOST_SERVER": host,
            "OPENALGO_HOST": host,
            "OPENALGO_API_KEY": "backtest",
            "WEBSOCKET_URL": f"ws://127.0.0.1:{port}",
        }
    )
    overlay.install()
    sys.addaudithook(wall(Path(folder), port))
    sys.argv = [str(script)]
    sys.path.insert(0, str(script.parent))
    import runpy
    import traceback

    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            finish(3, f"The script exited with code {exc.code}.")
    except BaseException as exc:
        traceback.print_exc()
        finish(3, f"The script stopped with an error: {exc}")
    finish(0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

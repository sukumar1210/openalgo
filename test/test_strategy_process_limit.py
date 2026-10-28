"""A strategy's process limit is a setting, and both launch paths follow it.

RLIMIT_NPROC counts every process and thread of the strategy's user ID on the
whole machine. In Docker the container's appuser is uid 1000, usually the host's
own desktop user too. On 2026-10-28 that user already ran ~270 threads (the
recorder, Claude Code sessions, OpenAlgo itself), so with the limit fixed at 256
both hosted strategies died at their 09:16 decision with "can't start new
thread". The eventlet launch hardcoded 256 in set_resource_limits; the gthread
bootstrap read a constant of 256. Now both read STRATEGY_NPROC_LIMIT, which the
environment sets (default 256, unchanged), and 0 sets no process limit.
"""

import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest

from blueprints import python_strategy as ps

pytestmark = pytest.mark.skipif(os.name == "nt", reason="resource limits are POSIX only")

ROOT = Path(__file__).resolve().parent.parent
REPORT = "import resource; print(resource.getrlimit(resource.RLIMIT_NPROC)[0])"


def granted(wanted):
    """What a non-root child can be given: never above the hard limit it inherits."""
    hard = resource.getrlimit(resource.RLIMIT_NPROC)[1]
    return wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)


def launched_limit():
    """The NPROC limit a strategy gets from the eventlet / dev-server launch (preexec_fn)."""
    out = subprocess.run(
        [sys.executable, "-c", REPORT],
        preexec_fn=ps.set_resource_limits,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return int(out.stdout.split()[-1])


def test_PORTED_DEFECT_the_eventlet_launch_follows_the_process_limit_setting(monkeypatch):
    monkeypatch.setattr(ps, "STRATEGY_NPROC_LIMIT", 4096)
    assert launched_limit() == granted(4096), "the launch the live worker uses ignored the setting"


def test_zero_sets_no_process_limit(monkeypatch):
    monkeypatch.setattr(ps, "STRATEGY_NPROC_LIMIT", 0)
    assert launched_limit() == resource.getrlimit(resource.RLIMIT_NPROC)[0]


def test_the_gthread_bootstrap_follows_the_same_setting(monkeypatch):
    monkeypatch.setattr(ps, "STRATEGY_NPROC_LIMIT", 4096)
    assert ps.apply_strategy_limits_env({})["OPENALGO_STRATEGY_NPROC"] == "4096"


@pytest.mark.parametrize("value, expected", [("4096", 4096), (None, 256)])
def test_the_setting_is_read_from_the_environment(value, expected):
    env = {k: v for k, v in os.environ.items() if k != "STRATEGY_NPROC_LIMIT"}
    if value is not None:
        env["STRATEGY_NPROC_LIMIT"] = value
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from blueprints import python_strategy as ps; print('NPROC', ps.STRATEGY_NPROC_LIMIT)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr[-500:]
    line = next(x for x in out.stdout.splitlines() if x.startswith("NPROC "))
    assert int(line.split()[1]) == expected

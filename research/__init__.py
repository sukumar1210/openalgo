"""
Research backtests: strategy versions, experiments and a deterministic engine.

This package is the research half of the platform, and the one rule it is built
around is that **it cannot reach a broker**. Nothing in it imports order
placement, the sandbox, the broker plugins or the broker history service, and
``research/worker.py`` installs an import guard that refuses them outright, so a
backtest is architecturally unable to send an order rather than merely
configured not to. ``test/research/test_safety.py`` holds that line.

Layout
------

* ``strategy``  the plugin contract, discovery and version identity
* ``engine``    the bar-by-bar simulation for single, isolated and shared modes
* ``dataset``   loading and hashing a market-data snapshot
* ``worker``    the subprocess entry point that runs one scenario
* ``reference`` the built-in reference strategy used by tests and UAT

The version constants below are part of every run's identity. Bump
``ENGINE_VERSION`` whenever a change could alter a result for unchanged inputs,
and ``CONFLICT_POLICY`` whenever shared-mode netting or allocation changes, so a
rerun is never mistaken for a reproduction of a run the old code produced.
"""

ENGINE_VERSION = "1"
CONFLICT_POLICY = "net-v1"

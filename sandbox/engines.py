"""
When the Sandbox's engines run: while Analyzer mode is on, or while anything
else trades in the Sandbox.

The execution engine fills resting orders (limit, stop-loss, GTT) and the
square-off scheduler closes intraday positions at the exchange's square-off
time. Both used to follow Analyzer mode alone, which was right while Analyzer
mode was the only way into the Sandbox. It is not any more: a Python strategy
in Sandbox mode, and a Sandbox account (sandbox/accounts.py), trade there with
Analyzer mode off. Without the engines their resting orders never fill and
their intraday positions are carried overnight.

So the engines start when the app starts or Analyzer mode is switched on, as
before, and also when either of the other two needs them; and switching
Analyzer mode off leaves them running while the others still do.
"""

from __future__ import annotations

from pathlib import Path

from utils.json_file import read_json
from utils.logging import get_logger

logger = get_logger(__name__)

#: The Python strategy host's configs (blueprints/python_strategy.CONFIG_FILE).
HOST_CONFIGS = Path("strategies") / "strategy_configs.json"


def needed_without_analyzer() -> bool:
    """Whether something other than Analyzer mode trades in the Sandbox."""
    from sandbox.accounts import any_active
    from utils.strategy_mode import SANDBOX, mode_of

    try:
        configs = read_json(HOST_CONFIGS, {})
    except Exception:
        logger.exception("Could not read the Python strategy configs")
        configs = {}
    in_sandbox = any(
        isinstance(config, dict) and mode_of(config) == SANDBOX for config in configs.values()
    )
    return in_sandbox or any_active()


def ensure_running() -> None:
    """Start both engines unless they already run."""
    from sandbox.execution_thread import start_execution_engine
    from sandbox.squareoff_thread import start_squareoff_scheduler

    ok, message = start_execution_engine()
    if not ok:
        logger.warning(f"Sandbox execution engine did not start: {message}")
    start_squareoff_scheduler()

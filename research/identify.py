"""
Read an uploaded strategy file's identity in a process of its own.

``python -m research.identify <key> <path> <out.json>``

The app never imports a trader's strategy file: the file may load a model or a
heavy library at import, and the app serves every request from one worker. This
process imports it under the same guard a backtest runs under (no order path,
no network), builds it once, and writes ``{"identity": ...}`` or
``{"error": message}`` to ``out.json``. The message is written for the trader;
the traceback goes to this process's log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def describe(exc: BaseException) -> str:
    """What went wrong loading the file, as the trader who wrote it can act on it."""
    from research.strategy import StrategyRefused

    if isinstance(exc, SyntaxError):
        return f"The file has a syntax error on line {exc.lineno}: {exc.msg}."
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        return f"The file imports {exc.name}, which is not installed on this server."
    if isinstance(exc, (StrategyRefused, TypeError, ValueError)):
        return str(exc)
    return f"The strategy could not be loaded: {exc}"


def main(argv: list[str]) -> int:
    key, path, out = argv[1], Path(argv[2]), Path(argv[3])
    from research.worker import install_guard

    install_guard(where="a strategy check")
    from research.strategy import file_spec, refuse_network, version_identity

    sys.addaudithook(refuse_network)
    try:
        result = {"identity": version_identity(file_spec(key, path))}
    except Exception as exc:
        from utils.logging import get_logger

        get_logger("research.identify").exception("Checking %s failed", path.name)
        result = {"error": describe(exc)}
    out.write_text(json.dumps(result, default=str), encoding="utf-8")
    return 0 if "identity" in result else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""
Named symbol lists for Python strategies: written once, chosen per strategy.

A list is a name, an exchange and its symbols. A strategy that uses one is
handed its symbols each time it starts, as ``OPENALGO_SYMBOLS`` (comma
separated), and a backtest of it gets the same list and downloads all of it
before replaying. So a universe shared by several strategies is typed once,
and changing it changes every strategy using it from that strategy's next start.

Lists live in ``strategies/symbol_lists.json``, beside the strategies' own
configs, so they persist in the same volume through upgrades and restarts.

They are kept apart from the Trading terminal's watchlists on purpose: what a
trader watches must never change what a strategy trades.
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from utils.json_file import read_json, write_json_atomic
from utils.logging import get_logger

logger = get_logger(__name__)

LISTS_FILE = Path("strategies") / "symbol_lists.json"
MAX_SYMBOLS = 500
MAX_NAME = 64
#: Yahoo-style suffixes people paste from elsewhere; OpenAlgo names have none.
_SUFFIXES = {".NS": "NSE", ".BO": "BSE"}

_lock = threading.Lock()


class SymbolListError(ValueError):
    """A list that cannot be saved or used, said so a trader can fix it."""


def parse_symbols(text: str) -> list[str]:
    """Symbols from pasted text: any mix of commas, spaces and new lines.

    Upper-cased, de-duplicated in the order given, with a ``.NS`` or ``.BO``
    suffix removed.
    """
    symbols: list[str] = []
    for raw in re.split(r"[\s,;]+", text or ""):
        symbol = raw.strip().upper()
        for suffix in _SUFFIXES:
            if symbol.endswith(suffix):
                symbol = symbol[: -len(suffix)]
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _unknown(symbols: list[str], exchange: str) -> list[str] | None:
    """Symbols the broker's master contract does not have; None when it is not loaded."""
    from database.token_db_enhanced import get_symbol_count, get_symbol_info

    if not get_symbol_count():
        return None
    return [s for s in symbols if get_symbol_info(s, exchange) is None]


def load_lists() -> dict[str, dict]:
    return read_json(LISTS_FILE, {})


def get_list(list_id: str | None) -> dict | None:
    return load_lists().get(list_id) if list_id else None


def save_list(list_id: str | None, name: str, exchange: str, symbols_text: str) -> dict:
    """Create a list (``list_id`` None) or replace one. Returns it with its id."""
    from database.market_calendar_db import SUPPORTED_EXCHANGES

    name = (name or "").strip()
    exchange = (exchange or "").strip().upper()
    symbols = parse_symbols(symbols_text)
    if not name or len(name) > MAX_NAME:
        raise SymbolListError(f"Give the list a name of up to {MAX_NAME} characters.")
    if exchange not in SUPPORTED_EXCHANGES:
        raise SymbolListError(f"Choose one of these exchanges: {', '.join(SUPPORTED_EXCHANGES)}.")
    if not symbols:
        raise SymbolListError("Paste at least one symbol.")
    if len(symbols) > MAX_SYMBOLS:
        raise SymbolListError(
            f"A list holds at most {MAX_SYMBOLS} symbols; this has {len(symbols)}."
        )
    unknown = _unknown(symbols, exchange)
    if unknown:
        raise SymbolListError(
            f"Not found on {exchange}: {', '.join(unknown[:20])}"
            + (f" and {len(unknown) - 20} more" if len(unknown) > 20 else "")
            + ". Check the spelling; OpenAlgo symbols have no suffix (RELIANCE, not RELIANCE.NS)."
        )
    with _lock:
        lists = load_lists()
        if list_id is not None and list_id not in lists:
            raise SymbolListError("That list no longer exists.")
        clash = [
            other
            for other_id, other in lists.items()
            if other_id != list_id and other["name"].casefold() == name.casefold()
        ]
        if clash:
            raise SymbolListError(f"A list named {name} already exists.")
        list_id = list_id or uuid.uuid4().hex[:12]
        lists[list_id] = {
            "name": name,
            "exchange": exchange,
            "symbols": symbols,
            "checked": unknown is not None,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        write_json_atomic(LISTS_FILE, lists)
    logger.info("Symbol list %s saved: %s, %d symbols on %s", list_id, name, len(symbols), exchange)
    return {"id": list_id, **lists[list_id]}


def delete_list(list_id: str, used_by: list[str]) -> None:
    """Delete a list no strategy uses; ``used_by`` names the ones that do."""
    if used_by:
        raise SymbolListError(
            f"{', '.join(used_by)} still use this list. Choose another list for "
            f"{'them' if len(used_by) > 1 else 'it'} first, on its Schedule tab."
        )
    with _lock:
        lists = load_lists()
        if lists.pop(list_id, None) is None:
            raise SymbolListError("That list no longer exists.")
        write_json_atomic(LISTS_FILE, lists)


def strategy_env(config: dict) -> dict[str, str]:
    """What a run of this strategy is handed for its list: its symbols, or nothing.

    A Run's source carries its symbols as they were when the Run took them,
    so a later edit to the list cannot change a source mid-life.
    """
    if config.get("symbols"):
        return {"OPENALGO_SYMBOLS": ",".join(config["symbols"])}
    chosen = config.get("symbol_list")
    if not chosen:
        return {}
    found = get_list(chosen)
    if found is None:
        raise SymbolListError(
            "This strategy's symbol list no longer exists. Choose a list on its Schedule tab, "
            "then start it again."
        )
    return {"OPENALGO_SYMBOLS": ",".join(found["symbols"])}

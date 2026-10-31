"""A Fyers update packet is published as one whole book, never half-applied.

Fyers' HSM feed sends a live update as one packet carrying every field that
changed. FyersHSMWebSocket applied it field by field and published a copy
after EACH changed field. A packet that moved the best bid and the best ask
together therefore went out first with the new bid and the old ask, crossed
whenever the price had moved up, and FyersAdapter's once-a-second filter kept
that first copy. In continuous trading on 2026-10-26 and 27, 1.9-2.1% of
OpenAlgo's books were crossed and 2.1-2.3% locked, against 0.000% and 0.000%
in the official SDK's on the same names, which applies a packet whole and
publishes once.

Now a packet is applied whole and published once, and FyersAdapter drops, and
reports, a crossed or locked NSE book in the continuous session as a defect.

No network: packets are built byte for byte and fed to the parser.
"""

import itertools
import re
import struct
import time
import types
from datetime import datetime

import pytest

import broker.fyers.streaming.fyers_adapter as fa
import broker.fyers.streaming.fyers_hsm_websocket as hsm

DP_TOPIC, SF_TOPIC, BARE_TOPIC = 7, 8, 9
DP, SF = "dp|nse_cm|3045", "sf|nse_cm|3045"
DEPTH = hsm.FyersHSMWebSocket.DEPTH_FIELDS
QUOTE = hsm.FyersHSMWebSocket.DATA_FIELDS
NO_VALUE = -2147483648  # what the feed sends for a field it does not change


def update(topic: int, fields: list[str], changes: dict[str, int]) -> bytes:
    """One live update (data type 85): field slots up to the last change, NO_VALUE where unchanged."""
    last = max(fields.index(name) for name in changes)
    values = [changes.get(name, NO_VALUE) for name in fields[: last + 1]]
    return (
        bytes([85])
        + struct.pack("H", topic)
        + struct.pack("B", len(values))
        + b"".join(struct.pack(">i", v) for v in values)
    )


def feed(*updates: bytes) -> bytearray:
    """A data-feed message: a 7-byte header, the scrip count, then the updates."""
    return bytearray(b"\x00" * 7 + struct.pack("!H", len(updates)) + b"".join(updates))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(hsm.FyersHSMWebSocket, "_extract_hsm_key", lambda self, token: "hsm-key")
    c = hsm.FyersHSMWebSocket("app:token")
    c.subscriptions = {DP_TOPIC: DP, SF_TOPIC: SF, BARE_TOPIC: "dp|nse_cm|1333"}
    c.depth_data[DP_TOPIC] = {
        "type": "dp",
        "hsm_token": DP,
        "bid_price1": 95275,
        "ask_price1": 95290,
    }
    c.scrips_data[SF_TOPIC] = {
        "type": "sf",
        "hsm_token": SF,
        "ltp": 95280,
        "vol_traded_today": 1000,
    }
    c.sent = []
    c.on_message_callback = c.sent.append
    return c


def test_PORTED_DEFECT_a_packet_moving_both_sides_is_published_once_and_not_crossed(client):
    client._parse_data_feed(
        feed(update(DP_TOPIC, DEPTH, {"bid_price1": 95295, "ask_price1": 95300}))
    )
    assert len(client.sent) == 1, "a half-applied copy went out before the whole book"
    (book,) = client.sent
    assert (book["bid_price1"], book["ask_price1"]) == (95295, 95300)
    assert book["update_type"] == "live"


def test_a_quote_packet_is_published_once_with_every_field(client):
    client._parse_data_feed(feed(update(SF_TOPIC, QUOTE, {"ltp": 95300, "vol_traded_today": 1025})))
    assert [(q["ltp"], q["vol_traded_today"]) for q in client.sent] == [(95300, 1025)]


def test_a_packet_that_changes_nothing_publishes_nothing(client):
    client._parse_data_feed(feed(update(DP_TOPIC, DEPTH, {"bid_price1": 95275})))
    assert client.sent == []


def test_topics_without_a_snapshot_are_skipped_whole(client):
    """An unknown topic, or a subscribed one with no snapshot yet, must not shift the next topic's fields."""
    client._parse_data_feed(
        feed(
            update(99, DEPTH, {"bid_price1": 1, "ask_price1": 2}),
            update(BARE_TOPIC, DEPTH, {"bid_price1": 3, "ask_price1": 4}),
            update(SF_TOPIC, QUOTE, {"ltp": 95310}),
        )
    )
    assert [q["ltp"] for q in client.sent] == [95310]


# ---------------------------------------------------------------- the crossed-book guard


def dp(bid: int, ask: int) -> dict:
    return {
        "type": "dp", "hsm_token": DP, "original_symbol": "NSE:SBIN-EQ", "precision": 2, "multiplier": 1,
        "bid_price1": bid, "bid_size1": 100, "bid_order1": 3, "ask_price1": ask, "ask_size1": 120, "ask_order1": 4,
    }  # fmt: skip


@pytest.fixture
def adapter(monkeypatch):
    a = fa.FyersAdapter("app:token", "trader")
    a.connected = True
    a.ws_client = types.SimpleNamespace(
        subscribe_symbols=lambda tokens, mappings=None: None,
        is_connected=lambda: True,
        disconnect=lambda: None,
    )

    def convert(symbols, data_type):
        token = DP if data_type == "DepthUpdate" else SF
        return [token], {token: "NSE:SBIN-EQ"}, []

    monkeypatch.setattr(a.token_converter, "convert_openalgo_symbols_to_hsm", convert)
    monkeypatch.setattr(fa, "get_br_symbol", lambda symbol, exchange: "NSE:SBIN-EQ")
    clock = itertools.count(1_000_000, 2)  # each message in a second of its own, past the dedupe
    a.mono = 0.0  # the clock the once-an-hour report is timed by, moved by the tests
    # The module's own clock, not the process-wide time.time.
    monkeypatch.setattr(
        fa,
        "time",
        types.SimpleNamespace(time=lambda: next(clock), monotonic=lambda: a.mono, sleep=time.sleep),
    )
    warned = []
    monkeypatch.setattr(a.logger, "warning", warned.append)
    got = []
    assert a.subscribe_symbols([{"exchange": "NSE", "symbol": "SBIN"}], "DepthUpdate", got.append)
    return a, got, warned


def at(monkeypatch, hour, minute):
    monkeypatch.setattr(fa, "_ist_now", lambda: datetime(2026, 11, 2, hour, minute, tzinfo=fa.IST))


def test_PLANTED_a_crossed_book_in_continuous_trading_is_dropped_and_reported(adapter, monkeypatch):
    a, got, warned = adapter
    at(monkeypatch, 10, 0)
    a._on_message(dp(bid=95300, ask=95290))
    assert got == [], "a crossed book reached the subscribers"
    assert a.crossed_rejected == 1 and len(warned) == 1 and "NSE:SBIN" in warned[0]
    a._on_message(dp(bid=95295, ask=95295))
    assert got == [] and a.crossed_rejected == 2, "a locked book is a defect there too"
    assert len(warned) == 1, "reported at most once an hour per symbol"
    a._on_message(dp(bid=95285, ask=95290))
    assert [b["depth"]["buy"][0]["price"] for b in got] == [952.85]


@pytest.mark.parametrize("hour, minute", [(9, 10), (9, 15), (15, 20), (15, 25)])
def test_auction_books_are_left_alone(adapter, monkeypatch, hour, minute):
    a, got, _ = adapter
    at(monkeypatch, hour, minute)
    a._on_message(dp(bid=95300, ask=95290))
    assert len(got) == 1 and a.crossed_rejected == 0


def test_the_guard_is_for_nse_cash_only(adapter, monkeypatch):
    a, _, _ = adapter
    at(monkeypatch, 10, 0)
    assert a._crossed_in_continuous("NSE", dp(95300, 95290))
    for exchange in ("NFO", "BSE", "MCX"):
        assert not a._crossed_in_continuous(exchange, dp(95300, 95290))
    assert not a._crossed_in_continuous("NSE", dp(0, 95290)), "an empty side is not a crossing"


def test_PLANTED_every_dropped_book_is_accounted_for(adapter, monkeypatch):
    """One symbol crossing every 2 s from 14:25: 3,300 drops, all in the log and the status."""
    a, got, warned = adapter
    at(monkeypatch, 14, 25)
    for i in range(3300):
        a.mono = 2.0 * i
        a._on_message(dp(bid=95300, ask=95290))
    assert got == [] and a.crossed_rejected == 3300
    reported = [int(m) for w in warned for m in re.findall(r"Dropped (\d+) crossed", w)]
    assert len(reported) == 2, "once an hour: at the first drop and an hour later"
    status = a.get_connection_status()
    assert status["crossed_books_dropped"] == 3300
    assert status["crossed_books_dropped_today"] == {"NSE:SBIN": 3300}

    at(monkeypatch, 15, 25)  # the window has closed: the next book brings the day's totals
    a._on_message(dp(bid=95285, ask=95290))
    (summary,) = [w for w in warned if "on 2026-11-02" in w]
    today, unreported = map(
        int, re.search(r"NSE:SBIN (\d+) \((\d+) not reported", summary).groups()
    )
    assert today == 3300 and sum(reported) + unreported == 3300, "drops went unaccounted for"
    assert a.get_connection_status()["crossed_books_dropped_today"] == {}, "a new day starts afresh"
    assert a.crossed_rejected == 3300


def drops(text: str) -> tuple[int, int]:
    """(today, not reported before) for NSE:SBIN in a day's summary line."""
    today, unreported = re.search(r"NSE:SBIN (\d+) \((\d+) not reported", text).groups()
    return int(today), int(unreported)


def test_PLANTED_a_shutdown_logs_the_days_drops_before_they_are_discarded(adapter, monkeypatch):
    """The last client leaves at 15:10: the adapter is discarded, and its count with it."""
    a, _, warned = adapter
    at(monkeypatch, 14, 25)
    for i in range(2700):
        a.mono = float(i)
        a._on_message(dp(bid=95300, ask=95290))
    reported = sum(int(m) for w in warned for m in re.findall(r"Dropped (\d+) crossed", w))
    a.disconnect(clear_mappings=True)
    (summary,) = [w for w in warned if "on 2026-11-02" in w]
    today, unreported = drops(summary)
    assert today == 2700 and reported + unreported == 2700, "the shutdown lost the day's drops"


def test_PLANTED_a_quote_packet_closes_the_window_after_depth_subscribers_have_gone(
    adapter, monkeypatch
):
    a, _, warned = adapter
    quotes = []
    assert a.subscribe_symbols(
        [{"exchange": "NSE", "symbol": "SBIN"}], "SymbolUpdate", quotes.append
    )
    at(monkeypatch, 14, 25)
    for _ in range(5):
        a._on_message(dp(bid=95300, ask=95290))
    a.subscription_callbacks.pop("DepthUpdate_NSE:SBIN")  # depth subscribers left at 15:10
    at(monkeypatch, 15, 25)
    a._on_message(
        {"type": "sf", "hsm_token": SF, "original_symbol": "NSE:SBIN-EQ", "precision": 2,
         "multiplier": 1, "ltp": 95285, "vol_traded_today": 1000, "last_traded_qty": 1}
    )  # fmt: skip
    assert quotes, "the quote itself still reaches its subscriber"
    (summary,) = [w for w in warned if "on 2026-11-02" in w]
    assert drops(summary)[0] == 5
    assert a.get_connection_status()["crossed_books_dropped_today"] == {}, (
        "yesterday shown as today"
    )

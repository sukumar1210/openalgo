"""A 50-level (TBT) subscription never holds the websocket proxy, and never silences another client.

The proxy calls the broker adapter's subscribe() inside its asyncio event loop, the same loop that
relays every client's ticks. On 2026-10-27 at 15:12 one client subscribed RELIANCE:50 and
HDFCBANK:50:
- the first subscribe built the TBT client, whose constructor looked up the socket URL (an HTTP call,
  up to 10s) and whose connect() then polled up to 15s for the socket to open. No client got a tick
  for 20 seconds, 15:12:02 to 15:12:21;
- the socket did not open in time, so RELIANCE:50 fell back to 5-level depth. That fallback was
  registered under RELIANCE's own name, replacing the callback of the client already taking
  RELIANCE's depth. The pool fans that one stream out to quote and LTP, so every client on RELIANCE,
  the sandbox's prices included, went silent from 15:12:27 to the close;
- unsubscribing the last 50-level symbol joined the client's threads, and the watchdog sleeps 30s at
  a time: the loop was held again for 21 seconds, 15:16:01 to 15:16:22.

Now the socket opens on its own thread and subscriptions made before it opens are sent when it does;
a socket that never opens is replaced by labelled 5-level depth under the ":50" name; teardown does
not join. The 50-level message also stops inventing a price (the bid/ask midpoint) and carries
Fyers' sequence number.

No network: the socket and the URL lookup are replaced.
"""

import gc
import json
import threading
import time
import types

import websocket_proxy  # noqa: F401  isort:skip

import pytest

import broker.fyers.streaming.fyers_tbt_websocket as tbt
import broker.fyers.streaming.fyers_websocket_adapter as wsa
from broker.fyers.streaming import msg_pb2
from broker.fyers.streaming.fyers_mapping import FyersDataMapper

SOCKETS = []


class FakeSocket:
    """A TBT socket whose handshake completes when the test says so, and which records what is sent."""

    def __init__(
        self, url, header=None, on_message=None, on_error=None, on_close=None, on_open=None
    ):
        self.on_open, self.on_close = on_open, on_close
        self.handshake = threading.Event()
        self.closed = threading.Event()
        self.sent = []
        SOCKETS.append(self)

    def run_forever(self, ping_interval=0):
        self.handshake.wait()  # a handshake in flight cannot be interrupted, as with the real client
        self.on_open(self)
        self.closed.wait()
        self.on_close(self, 1000, "closed")

    def send(self, message):
        self.sent.append(json.loads(message))

    def close(self, **kwargs):
        self.closed.set()


def shut(a):
    """Tear the adapter down now, while the test's log streams are open, not in __del__ at exit."""
    a.fyers_adapter = None
    a.disconnect()
    gc.collect()


def until(condition, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


def offline(*args, **kwargs):
    raise OSError("no network in tests")


@pytest.fixture
def no_network(monkeypatch):
    SOCKETS.clear()
    monkeypatch.setattr(tbt.websocket, "WebSocketApp", FakeSocket)
    monkeypatch.setattr(tbt.requests, "get", offline)
    yield
    for s in SOCKETS:  # let every fake socket's thread finish
        s.closed.set()
        s.handshake.set()


@pytest.fixture
def adapter(no_network, monkeypatch):
    a = wsa.FyersWebSocketAdapter()
    a.connected = True
    a.access_token = "token"
    a.fyers_adapter = types.SimpleNamespace(connected=True, subscription_callbacks={})
    a.hsm = []
    monkeypatch.setattr(a, "_enqueue_hsm_subscribe", lambda *args, **kw: a.hsm.append((args, kw)))
    monkeypatch.setattr(
        a, "_convert_to_fyers_ticker", lambda symbol, exchange: f"{exchange}:{symbol}-EQ"
    )
    a.published = []
    monkeypatch.setattr(a, "_send_data", lambda data: a.published.append(dict(data)))
    yield a
    shut(a)


def subscribes(sock):
    return [m["data"]["symbols"] for m in sock.sent if m["type"] == 1 and m["data"]["subs"] == 1]


def test_PORTED_DEFECT_a_first_50_level_subscribe_returns_before_the_socket_opens(
    adapter, monkeypatch
):
    def slow_lookup(*args, **kwargs):
        time.sleep(1)
        raise OSError("slow")

    monkeypatch.setattr(tbt.requests, "get", slow_lookup)
    t0 = time.monotonic()
    result = adapter.subscribe("RELIANCE:50", "NSE", mode=3)
    took = time.monotonic() - t0
    assert result["status"] == "success"
    assert took < 0.5, f"subscribe held the proxy's event loop for {took:.1f}s"
    assert adapter.hsm == [], "nothing falls back while the socket is still opening"


def test_subscriptions_made_while_the_socket_opens_are_sent_once_when_it_opens(adapter):
    adapter.subscribe("RELIANCE:50", "NSE", mode=3)
    adapter.subscribe("HDFCBANK:50", "NSE", mode=3)
    assert until(lambda: SOCKETS)
    sock = SOCKETS[0]
    time.sleep(0.3)  # past the client's 150ms subscribe batch, which finds the socket not yet open
    sock.handshake.set()
    assert until(lambda: any(m["type"] == 2 for m in sock.sent))
    assert subscribes(sock) == [["NSE:HDFCBANK-EQ", "NSE:RELIANCE-EQ"]], (
        "both, in one message, once"
    )
    resume = next(m for m in sock.sent if m["type"] == 2)
    assert resume["data"]["resumeChannels"] == ["1"]
    assert sock.sent.index(resume) > 0, "subscribe first, then resume, as the batch flush does"


def test_a_symbol_dropped_while_the_socket_opens_is_not_sent(adapter):
    adapter.subscribe("RELIANCE:50", "NSE", mode=3)
    adapter.subscribe("HDFCBANK:50", "NSE", mode=3)
    adapter.unsubscribe("RELIANCE:50", "NSE", mode=3)
    assert until(lambda: SOCKETS)
    SOCKETS[0].handshake.set()
    assert until(lambda: subscribes(SOCKETS[0]))
    assert subscribes(SOCKETS[0]) == [["NSE:HDFCBANK-EQ"]]


def test_a_socket_that_never_opens_falls_back_to_five_levels_and_says_so(adapter, monkeypatch):
    monkeypatch.setattr(wsa.FyersWebSocketAdapter, "TBT_OPEN_TIMEOUT_SEC", 0.3)
    adapter.subscribe("RELIANCE:50", "NSE", mode=3)
    client = adapter.tbt_client
    assert until(lambda: adapter.hsm), "the 50-level client was left with nothing"
    (args, kw) = adapter.hsm[0]
    assert args[:3] == ("DepthUpdate", "NSE", "RELIANCE") and kw["topic_symbol"] == "RELIANCE:50"
    args[3]({"exchange": "NSE", "symbol": "RELIANCE", "depth": {"buy": [], "sell": []}})
    (msg,) = adapter.published
    assert msg["symbol"] == "RELIANCE:50"
    assert (msg["is_50_depth"], msg["depth_levels"]) == (False, 5), "a client must be able to tell"
    assert adapter.tbt_client is None and not client.running, (
        "the socket that never opened is stopped"
    )


def test_a_client_that_gave_up_reconnecting_is_replaced_and_keeps_its_symbols(adapter):
    adapter.subscribe("RELIANCE:50", "NSE", mode=3)
    assert until(lambda: SOCKETS)
    first = adapter.tbt_client
    SOCKETS[0].handshake.set()
    assert until(lambda: first.connected)
    first.running = False  # what _handle_reconnect leaves after its last attempt
    adapter.subscribe("HDFCBANK:50", "NSE", mode=3)
    assert adapter.tbt_client is not first
    assert until(lambda: len(SOCKETS) == 2)
    SOCKETS[1].handshake.set()
    assert until(lambda: subscribes(SOCKETS[1]))
    assert subscribes(SOCKETS[1]) == [["NSE:HDFCBANK-EQ", "NSE:RELIANCE-EQ"]]


def test_a_handshake_that_completes_after_teardown_does_not_start_the_client(no_network):
    opened = []
    c = tbt.FyersTbtWebSocket("app:token")
    c.set_callbacks(on_open=lambda: opened.append(1))
    assert c.connect()
    assert until(lambda: SOCKETS)
    c.disconnect(wait=False)
    SOCKETS[0].handshake.set()
    c.ws_thread.join(2)
    assert not c.ws_thread.is_alive()
    assert opened == [] and not c.connected and SOCKETS[0].closed.is_set()


def test_unsubscribing_the_last_50_level_symbol_does_not_hold_the_loop(adapter):
    adapter.subscribe("RELIANCE:50", "NSE", mode=3)
    assert until(lambda: SOCKETS)
    client = adapter.tbt_client
    SOCKETS[0].handshake.set()
    assert until(lambda: client.connected and client.health_check_thread is not None)
    t0 = time.monotonic()
    adapter.unsubscribe("RELIANCE:50", "NSE", mode=3)
    took = time.monotonic() - t0
    assert took < 0.5, f"the last 50-level unsubscribe held the proxy's event loop for {took:.1f}s"
    for t in (client.ws_thread, client.ping_thread, client.health_check_thread):
        t.join(2)
        assert not t.is_alive(), f"{t.name} outlived its client"


def test_PORTED_DEFECT_a_five_level_fallback_never_takes_the_plain_symbols_stream(monkeypatch):
    """15:12:27 on 2026-10-27: RELIANCE:50 fell back to 5 levels and RELIANCE went silent for all."""
    a = wsa.FyersWebSocketAdapter()
    a.connected = True
    dispatchers = []
    a.fyers_adapter = types.SimpleNamespace(
        connected=True,
        subscription_callbacks={},
        subscribe_depth=lambda info, cb: dispatchers.append((info, cb)),
        subscribe_quote=lambda info, cb: dispatchers.append((info, cb)),
    )
    monkeypatch.setattr(a, "_subscribe_tbt_depth", lambda *args, **kw: False)  # TBT unavailable
    published = []
    monkeypatch.setattr(a, "_send_data", lambda data: published.append(dict(data)))
    try:
        a.subscribe("RELIANCE", "NSE", mode=3)  # the recorder; through the pool, quote and LTP too
        a.subscribe("RELIANCE:50", "NSE", mode=3)  # a 50-level client served from 5 levels
        a._flush_hsm_batch()
        assert until(lambda: dispatchers)
        info, dispatch = dispatchers[-1]
        assert info == [{"exchange": "NSE", "symbol": "RELIANCE"}], (
            "one broker subscription serves both"
        )

        def tick():
            dispatch({"exchange": "NSE", "symbol": "RELIANCE", "depth": {"buy": [], "sell": []}})

        tick()
        assert sorted(p["symbol"] for p in published) == ["RELIANCE", "RELIANCE:50"]
        plain = next(p for p in published if p["symbol"] == "RELIANCE")
        assert "is_50_depth" not in plain
        published.clear()
        a.unsubscribe("RELIANCE:50", "NSE", mode=3)
        tick()
        assert [p["symbol"] for p in published] == ["RELIANCE"], (
            "the plain stream outlives the 50-level client"
        )
    finally:
        shut(a)


def test_a_50_level_message_carries_no_made_up_price_and_keeps_the_feeds_order(no_network):
    feed = msg_pb2.MarketFeed(ticker="NSE:RELIANCE-EQ", sequence_no=4242)
    feed.feed_time.value, feed.send_time.value = 1_000, 1_001
    for side, price in ((feed.depth.bids, 130010), (feed.depth.asks, 130030)):
        level = side.add()
        level.num.value, level.price.value, level.qty.value, level.nord.value = 0, price, 25, 2
    depth = tbt.FyersTbtWebSocket("app:token")._extract_depth("NSE:RELIANCE-EQ", feed, False)
    mapped = FyersDataMapper().map_tbt_depth_to_openalgo(
        "NSE:RELIANCE-EQ", depth, "RELIANCE:50", "NSE"
    )
    assert "ltp" not in mapped, "the midpoint 1300.2 is not a price anything traded at"
    assert (mapped["sequence_no"], mapped["snapshot"], mapped["send_time"]) == (4242, False, 1_001)
    assert mapped["depth"]["buy"][0]["price"] == 1300.1 and mapped["is_50_depth"] is True

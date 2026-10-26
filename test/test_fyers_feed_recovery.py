"""The Fyers market data feed comes back after an outage with every symbol it had.

On 2026-10-26 the network dropped at 12:00 for about 55 minutes. Three
defects turned that into 49 symbols silent until past 15:00:

* **The HSM client gave up.** After 10 reconnect attempts, about nine minutes,
  it stopped for good, so nothing retried once the network was back.
* **A replacement client knew none of the symbols.** The next subscribe found
  the adapter disconnected and built a new client with an empty subscription
  list. Only symbols the pool had never seen were sent to it; for the rest the
  pool answered "already subscribed" and never asked again.
* **The adapter never noticed a reconnect.** After the client reconnected by
  itself the adapter still read as disconnected, so the next subscribe built a
  second client beside the live one.

No network: the WebSocket, the sleeps and the token are replaced.
"""

import pytest

import broker.fyers.streaming.fyers_adapter as fa
import broker.fyers.streaming.fyers_hsm_websocket as hsm
from websocket_proxy.connection_manager import _is_auth_error

SYMBOLS = ["sf|nse_cm|2885", "sf|nse_cm|1333"]
MAPPINGS = {"sf|nse_cm|2885": "NSE:RELIANCE-EQ", "sf|nse_cm|1333": "NSE:HDFCBANK-EQ"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(hsm.FyersHSMWebSocket, "_extract_hsm_key", lambda self, token: "hsm-key")
    monkeypatch.setattr(hsm.time, "sleep", lambda seconds: None)


def client(**state):
    c = hsm.FyersHSMWebSocket("app:token")
    for name, value in state.items():
        setattr(c, name, value)
    return c


class SentFrames:
    def __init__(self):
        self.frames = []

    def send(self, frame, opcode=None):
        self.frames.append(frame)

    def close(self):
        pass


def test_PORTED_DEFECT_the_client_keeps_reconnecting_past_ten_attempts(monkeypatch):
    """It stopped for good after 10 attempts; now only a disconnect stops it."""
    c = client(running=True)
    attempts = []

    class DeadNetwork:
        def __init__(self, *args, **kwargs):
            pass

        def run_forever(self, **kwargs):
            attempts.append(1)
            if len(attempts) == 25:
                c.running = False  # the disconnect, the only thing that ends it

    monkeypatch.setattr(hsm.websocket, "WebSocketApp", DeadNetwork)
    monkeypatch.setattr(c, "_refresh_access_token", lambda: None)
    c._run_websocket()
    assert len(attempts) == 25


def test_the_reconnect_delay_is_capped(monkeypatch):
    delays = []
    monkeypatch.setattr(hsm.time, "sleep", delays.append)
    c = client()
    for _ in range(12):
        c._handle_reconnect()
    assert delays[:5] == [5, 10, 20, 40, 60]
    assert max(delays) == c.MAX_RECONNECT_DELAY == 60


def test_a_replacement_client_subscribes_the_old_ones_symbols_again():
    old = client(_pending_hsm_symbols=list(SYMBOLS), symbol_mappings=dict(MAPPINGS))
    new = client()
    new.take_subscriptions(old)
    new.authenticated, new.ws = True, SentFrames()
    new._resubscribe_all()  # what authentication does when symbols are pending
    assert new._pending_hsm_symbols == SYMBOLS
    assert new.symbol_mappings == MAPPINGS
    assert new.ws.frames == [new._create_subscription_message(SYMBOLS, channel=11)]


class OfflineClient(hsm.FyersHSMWebSocket):
    """Connects without a network, as a real one does once the network is back."""

    def connect(self):
        self.running = self.connected = self.authenticated = True
        self.ws = SentFrames()
        if self._pending_hsm_symbols:
            self._resubscribe_all()


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(fa, "FyersHSMWebSocket", OfflineClient)
    return fa.FyersAdapter("app:token", "trader")


def test_PORTED_DEFECT_a_stopped_clients_symbols_move_to_its_replacement(adapter):
    old = OfflineClient("app:token")
    old._pending_hsm_symbols, old.symbol_mappings = list(SYMBOLS), dict(MAPPINGS)
    old.running = False  # stopped: the outage outlasted it
    old.set_callbacks(on_open=adapter._on_open, on_close=adapter._on_close)
    adapter.ws_client = old

    assert adapter.connect() is True
    new = adapter.ws_client
    assert new is not old
    assert new.ws.frames == [new._create_subscription_message(SYMBOLS, channel=11)]
    assert old.on_close_callback is None, "the old client can no longer flip the adapter"


def test_PORTED_DEFECT_no_second_client_while_the_first_is_still_reconnecting(adapter):
    retrying = OfflineClient("app:token")
    retrying.running = True  # in its own backoff, with every symbol it was given
    adapter.ws_client = retrying

    assert adapter.connect() is False
    assert adapter.ws_client is retrying
    # The pool tears the whole adapter down on an auth error; this is not one.
    assert adapter.last_error and not _is_auth_error(adapter.last_error)


def test_PORTED_DEFECT_the_adapter_reads_connected_after_the_clients_own_reconnect(adapter):
    adapter.connected = True
    adapter._on_close()
    assert adapter.connected is False
    adapter._on_open()  # the client reconnected and authenticated by itself
    assert adapter.connected is True

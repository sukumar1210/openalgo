"""An empty Fyers history range is an answer, not a failure.

Fyers answers a range with no trades with
{"s": "no_data", "code": 200, "candles": [], "message": ""}. get_history
treated every status other than "ok" as an error and retried it three times
with 2, 4 and 6 s of backoff, then slept 1 s before the next chunk: about 13 s
for each empty chunk, whether it spanned one day or 55. On the night of
2026-10-30 that was about 6.5 hours of the derivatives collector's run: 884
contracts answered no_data, at 27.5 s each against 0.6 s for a filled one.

Now no_data ends the chunk at once. A real error still retries with the same
backoff, and HTTP 429 is still retried inside get_api_response.

No network: the HTTP layer and the symbol lookup are replaced.
"""

import time
import types

import httpx
import pytest

import broker.fyers.api.data as fd

NO_DATA = {"s": "no_data", "code": 200, "candles": [], "message": ""}


def candles(day_epoch, n=3):
    """Equity candles, as Fyers sends them without OI: [epoch, o, h, l, c, volume]."""
    return [[day_epoch + 60 * i, 100.0, 101.0, 99.5, 100.5, 10] for i in range(n)]


@pytest.fixture
def fyers(monkeypatch):
    """BrokerData with get_api_response answering from a script of responses."""
    world = {"answers": [], "calls": []}

    def answer(endpoint, auth, *a, **k):
        world["calls"].append(endpoint)
        return world["answers"].pop(0) if world["answers"] else NO_DATA

    monkeypatch.setattr(fd, "get_api_response", answer)
    monkeypatch.setattr(fd, "get_br_symbol", lambda symbol, exchange: f"NSE:{symbol}")
    return fd.BrokerData("token"), world


def test_PORTED_DEFECT_an_empty_range_returns_at_once(fyers):
    data, world = fyers
    t0 = time.monotonic()
    df = data.get_history("SBIN29DEC26930CE", "NFO", "1m", "2026-10-30", "2026-10-30")
    took = time.monotonic() - t0
    assert df.empty
    assert took < 1.0, f"an empty range took {took:.1f}s (retried as if it had failed)"
    assert len(world["calls"]) == 1, "an empty answer was asked again"


def test_a_real_error_still_backs_off(fyers, monkeypatch):
    data, world = fyers
    slept = []
    monkeypatch.setattr(fd, "time", types.SimpleNamespace(sleep=slept.append, time=time.time))
    world["answers"] = [{"s": "error", "code": -300, "message": "Invalid symbol"}] * 4
    df = data.get_history("SBIN", "NSE", "1m", "2026-10-30", "2026-10-30")
    assert df.empty
    assert len(world["calls"]) == 4, "a failed chunk is tried once and retried three times"
    assert slept == [2, 4, 6, 1]


def test_an_empty_chunk_beside_a_filled_one(fyers, monkeypatch):
    data, world = fyers
    slept = []
    monkeypatch.setattr(fd, "time", types.SimpleNamespace(sleep=slept.append, time=time.time))
    day2 = 1_793_000_000
    # 70 days at 1m resolution is two 60-day chunks: the first empty, the second filled.
    world["answers"] = [NO_DATA, {"s": "ok", "code": 200, "candles": candles(day2)}]
    df = data.get_history("SBIN", "NSE", "1m", "2026-08-21", "2026-10-29")
    assert len(world["calls"]) == 2
    assert len(df) == 3 and slept == []


def test_http_429_is_still_retried_with_backoff(monkeypatch):
    slept, sent = [], []
    request = httpx.Request("GET", "https://api-t1.fyers.in/data/history")

    class Client:
        def get(self, url, headers=None):
            sent.append(url)
            status = 429 if len(sent) == 1 else 200
            return httpx.Response(status, json=NO_DATA if status == 200 else {}, request=request)

    monkeypatch.setattr(fd, "get_httpx_client", lambda: Client())
    monkeypatch.setattr(fd, "apply_rate_limit", lambda: None)
    monkeypatch.setattr(fd, "time", types.SimpleNamespace(sleep=slept.append, time=time.time))
    assert fd.get_api_response("/data/history?x=1", "token") == NO_DATA
    assert len(sent) == 2 and len(slept) == 1 and slept[0] > 0, "a 429 was not waited out"

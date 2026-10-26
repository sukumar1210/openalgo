"""A Fyers depth message carries the last traded price, never the bid/ask midpoint.

Fyers' HSM depth packets ("dp") carry the order book and no trade. The mapping
filled the gap with ltp = (best bid + best ask) / 2, and the connection pool
sends a broker only the highest mode it was asked for: once any client took a
symbol in Depth, every message for it was a depth message, handed to Quote and
LTP subscribers too. On 2026-10-26 all 1.5 million quotes the recorder stored
had that midpoint as their price and 0 as their volume, and the Sandbox's live
order engine checked pending orders against the midpoint.

A depth subscription now also takes the quote feed ("sf"), and a depth message
carries the latest quote's traded price, volume and day range. Until a quote has
arrived it carries no "ltp" at all.

No network and no database: the socket, the symbol-token API and the symbol
lookup are replaced.
"""

import itertools
import types

import pytest

import broker.fyers.streaming.fyers_adapter as fa
from broker.fyers.streaming.fyers_mapping import FyersDataMapper

SF, DP = "sf|nse_cm|3045", "dp|nse_cm|3045"


def sf_packet(ltp, volume):
    """A full quote as the HSM client hands it over, prices in paise."""
    return {
        "type": "sf", "hsm_token": SF, "original_symbol": "NSE:SBIN-EQ", "precision": 2, "multiplier": 1,
        "ltp": ltp, "vol_traded_today": volume, "last_traded_qty": 25, "avg_trade_price": 95210,
        "open_price": 94800, "high_price": 95600, "low_price": 94700, "prev_close_price": 94500,
    }  # fmt: skip


def dp_packet(bid, ask):
    return {
        "type": "dp", "hsm_token": DP, "original_symbol": "NSE:SBIN-EQ", "precision": 2, "multiplier": 1,
        "bid_price1": bid, "bid_size1": 100, "bid_order1": 3, "ask_price1": ask, "ask_size1": 120, "ask_order1": 4,
    }  # fmt: skip


@pytest.fixture
def adapter(monkeypatch):
    a = fa.FyersAdapter("app:token", "trader")
    a.connected = True
    a.sent = []
    a.ws_client = types.SimpleNamespace(
        subscribe_symbols=lambda tokens, mappings=None: a.sent.append(list(tokens))
    )

    def convert(symbols, data_type):
        token = DP if data_type == "DepthUpdate" else SF
        return [token], {token: "NSE:SBIN-EQ"}, []

    monkeypatch.setattr(a.token_converter, "convert_openalgo_symbols_to_hsm", convert)
    monkeypatch.setattr(fa, "get_br_symbol", lambda symbol, exchange: "NSE:SBIN-EQ")
    # The adapter drops a second message for a symbol within the same second;
    # each packet here lands in a second of its own.
    clock = itertools.count(1_000_000, 2)
    monkeypatch.setattr(fa.time, "time", lambda: next(clock))
    return a


def take_depth(adapter):
    got = []
    assert adapter.subscribe_symbols(
        [{"exchange": "NSE", "symbol": "SBIN"}], "DepthUpdate", got.append
    )
    return got


def test_PORTED_DEFECT_a_depth_message_carries_the_traded_price_not_the_midpoint(adapter):
    got = take_depth(adapter)
    adapter._on_message(sf_packet(ltp=95285, volume=6_680_063))
    adapter._on_message(dp_packet(bid=95275, ask=95290))
    (depth,) = got
    assert depth["ltp"] == 952.85, "the midpoint 952.825 is not a price anything traded at"
    assert (depth["volume"], depth["last_trade_quantity"]) == (6_680_063, 25)
    assert (depth["open"], depth["high"], depth["low"], depth["close"]) == (
        948.0,
        956.0,
        947.0,
        945.0,
    )
    assert (
        depth["depth"]["buy"][0]["price"] == 952.75 and depth["depth"]["sell"][0]["price"] == 952.9
    )


def test_a_depth_message_before_any_trade_has_no_price(adapter):
    got = take_depth(adapter)
    adapter._on_message(dp_packet(bid=95275, ask=95290))
    (depth,) = got
    assert "ltp" not in depth and "volume" not in depth
    assert depth["depth"]["buy"][0]["price"] == 952.75


def test_a_depth_subscription_also_takes_the_quote_feed(adapter):
    got = take_depth(adapter)
    assert adapter.sent == [[DP], [SF]]
    assert adapter.hsm_to_symbol[SF] == "NSE:SBIN"
    adapter._on_message(sf_packet(ltp=95285, volume=1))
    assert got == [], "nobody asked for quotes, so a quote is kept, not sent"


def test_the_depth_mapping_never_makes_up_a_price():
    mapped = FyersDataMapper().map_to_openalgo_depth(dp_packet(bid=95275, ask=95290))
    assert "ltp" not in mapped

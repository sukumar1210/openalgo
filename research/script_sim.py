"""
The OpenAlgo API a replayed Python strategy talks to, answered from stored bars.

A script hosted at /python reaches OpenAlgo over HTTP (the ``openalgo`` SDK, or
any client) and, in analyzer mode, trades against the sandbox. A backtest runs
the same file unchanged, so it needs the same API: this module answers it from
the scenario's frozen 1-minute snapshot, at the replay's virtual time, the way
the sandbox would have answered at that moment.

What a script sees at time ``t``
--------------------------------

Only what had happened by ``t``. The last traded price is the open of the
minute bar that is forming at ``t`` (its first trade has happened), or the
close of the last bar before it. History returns the bars that have closed,
plus the forming bar as a flat bar at its open, as a broker returns the
current candle; longer intervals are built from the minute bars, each day's
buckets starting at its first bar, as a broker's do.

How orders fill
---------------

As the sandbox fills them (``sandbox/execution_engine.py``): a market order at
the last traded price the moment it is placed; a limit order at once if it is
marketable, otherwise on the first later bar that trades through its price; a
stop order when a later bar touches its trigger, at the trigger or at a worse
open. Intraday (MIS) positions are squared off at the exchange's square-off
time with the sandbox's own defaults, and MIS orders after it are refused; open
orders expire at the day's close. A smart order moves the position to its
``position_size`` with the sandbox's own rules. An order that would take gross
exposure past the configuration's buying power (capital x allocation x
leverage) is refused for insufficient funds.

Costs are not charged here. Every fill is handed to ``research.fills``, which
charges the scenario's cost model exactly as every other run does.

Anything else the API offers (option chains, baskets, alerts, Telegram) is
answered with a refusal that names the endpoint, and the replay counts how
often each was asked for, so a result says what its script could not do.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from research.strategy import IST_OFFSET_SECONDS, ist_day, ist_minute

#: The sandbox's default MIS square-off times (sandbox/squareoff_manager.py).
SQUARE_OFF = {
    "NSE": "15:15",
    "BSE": "15:15",
    "NFO": "15:15",
    "BFO": "15:15",
    "CDS": "16:45",
    "BCD": "16:45",
    "MCX": "23:30",
    "NCDEX": "17:00",
}
#: When each exchange's trading day ends; open orders expire then.
MARKET_CLOSE = {
    "NSE": "15:30",
    "BSE": "15:30",
    "NFO": "15:30",
    "BFO": "15:30",
    "CDS": "17:00",
    "BCD": "17:00",
    "MCX": "23:55",
    "NCDEX": "17:00",
}
PRICE_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
PRODUCTS = ("MIS", "CNC", "NRML")
_INTERVAL_RE = re.compile(r"^(\d+)(m|h)$")


def at_time(day: int, hhmm: str) -> int:
    """Epoch seconds of an IST wall-clock time on an IST day number."""
    hours, minutes = (int(x) for x in hhmm.split(":"))
    return day * 86_400 - IST_OFFSET_SECONDS + hours * 3600 + minutes * 60


def _stamp(t: float) -> str:
    """A time as the sandbox prints it: IST wall clock."""
    return (datetime.fromtimestamp(int(t), UTC) + timedelta(seconds=IST_OFFSET_SECONDS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _day_of(text: str) -> int:
    """An IST day number from YYYY-MM-DD."""
    return (date.fromisoformat(str(text)[:10]) - date(1970, 1, 1)).days


class Refused(ValueError):
    """A request the sandbox would refuse, with its message."""


class Market:
    """The snapshot's 1-minute bars, by symbol, as arrays."""

    def __init__(self, frames) -> None:
        self.bars = {
            symbol: {
                name: frame[name].to_numpy("int64" if name == "timestamp" else "float64")
                for name in ("timestamp", "open", "high", "low", "close", "volume")
            }
            for symbol, frame in frames.items()
        }

    def has(self, symbol: str) -> bool:
        return symbol in self.bars

    def _index(self, symbol: str, t: float) -> int:
        """The last bar that opened at or before ``t``, or -1."""
        return int(np.searchsorted(self.bars[symbol]["timestamp"], t, "right")) - 1

    def ltp(self, symbol: str, t: float) -> float | None:
        if not self.has(symbol):
            return None
        i = self._index(symbol, t)
        if i < 0:
            return None
        b = self.bars[symbol]
        return float(b["open"][i] if t < b["timestamp"][i] + 60 else b["close"][i])

    def known(self, symbol: str, t: float, since: float = 0) -> pd.DataFrame:
        """Bars known at ``t`` from ``since``: closed ones, then the forming one flat at its open."""
        b = self.bars[symbol]
        start = int(np.searchsorted(b["timestamp"], since, "left"))
        end = max(self._index(symbol, t) + 1, start)
        frame = pd.DataFrame({name: b[name][start:end] for name in b})
        if end > start and t < b["timestamp"][end - 1] + 60:
            last = end - start - 1
            opened = frame.at[last, "open"]
            frame.loc[last, ["high", "low", "close", "volume"]] = [opened, opened, opened, 0.0]
        return frame

    def history(self, symbol: str, interval: str, start: str, end: str, t: float) -> list[dict]:
        frame = self.known(symbol, t, since=at_time(_day_of(start), "00:00"))
        days = (frame["timestamp"] + IST_OFFSET_SECONDS) // 86_400
        frame = frame[days <= _day_of(end)]
        if frame.empty:
            return []
        days = (frame["timestamp"] + IST_OFFSET_SECONDS) // 86_400
        if interval == "D":
            bucket = days * 86_400 - IST_OFFSET_SECONDS
        else:
            match = _INTERVAL_RE.match(interval)
            if not match:
                raise Refused(f"The {interval} interval is not available in a backtest.")
            size = int(match.group(1)) * (60 if match.group(2) == "m" else 3600)
            first = frame.groupby(days)["timestamp"].transform("min")
            bucket = first + (frame["timestamp"] - first) // size * size
        out = frame.groupby(bucket.to_numpy()).agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        return [
            {
                "timestamp": int(ts),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
                "oi": 0,
            }
            for ts, row in out.iterrows()
        ]

    def quote(self, symbol: str, t: float) -> dict:
        today = ist_day(int(t))
        # Today and the days before it that a previous close can come from.
        frame = self.known(symbol, t, since=at_time(today - 10, "00:00"))
        days = (frame["timestamp"] + IST_OFFSET_SECONDS) // 86_400
        session = frame[days == today]
        before = frame[days < today]
        ltp = self.ltp(symbol, t) or 0.0
        return {
            "ltp": ltp,
            "open": float(session["open"].iloc[0]) if len(session) else 0.0,
            "high": float(session["high"].max()) if len(session) else 0.0,
            "low": float(session["low"].min()) if len(session) else 0.0,
            "prev_close": float(before["close"].iloc[-1]) if len(before) else 0.0,
            "volume": float(session["volume"].sum()) if len(session) else 0.0,
            "bid": ltp,
            "ask": ltp,
            "oi": 0,
        }

    def closed_bars(self, symbol: str, after: float, upto: float):
        """Bars that opened at or after ``after`` and had closed by ``upto``, in order."""
        b = self.bars[symbol]
        lo = int(np.searchsorted(b["timestamp"], after, "left"))
        hi = int(np.searchsorted(b["timestamp"], upto - 60, "right"))
        for i in range(lo, hi):
            yield int(b["timestamp"][i]), b["open"][i], b["high"][i], b["low"][i]


def new_state(capital: float) -> dict:
    """A replay's state before its first day. It is carried from day to day as JSON."""
    return {
        "capital": float(capital),
        "realized": 0.0,
        "positions": {},
        "orders": [],
        "trades": [],
        "fills": [],
        "next_id": 1,
        "squared_off": [],
        "unsupported": {},
        "stopped_early": [],
    }


class Simulator:
    """The sandbox, for one replay: positions, orders and fills over the snapshot."""

    def __init__(self, market: Market, state: dict, buying_power: float, exchange: str) -> None:
        self.market = market
        self.state = state
        self.buying_power = float(buying_power)
        self.exchange = exchange
        self.now = 0.0

    # ------------------------------------------------------------- the day

    def start_day(self, t: float) -> None:
        """A new trading day: yesterday's orders and trades leave the books."""
        self.state["orders"] = []
        self.state["trades"] = []
        self.now = float(t)

    def end_day(self, day: int) -> None:
        """Run the market to the close: fills, the square-off, then every open order expires."""
        close = max(at_time(day, MARKET_CLOSE.get(x, "15:30")) for x in self._exchanges())
        self.advance(close)
        for order in self._open_orders():
            self._finish(order, "cancelled", "Expired at the end of the trading day")

    def _exchanges(self) -> set[str]:
        held = {p["exchange"] for p in self.state["positions"].values()}
        return held | {o["exchange"] for o in self._open_orders()} | {self.exchange}

    # ------------------------------------------------------------- time

    def advance(self, t: float) -> None:
        """Let the market run to ``t``: pending orders fill, then MIS squares off."""
        if t <= self.now:
            return
        day = ist_day(int(self.now))
        for exchange in sorted(self._exchanges()):
            square_off = at_time(day, SQUARE_OFF.get(exchange, "15:15"))
            key = f"{day}:{exchange}"
            if self.now <= square_off <= t and key not in self.state["squared_off"]:
                self._fill_pending(square_off, exchange)
                self._square_off(exchange, square_off)
                self.state["squared_off"].append(key)
        self._fill_pending(t)
        self.now = float(t)

    def _fill_pending(self, upto: float, exchange: str | None = None) -> None:
        for order in self._open_orders():
            if exchange is not None and order["exchange"] != exchange:
                continue
            for ts, opened, high, low in self.market.closed_bars(
                order["symbol"], order["_scan_from"], upto
            ):
                order["_scan_from"] = ts + 60
                price = self._touched(order, opened, high, low)
                if price is not None:
                    self._execute(order, price, ts)
                    break

    @staticmethod
    def _touched(order: dict, opened: float, high: float, low: float) -> float | None:
        """The price a resting order fills at on this bar, or None."""
        buy = order["action"] == "BUY"
        kind, price, trigger = order["pricetype"], order["price"], order["trigger_price"]
        if kind in ("SL", "SL-M") and order["order_status"] == "trigger pending":
            if not (high >= trigger if buy else low <= trigger):
                return None
            hit = max(opened, trigger) if buy else min(opened, trigger)
            if kind == "SL-M":
                return hit
            # A stop-limit becomes a limit at its price once triggered.
            order["order_status"] = "open"
            if (hit <= price) if buy else (hit >= price):
                return hit
            return None
        if buy and low <= price:
            return min(opened, price)
        if not buy and high >= price:
            return max(opened, price)
        return None

    def _square_off(self, exchange: str, t: int) -> None:
        for order in self._open_orders():
            if order["product"] == "MIS" and order["exchange"] == exchange:
                self._finish(order, "cancelled", "Cancelled at the intraday square-off time")
        for _key, position in sorted(self.state["positions"].items()):
            if position["product"] != "MIS" or position["exchange"] != exchange:
                continue
            if not position["qty"]:
                continue
            price = self.market.ltp(position["symbol"], t)
            if price is None:
                continue
            order = self._new_order(
                {
                    "symbol": position["symbol"],
                    "exchange": exchange,
                    "action": "SELL" if position["qty"] > 0 else "BUY",
                    "quantity": abs(position["qty"]),
                    "pricetype": "MARKET",
                    "product": "MIS",
                    "strategy": "AUTO_SQUARE_OFF",
                },
                t,
            )
            self._execute(order, price, t, reason="square_off")

    # ------------------------------------------------------------- orders

    def need(self, symbol: str) -> None:
        """Refuse a symbol the snapshot does not hold, noting it so the replay can fetch it.

        The script decides which symbols it trades, so the replay learns them by
        watching what it asks for; ``research.script_replay`` loads them and
        runs the backtest again.
        """
        if self.market.has(symbol):
            return
        asked = self.state.setdefault("asked_for", [])
        if symbol and symbol not in asked:
            asked.append(symbol)
        raise Refused(f"{symbol} has no bars in this backtest yet.")

    def _open_orders(self) -> list[dict]:
        return [o for o in self.state["orders"] if o["order_status"] in ("open", "trigger pending")]

    def _position(self, symbol: str, exchange: str, product: str) -> dict:
        key = f"{symbol}|{exchange}|{product}"
        return self.state["positions"].setdefault(
            key, {"symbol": symbol, "exchange": exchange, "product": product, "qty": 0, "avg": 0.0}
        )

    def _held(self, symbol: str, exchange: str, product: str) -> int:
        position = self.state["positions"].get(f"{symbol}|{exchange}|{product}")
        return position["qty"] if position else 0

    def _gross(self, t: float, extra: dict[str, int] | None = None) -> float:
        held: dict[str, int] = {}
        for p in self.state["positions"].values():
            held[p["symbol"]] = held.get(p["symbol"], 0) + p["qty"]
        for symbol, qty in (extra or {}).items():
            held[symbol] = held.get(symbol, 0) + qty
        return sum(abs(q) * (self.market.ltp(s, t) or 0.0) for s, q in held.items())

    def _new_order(self, data: dict, t: float) -> dict:
        number = self.state["next_id"]
        self.state["next_id"] += 1
        order = {
            "orderid": f"BT{number:08d}",
            "symbol": data["symbol"],
            "exchange": data["exchange"],
            "action": data["action"],
            "quantity": int(data["quantity"]),
            "price": float(data.get("price") or 0),
            "trigger_price": float(data.get("trigger_price") or 0),
            "pricetype": data["pricetype"],
            "product": data["product"],
            "order_status": "open",
            "average_price": 0.0,
            "filled_quantity": 0,
            "pending_quantity": int(data["quantity"]),
            "rejection_reason": "",
            "timestamp": _stamp(t),
            "strategy": str(data.get("strategy") or ""),
            "_scan_from": t,
        }
        self.state["orders"].append(order)
        return order

    def _finish(self, order: dict, status: str, reason: str = "") -> None:
        order["order_status"] = status
        order["rejection_reason"] = reason

    def _execute(self, order: dict, price: float, t: float, reason: str = "signal") -> None:
        qty = order["quantity"] if order["action"] == "BUY" else -order["quantity"]
        position = self._position(order["symbol"], order["exchange"], order["product"])
        before = position["qty"]
        after = before + qty
        if before and (before > 0) != (qty > 0):
            closed = min(abs(qty), abs(before))
            self.state["realized"] += closed * (price - position["avg"]) * (1 if before > 0 else -1)
        if after == 0:
            position["avg"] = 0.0
        elif not before or (before > 0) != (after > 0):
            position["avg"] = price
        elif abs(after) > abs(before):
            position["avg"] = (position["avg"] * abs(before) + price * abs(qty)) / abs(after)
        position["qty"] = after
        order.update(
            order_status="complete",
            average_price=price,
            filled_quantity=order["quantity"],
            pending_quantity=0,
        )
        self.state["fills"].append([int(t), order["symbol"], qty, float(price), reason])
        self.state["trades"].append(
            {
                "tradeid": f"T{order['orderid']}",
                "orderid": order["orderid"],
                "symbol": order["symbol"],
                "exchange": order["exchange"],
                "action": order["action"],
                "quantity": order["quantity"],
                "average_price": round(price, 2),
                "price": round(price, 2),
                "trade_value": round(price * order["quantity"], 2),
                "product": order["product"],
                "strategy": order["strategy"],
                "timestamp": _stamp(t),
            }
        )

    def place(self, data: dict, t: float) -> dict:
        """Place one order as the sandbox would. Raises Refused with its reason."""
        symbol = str(data.get("symbol") or "").upper()
        exchange = str(data.get("exchange") or self.exchange).upper()
        action = str(data.get("action") or "").upper()
        kind = str(data.get("pricetype") or data.get("price_type") or "MARKET").upper()
        product = str(data.get("product") or "MIS").upper()
        try:
            quantity = int(float(data.get("quantity") or 0))
            price = float(data.get("price") or 0)
            trigger = float(data.get("trigger_price") or 0)
        except (TypeError, ValueError) as exc:
            raise Refused("Quantity and prices must be numbers.") from exc
        if action not in ("BUY", "SELL"):
            raise Refused("Action must be BUY or SELL.")
        if kind not in PRICE_TYPES:
            raise Refused("Price type must be MARKET, LIMIT, SL or SL-M.")
        if product not in PRODUCTS:
            raise Refused("Product must be MIS, CNC or NRML.")
        if quantity <= 0:
            raise Refused("Quantity must be a positive whole number.")
        if kind in ("LIMIT", "SL") and price <= 0:
            raise Refused("A LIMIT or SL order needs a price.")
        if kind in ("SL", "SL-M") and trigger <= 0:
            raise Refused("A stop order needs a trigger price.")
        self.need(symbol)
        ltp = self.market.ltp(symbol, t)
        if ltp is None:
            raise Refused(f"{symbol} has no price yet at this time.")
        day = ist_day(int(t))
        if product == "MIS" and t >= at_time(day, SQUARE_OFF.get(exchange, "15:15")):
            raise Refused("MIS orders are not accepted after the intraday square-off time.")
        signed = quantity if action == "BUY" else -quantity
        held = sum(p["qty"] for p in self.state["positions"].values() if p["symbol"] == symbol)
        if abs(held + signed) > abs(held) and (
            self._gross(t, {symbol: signed}) > self.buying_power + 1e-6
        ):
            raise Refused("Insufficient funds for this order.")
        order = self._new_order(
            {
                "symbol": symbol,
                "exchange": exchange,
                "action": action,
                "quantity": quantity,
                "price": price,
                "trigger_price": trigger,
                "pricetype": kind,
                "product": product,
                "strategy": data.get("strategy"),
            },
            t,
        )
        if kind == "MARKET":
            self._execute(order, ltp, t)
        elif kind == "LIMIT" and ((ltp <= price) if action == "BUY" else (ltp >= price)):
            self._execute(order, ltp, t)
        elif kind in ("SL", "SL-M"):
            order["order_status"] = "trigger pending"
        # A resting order fills only on bars that open after it was placed.
        order["_scan_from"] = (int(t) // 60 + 1) * 60
        return order

    # ------------------------------------------------------------- the API

    def handle(self, endpoint: str, body: dict, t: float) -> tuple[int, dict]:
        """Answer one API call at time ``t``, as ``(http status, json body)``."""
        self.advance(t)
        method = getattr(self, "api_" + endpoint.strip("/").replace("/", "_"), None)
        if method is None:
            counts = self.state["unsupported"]
            counts[endpoint] = counts.get(endpoint, 0) + 1
            return 404, {
                "status": "error",
                "message": f"{endpoint} is not available in a backtest.",
                "mode": "analyze",
            }
        try:
            return 200, {**method(body or {}, t), "mode": "analyze"}
        except Refused as exc:
            return 400, {"status": "error", "message": str(exc), "mode": "analyze"}

    def api_ping(self, body, t):
        return {"status": "success", "data": {"broker": "backtest", "message": "pong"}}

    def api_analyzer(self, body, t):
        return {"status": "success", "data": {"mode": "analyze", "analyze_mode": True}}

    def api_analyzer_toggle(self, body, t):
        if body.get("mode") is False:
            raise Refused("A backtest always runs in analyzer mode.")
        return self.api_analyzer(body, t)

    def api_intervals(self, body, t):
        return {
            "status": "success",
            "data": {
                "seconds": [],
                "minutes": ["1m", "3m", "5m", "10m", "15m", "30m"],
                "hours": ["1h"],
                "days": ["D"],
                "weeks": [],
                "months": [],
            },
        }

    def api_history(self, body, t):
        symbol = str(body.get("symbol") or "").upper()
        self.need(symbol)
        data = self.market.history(
            symbol,
            str(body.get("interval") or "1m"),
            str(body.get("start_date") or "1970-01-01"),
            str(body.get("end_date") or "2999-12-31"),
            t,
        )
        return {"status": "success", "data": data}

    def api_quotes(self, body, t):
        symbol = str(body.get("symbol") or "").upper()
        self.need(symbol)
        return {"status": "success", "data": self.market.quote(symbol, t)}

    def api_multiquotes(self, body, t):
        results = []
        for item in body.get("symbols") or []:
            symbol = str(item.get("symbol") or "").upper()
            exchange = str(item.get("exchange") or self.exchange).upper()
            try:
                self.need(symbol)
                results.append(
                    {"symbol": symbol, "exchange": exchange, "data": self.market.quote(symbol, t)}
                )
            except Refused:
                results.append({"symbol": symbol, "exchange": exchange, "error": "No bars"})
        return {"status": "success", "results": results}

    def api_depth(self, body, t):
        quote = self.api_quotes(body, t)["data"]
        level = [{"price": quote["ltp"], "quantity": 0}] + [{"price": 0, "quantity": 0}] * 4
        return {"status": "success", "data": {**quote, "bids": level, "asks": level}}

    def api_funds(self, body, t):
        unrealized = 0.0
        for p in self.state["positions"].values():
            if p["qty"]:
                ltp = self.market.ltp(p["symbol"], t) or p["avg"]
                unrealized += p["qty"] * (ltp - p["avg"])
        used = self._gross(t)
        return {
            "status": "success",
            "data": {
                "availablecash": round(self.state["capital"] + self.state["realized"] - used, 2),
                "collateral": 0.0,
                "m2mrealized": round(self.state["realized"], 2),
                "m2munrealized": round(unrealized, 2),
                "utiliseddebits": round(used, 2),
            },
        }

    def api_placeorder(self, body, t):
        order = self.place(body, t)
        return {"status": "success", "orderid": order["orderid"]}

    def api_placesmartorder(self, body, t):
        symbol = str(body.get("symbol") or "").upper()
        exchange = str(body.get("exchange") or self.exchange).upper()
        product = str(body.get("product") or "MIS").upper()
        target = int(float(body.get("position_size") or 0))
        quantity = int(float(body.get("quantity") or 0))
        current = self._held(symbol, exchange, product)
        if target == 0 and current == 0 and quantity != 0:
            action = str(body.get("action") or "").upper()
        elif target == current:
            message = (
                "No OpenPosition Found. Not placing Exit order."
                if quantity == 0
                else "Positions Already Matched. No Action needed."
            )
            return {"status": "success", "message": message}
        else:
            action = "BUY" if target > current else "SELL"
            quantity = abs(target - current)
        order = self.place({**body, "action": action, "quantity": quantity}, t)
        return {"status": "success", "orderid": order["orderid"]}

    def _order(self, orderid: str) -> dict:
        for order in self.state["orders"]:
            if order["orderid"] == orderid:
                return order
        raise Refused(f"Order {orderid} not found")

    @staticmethod
    def _public(order: dict) -> dict:
        return {k: v for k, v in order.items() if not k.startswith("_")}

    def api_orderstatus(self, body, t):
        order = self._public(self._order(str(body.get("orderid") or "")))
        order["price_type"] = order["pricetype"]
        return {"status": "success", "data": order}

    def api_orderbook(self, body, t):
        orders = [self._public(o) for o in reversed(self.state["orders"])]
        return {"status": "success", "data": {"orders": orders, "statistics": {}}}

    def api_tradebook(self, body, t):
        return {"status": "success", "data": list(reversed(self.state["trades"]))}

    def api_positionbook(self, body, t):
        data = []
        for p in self.state["positions"].values():
            ltp = self.market.ltp(p["symbol"], t) or 0.0
            data.append(
                {
                    "symbol": p["symbol"],
                    "exchange": p["exchange"],
                    "product": p["product"],
                    "quantity": p["qty"],
                    "average_price": round(p["avg"], 2) if p["qty"] else 0.0,
                    "ltp": ltp,
                    "pnl": round(p["qty"] * (ltp - p["avg"]), 2) if p["qty"] else 0.0,
                }
            )
        return {"status": "success", "data": data}

    def api_holdings(self, body, t):
        return {"status": "success", "data": {"holdings": [], "statistics": {}}}

    def api_openposition(self, body, t):
        quantity = self._held(
            str(body.get("symbol") or "").upper(),
            str(body.get("exchange") or self.exchange).upper(),
            str(body.get("product") or "MIS").upper(),
        )
        return {"status": "success", "quantity": quantity}

    def api_cancelorder(self, body, t):
        order = self._order(str(body.get("orderid") or ""))
        if order["order_status"] not in ("open", "trigger pending"):
            raise Refused(f"Order {order['orderid']} is already {order['order_status']}.")
        self._finish(order, "cancelled")
        return {"status": "success", "orderid": order["orderid"]}

    def api_cancelallorder(self, body, t):
        cancelled = [o["orderid"] for o in self._open_orders()]
        for order in self._open_orders():
            self._finish(order, "cancelled")
        return {"status": "success", "canceled_orders": cancelled, "failed_cancellations": []}

    def api_modifyorder(self, body, t):
        order = self._order(str(body.get("orderid") or ""))
        if order["order_status"] not in ("open", "trigger pending"):
            raise Refused(f"Order {order['orderid']} is already {order['order_status']}.")
        status = order["order_status"]
        self._finish(order, "cancelled")
        try:
            replaced = self.place({**self._public(order), **body}, t)
        except Refused:
            self._finish(order, status)
            raise
        # The modified order keeps its number, as a broker's does.
        self.state["orders"].remove(order)
        replaced["orderid"] = order["orderid"]
        return {"status": "success", "orderid": order["orderid"]}

    def api_closeposition(self, body, t):
        for p in list(self.state["positions"].values()):
            if p["qty"]:
                self.place(
                    {
                        "symbol": p["symbol"],
                        "exchange": p["exchange"],
                        "product": p["product"],
                        "action": "SELL" if p["qty"] > 0 else "BUY",
                        "quantity": abs(p["qty"]),
                        "pricetype": "MARKET",
                        "strategy": body.get("strategy"),
                    },
                    t,
                )
        return {"status": "success", "message": "All Open Positions Squared Off"}

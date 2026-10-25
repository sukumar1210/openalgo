"""
A Run, backtested: its accounts trading their sources' fills as the Sandbox would.

In the Sandbox (services/run_service.py) a Run trades each distinct strategy
configuration once, as a source at a reference capital, and each member's
account copies the source (sandbox/copy_engine.py). A backtest replays each
configuration once (research.script_replay) and hands the fills here, where
the Run is simulated by the Sandbox's own rules: portfolio/run_capital.py for
capital, copies and limits, portfolio/strategy_pnl.py for charges and a Shared
Run's split. Pure: plain values in, plain values out, so the Sandbox and a
backtest give the same answers from the same fills.

Each trading day:

* **The open.** Each account's capital and transfer by the P&L policy, from
  what it closed the last day with; its exposure limit; its daily loss limit.
* **A source fill.** The source's position moves, then each member following
  it gets one market order for the difference between what it should hold
  (``copy_target``: scaled, rounded toward zero, within the limit at cost)
  and what it holds, at the source's price, charged as the Sandbox charges a
  fill. A Shared Run's members net in one account, each reading its own
  position from the fills carrying its order name and its share of a
  square-off (``attribute``). The source's own square-off is not copied.
* **The daily loss limit.** Checked after every fill, and every minute while
  the account holds positions when ``ltp`` is given: past it, after charges,
  the account is squared off at the marks and opens nothing more that day.
* **The square-off** at 15:15 (NSE and BSE, the exchanges a Run takes):
  every open position at the last price, charged.

Not modelled: margin (the exposure limit keeps a copy within its capital),
resting orders (a copy is a market order), and copies landing a moment after
the source's fill (here they land at its price, as the Sandbox prices them).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from portfolio import run_capital as rc
from portfolio.strategy_pnl import SQUARE_OFF_TAG, attribute, daily_results, fill_charges

IST_OFFSET = 19_800
#: NSE's and BSE's intraday square-off, seconds after IST midnight.
SQUARE_OFF_AT = 15 * 3600 + 15 * 60
#: The seat of a Shared Run's one account (services/run_service.SHARED_SEAT).
SHARED_SEAT = "run"
#: How a replayed source fill names its own square-off (research.script_sim).
SOURCE_SQUARE_OFF = "square_off"
CENT = Decimal("0.01")
ZERO = Decimal("0")


@dataclass(frozen=True)
class Member:
    """One strategy of the Run, as run_service freezes it."""

    seat: str
    label: str
    source: str  # the key of the source it copies: its configuration's fingerprint
    allocation_kind: str
    allocation_value: float
    sizing: str = "platform"
    order_name: str = ""


@dataclass(frozen=True)
class RunDefinition:
    capital: float
    mode: str  # isolated or shared
    policy: str
    limit: str
    members: tuple[Member, ...]
    loss_limit_pct: float | None = None


@dataclass(frozen=True)
class Source:
    """A configuration's replay: its reference capital, exchange and fills.

    Each fill is ``(epoch seconds, symbol, signed quantity, price, reason)``.
    """

    reference_capital: float
    exchange: str
    fills: Sequence[tuple]


def _day(ts: int) -> int:
    return (int(ts) + IST_OFFSET) // 86_400


def _date(day: int) -> date:
    return date(1970, 1, 1) + timedelta(days=day)


def _stamp(ts: int) -> datetime:
    """Naive IST, as the Sandbox stamps its trades."""
    return datetime(1970, 1, 1) + timedelta(seconds=int(ts) + IST_OFFSET)


def _stored(value, places: int) -> Decimal:
    """``value`` as the Sandbox reads it back: SQLite keeps a DECIMAL as a float,
    and SQLAlchemy returns it rounded to the column's scale."""
    return Decimal(f"{float(value):.{places}f}")


@dataclass
class _Account:
    """One account's day, booked as the Sandbox books it.

    A position's average price is kept to the paise, as its column stores it,
    and a reduction realizes against that average
    (sandbox/execution_engine._update_position), so the account ends the day
    holding what a Sandbox account would, to the paise.
    """

    opening: Decimal
    transfer: Decimal
    limit: Decimal
    loss_limit: Decimal | None
    qty: dict = field(default_factory=lambda: defaultdict(int))
    avg: dict = field(default_factory=lambda: defaultdict(lambda: ZERO))
    realized: Decimal = ZERO
    charges: Decimal = ZERO
    fills: list = field(default_factory=list)
    refused: int = 0
    stopped: bool = False

    def fill(self, inst, qty: int, price, ts: int, strategy: str) -> None:
        symbol, exchange = inst
        price = Decimal(str(price))
        before = self.qty[inst]
        after = before + qty
        if before == 0:
            self.avg[inst] = _stored(price, 2)
        elif (before > 0 and after > before) or (before < 0 and after < before):
            total = self.avg[inst] * abs(before) + price * abs(qty)
            self.avg[inst] = _stored(total / abs(after), 2)
        else:  # reduced, closed or reversed: the closed part realizes
            closed = min(abs(before), abs(qty))
            self.realized += (price - self.avg[inst]) * closed * (1 if before > 0 else -1)
            if abs(qty) > abs(before):
                self.avg[inst] = _stored(price, 2)  # reversed through zero
        self.qty[inst] = after
        cost = fill_charges({"exchange": exchange, "product": "MIS", "qty": qty, "price": price})
        charge = Decimal(str(cost)).quantize(CENT) if cost else ZERO
        self.charges += charge
        self.fills.append(
            {
                "strategy": strategy,
                "symbol": symbol,
                "exchange": exchange,
                "product": "MIS",
                "action": "BUY" if qty > 0 else "SELL",
                "quantity": abs(qty),
                "price": float(price),
                "timestamp": _stamp(ts),
                "charges": float(charge),
            }
        )

    def named(self, order_name: str, inst) -> int:
        """A Shared member's position: copy_engine._named_position over today's fills."""
        symbol, exchange = inst
        rows = [f for f in self.fills if (f["symbol"], f["exchange"]) == (symbol, exchange)]
        # ponytail: re-reads the day's fills per copy, as the Sandbox does; fine at a
        # day's few hundred fills, keep running totals if a day ever holds thousands.
        return sum(int(f["qty"]) for f in attribute(rows, [order_name]))

    def exposure_elsewhere(self, inst) -> Decimal:
        """What it holds in other instruments, at cost (copy_engine._exposure_elsewhere)."""
        return sum(
            (abs(Decimal(q)) * self.avg[i] for i, q in self.qty.items() if q and i != inst), ZERO
        )

    def holds(self) -> bool:
        return any(self.qty.values())

    def pnl(self, mark: Callable) -> Decimal:
        """The day's P&L after charges, open positions at ``mark(inst)`` or at cost."""
        value = self.realized - self.charges
        for inst, q in self.qty.items():
            price = mark(inst) if q else None
            if price is not None:
                value += Decimal(q) * (Decimal(str(price)) - self.avg[inst])
        return value

    def flatten(self, mark: Callable, ts: int) -> None:
        for inst, q in sorted(self.qty.items()):
            price = mark(inst) if q else None
            if price is not None:
                self.fill(inst, -q, price, ts, SQUARE_OFF_TAG)

    def check(self, mark: Callable, ts: int) -> None:
        """The daily loss limit (squareoff_manager._check_loss_limit), at ``ts``."""
        if self.loss_limit is None:
            return
        if not self.stopped and self.pnl(mark) <= -self.loss_limit:
            self.stopped = True
        if self.stopped and self.holds():
            self.flatten(mark, ts)  # every pass: anything still open is closed

    def watching(self) -> bool:
        return self.loss_limit is not None and self.holds()


@dataclass
class _Market:
    """What one day's positions are marked at: the replay's prices, else the last fill."""

    ltp: Callable | None
    last: dict = field(default_factory=dict)

    def at(self, ts: int) -> Callable:
        def mark(inst):
            if self.ltp is not None:
                price = self.ltp(inst[0], inst[1], ts)
                if price is not None:
                    return price
            return self.last.get(inst)

        return mark


def _watch(accounts: Mapping[str, _Account], market: _Market, start: int, end: int) -> None:
    """The daily loss limit minute by minute in ``(start, end)``, while it can bite.

    The Sandbox checks every few seconds; with the replay's minute bars, every
    minute is as close as a backtest gets. Without them only fills move a mark.
    """
    if market.ltp is None:
        return
    minute = (start // 60 + 1) * 60
    while minute < end and any(a.watching() for a in accounts.values()):
        for account in accounts.values():
            if account.watching():
                account.check(market.at(minute), minute)
        minute += 60


def _trade_day(run, day, accounts, follows, events, sources, market: _Market) -> None:
    """One day's source fills, copied into the accounts, then the square-off."""
    held_by_source: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    square_off = day * 86_400 - IST_OFFSET + SQUARE_OFF_AT
    now = day * 86_400 - IST_OFFSET
    for ts, _rank, _n, key, symbol, qty, price, reason in events:
        _watch(accounts, market, now, ts)
        now = ts
        inst = (symbol, sources[key].exchange)
        held_by_source[key][inst] += qty
        market.last[inst] = price
        if reason == SOURCE_SQUARE_OFF:
            continue  # every account is squared off on its own
        for member, account, scale in follows[key]:
            held = (
                account.named(member.order_name, inst)
                if run.mode == "shared"
                else account.qty[inst]
            )
            target = rc.copy_target(
                held_by_source[key][inst],
                scale=scale,
                sizing=member.sizing,
                price=Decimal(str(price)),
                limit=account.limit,
                exposure_elsewhere=account.exposure_elsewhere(inst),
            )
            delta = target - held
            if not delta:
                continue
            if account.stopped or ts >= square_off:
                # sandbox/order_manager refuses new MIS exposure then; a stopped
                # account is flat, so nothing it is sent can reduce a position.
                account.refused += 1
                continue
            account.fill(inst, delta, price, ts, member.order_name)
        for account in accounts.values():
            account.check(market.at(ts), ts)
    _watch(accounts, market, now, square_off)
    for account in accounts.values():
        account.check(market.at(square_off), square_off)
        account.flatten(market.at(square_off), square_off)


def _seats(run: RunDefinition) -> dict[str, list[Member]]:
    if run.mode == "shared":
        return {SHARED_SEAT: list(run.members)}
    return {m.seat: [m] for m in run.members}


def _events(run: RunDefinition, sources: Mapping[str, Source]) -> dict[int, list]:
    """Every source fill by IST day, in time order; sources in the members' order."""
    order = list(dict.fromkeys(m.source for m in run.members))
    by_day: dict[int, list] = defaultdict(list)
    for rank, key in enumerate(order):
        for n, (ts, symbol, qty, price, *reason) in enumerate(sources[key].fills):
            by_day[_day(ts)].append(
                (int(ts), rank, n, key, symbol, int(qty), price, reason[0] if reason else "")
            )
    for events in by_day.values():
        events.sort()
    return by_day


def simulate(
    run: RunDefinition,
    sources: Mapping[str, Source],
    *,
    days: Iterable[date] = (),
    ltp: Callable[[str, str, int], float | None] | None = None,
) -> dict:
    """Trade ``run`` over its sources' fills, one day at a time.

    Args:
        run: The Run's definition.
        sources: Each member's source by key, with its replayed fills.
        days: Trading days to open and close even with no fills on them (a day
            with fills is always one).
        ltp: The last traded price of ``(symbol, exchange)`` at an epoch second,
            to mark positions minute by minute for the daily loss limit and to
            square off at. Without it, the last fill's price marks them.

    Returns:
        ``{"days": [...], "seats": {seat: [...]}, "members": {seat: [...]},
        "refused": n, "stopped": [...]}``: a row per day for the whole Run, per
        account and per member (``date``, ``opening``, ``transfer``, ``pnl``
        before charges, ``charges``, ``net``, ``trades``), oldest first, the
        copies refused, and each day an account reached its loss limit.
    """
    seats = _seats(run)
    share = {
        m.seat: rc.allocation(run.capital, m.allocation_kind, m.allocation_value)
        for m in run.members
    }
    allocated = {seat: sum((share[m.seat] for m in group), ZERO) for seat, group in seats.items()}
    seat_of = {m.seat: seat for seat, group in seats.items() for m in group}
    events = _events(run, sources)
    trading = sorted(set(events) | {(d - date(1970, 1, 1)).days for d in days})

    closing: dict[str, Decimal] = {}
    out = {
        "days": [],
        "seats": {s: [] for s in seats},
        "members": {m.seat: [] for m in run.members},
    }
    refused, stopped = 0, []
    for day in trading:
        opened = rc.day_open(run.policy, allocated, closing)
        accounts = {
            seat: _Account(
                opening=capital,
                transfer=transfer,
                limit=rc.limit_for(run.limit, capital, allocated[seat]),
                loss_limit=(capital * Decimal(str(run.loss_limit_pct)) / 100).quantize(CENT)
                if run.loss_limit_pct
                else None,
            )
            for seat, (capital, transfer) in opened.items()
        }
        follows: dict[str, list] = defaultdict(list)
        for m in run.members:
            seat = seat_of[m.seat]
            capital = accounts[seat].opening * share[m.seat] / allocated[seat]
            scale = _stored(capital / Decimal(str(sources[m.source].reference_capital)), 10)
            follows[m.source].append((m, accounts[seat], scale))

        _trade_day(run, day, accounts, follows, events.get(day, []), sources, _Market(ltp))

        iso = _date(day).isoformat()
        total = {"date": iso, "opening": 0.0, "transfer": 0.0, "pnl": 0.0, "charges": 0.0,
                 "net": 0.0, "trades": 0}  # fmt: skip
        for seat, account in accounts.items():
            net = account.pnl(lambda inst: None)  # flat after the square-off
            closing[seat] = account.opening + net
            row = {
                "date": iso,
                "opening": float(account.opening),
                "transfer": float(account.transfer),
                "pnl": float(net + account.charges),
                "charges": float(account.charges),
                "net": float(net),
                "trades": len(account.fills),
            }
            out["seats"][seat].append(row)
            for name in ("opening", "transfer", "pnl", "charges", "net", "trades"):
                total[name] += row[name]
            refused += account.refused
            if account.stopped:
                stopped.append({"date": iso, "seat": seat})
            for member in seats[seat]:
                out["members"][member.seat].append(
                    dict(row)
                    if run.mode != "shared"
                    else _member_day(member, account, share[member.seat] / allocated[seat], iso)
                )
        out["days"].append(total)
    return {**out, "refused": refused, "stopped": stopped}


def _member_day(member: Member, account: _Account, part: Decimal, iso: str) -> dict:
    """A Shared member's day: its own fills and its square-off share, as the P&L page splits it."""
    results = daily_results(attribute(account.fills, [member.order_name]), date.fromisoformat(iso))
    day = results[0] if results else {"pnl": 0.0, "charges": 0.0, "net": 0.0, "trades": 0}
    return {
        "date": iso,
        "opening": float(account.opening * part),
        "transfer": 0.0,
        "pnl": day["pnl"],
        "charges": day["charges"],
        "net": day["net"],
        "trades": day["trades"],
    }

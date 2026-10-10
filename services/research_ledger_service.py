"""
The research ledger as one journal, and the checks that it reconciles.

Phase 5 of the master PRD (sections 16-18, 43, 52). A run's ledger lives in
five tables that share one sequence: ``ledger_events`` (what strategies and the
platform decided, and what a venue said about each order), ``ledger_orders``,
``ledger_fills``, ``ledger_trades`` and ``ledger_positions``. Each fact is
written once. The journal presents them as the PRD's event list, deriving the
events whose facts already live in a typed table instead of copying them:

* ``FILL``, ``FEE``, ``SLIPPAGE`` and ``POSITION_UPDATE`` from each fill
* ``REALIZED_PNL`` from each closed trade
* ``UNREALIZED_PNL`` from each position snapshot
* in a backtest, ``ORDER_SUBMITTED`` and ``ORDER_FILLED`` from the simulated
  broker, which accepts every order and fills it as ``ledger_orders`` records

Every entry carries the PRD's field set (section 16) where it applies.

Paging is by sequence number (keyset), so a page costs the same at the end of a
large run as at its start, and filtering happens per page: a sparse filter
returns short pages and a cursor, never a scan of the whole run in one request.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterator

from sqlalchemy import case, func

from database import research_db as rdb

#: Rows read from each table per page.
JOURNAL_PAGE = 200
#: Pages read at most to fill one filtered response before handing back a cursor.
MAX_SCANS = 10
#: Money differences below this are rounding, not discrepancies.
TOLERANCE = 1e-6

EVENT_KINDS = (
    "SIGNAL",
    "TARGET_POSITION",
    "SQUARE_OFF",
    "ORDER_INTENT",
    "ORDER_SUBMITTED",
    "ORDER_FILLED",
    "ORDER_REJECTED",
    "ORDER_UNFILLED",
    "SESSION_EVENT",
    "MARK",
)
FILL_KINDS = ("FILL", "FEE", "SLIPPAGE", "POSITION_UPDATE")
KINDS = (*EVENT_KINDS, *FILL_KINDS, "REALIZED_PNL", "UNREALIZED_PNL")
TERMINAL = ("ORDER_FILLED", "ORDER_REJECTED", "ORDER_UNFILLED")

CSV_FIELDS = (
    "seq", "kind", "timestamp", "at", "session_id", "experiment_id", "scenario_id",
    "strategy_instance_id", "strategy_id", "strategy_version_id", "config_version_id",
    "signal_id", "platform_order_id", "execution_order_id", "fill_id", "trade_id", "symbol",
    "side", "quantity", "requested_price", "fill_price", "fees", "slippage", "gross_pnl",
    "net_pnl", "detail",
)  # fmt: skip


class LedgerError(ValueError):
    """The request cannot be answered. The message is written for the trader."""


class RunNotFound(LedgerError):
    pass


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #


class _Run:
    """What every entry of one run shares, read once per request."""

    def __init__(self, session, run_id: int) -> None:
        run = session.get(rdb.ResearchRun, run_id)
        if run is None:
            raise RunNotFound("That run does not exist.")
        scenario = session.get(rdb.ResearchScenario, run.scenario_id)
        self.run = run
        self.backtest = run.environment == rdb.BACKTEST
        self.base = {
            "session_id": run.id,
            "experiment_id": scenario.experiment_id,
            "scenario_id": scenario.id,
        }
        self.instances: dict[int, dict] = {}
        for inst in session.query(rdb.ResearchScenarioInstance).filter_by(scenario_id=scenario.id):
            version = session.get(rdb.ResearchStrategyVersion, inst.strategy_version_id)
            self.instances[inst.position] = {
                "strategy_id": version.strategy_id,
                "strategy_version_id": version.id,
                "config_version_id": inst.config_version_id,
            }

    def entry(self, seq: int, kind: str, timestamp, instance=None, **fields) -> dict:
        out = dict.fromkeys(CSV_FIELDS)
        out.update(self.base)
        out.update(seq=seq, kind=kind, timestamp=timestamp, strategy_instance_id=instance)
        if instance is not None:
            out.update(self.instances.get(instance, {}))
        out.update(fields)
        return out

    def order_id(self, order_seq) -> str | None:
        return None if order_seq is None else f"research:{self.run.id}:{order_seq}"


def _side(qty) -> str | None:
    if not qty:
        return None
    return "BUY" if qty > 0 else "SELL"


def _fetch(session, model, run_id: int, cursor: int, limit: int):
    return (
        session.query(model)
        .filter(model.run_id == run_id, model.seq > cursor)
        .order_by(model.seq)
        .limit(limit)
        .all()
    )


def journal(
    run_id: int,
    cursor: int = 0,
    kinds: set[str] | None = None,
    instance: int | None = None,
    symbol: str | None = None,
    limit: int = JOURNAL_PAGE,
) -> dict:
    """One page of a run's ledger as the PRD's events, oldest first.

    Args:
        cursor: Return entries after this sequence number; 0 for the start.
        kinds: Only these kinds (``KINDS``); all when empty.
        instance: Only this strategy instance's own entries. Orders belong to a
            portfolio rather than one strategy, so they are left out.
        symbol: Only entries about this symbol.

    Returns:
        ``{"items": [...], "next_cursor": int | None}``; a None cursor means the
        ledger has nothing further for this filter.
    """
    unknown = sorted(set(kinds or ()) - set(KINDS))
    if unknown:
        raise LedgerError(f"Unknown ledger entries: {', '.join(unknown)}.")
    session = rdb.db_session
    ctx = _Run(session, run_id)
    wanted = set(kinds or KINDS)
    symbol = symbol.strip().upper() if symbol else None
    items: list[dict] = []
    for _scan in range(MAX_SCANS):
        page, cursor, more = _page(session, ctx, cursor, wanted, limit)
        items.extend(
            e
            for e in page
            if e["kind"] in wanted
            and (instance is None or e["strategy_instance_id"] == instance)
            and (symbol is None or _about(e, symbol))
        )
        if not more or len(items) >= limit:
            return {"items": items, "next_cursor": cursor if more else None}
    return {"items": items, "next_cursor": cursor}


def _about(entry: dict, symbol: str) -> bool:
    if entry["symbol"] == symbol:
        return True
    if entry["kind"] == "SIGNAL" and entry["detail"]:
        return symbol in {part.split(" ")[0] for part in entry["detail"].split(", ")}
    return False


def _page(session, ctx: _Run, cursor: int, wanted: set[str], limit: int):
    """Entries for the next ``limit`` sequence numbers after ``cursor``."""
    run_id = ctx.run.id
    sources = []
    if wanted & set(EVENT_KINDS) or (ctx.backtest and wanted & {"ORDER_SUBMITTED"}):
        sources.append(("event", _fetch(session, rdb.LedgerEvent, run_id, cursor, limit)))
    if ctx.backtest and "ORDER_FILLED" in wanted:
        sources.append(("order", _fetch(session, rdb.LedgerOrder, run_id, cursor, limit)))
    if wanted & set(FILL_KINDS):
        sources.append(("fill", _fetch(session, rdb.LedgerFill, run_id, cursor, limit)))
    if "REALIZED_PNL" in wanted:
        sources.append(("trade", _fetch(session, rdb.LedgerTrade, run_id, cursor, limit)))
    if "UNREALIZED_PNL" in wanted:
        sources.append(("position", _fetch(session, rdb.LedgerPosition, run_id, cursor, limit)))
    # Each source gave its first ``limit`` rows after the cursor, so every row up
    # to the smallest last-seq of a full source is present: that is the page.
    full = [rows[-1].seq for _, rows in sources if len(rows) == limit]
    edge = min(full) if full else None
    rows = sorted(
        ((name, row) for name, rows in sources for row in rows if edge is None or row.seq <= edge),
        key=lambda pair: pair[1].seq,
    )
    if not rows:
        return [], cursor, False

    order_seqs = {r.order_seq for n, r in rows if n == "fill" and r.order_seq is not None}
    order_seqs |= {r.seq for n, r in rows if n == "order"}
    order_ids = [ctx.order_id(s) for s in order_seqs]
    order_ids += [r.platform_order_id for n, r in rows if n == "event" and r.platform_order_id]
    venue, requested = _order_facts(session, run_id, order_ids)
    held = _positions_before(session, run_id, rows[0][1].seq) if "fill" in dict(rows) else {}

    out: list[dict] = []
    for name, row in rows:
        out.extend(_entries(ctx, name, row, venue, requested, held))
    return out, rows[-1][1].seq, edge is not None


def _order_facts(session, run_id: int, order_ids: list[str]) -> tuple[dict, dict]:
    """Venue order ids and requested prices for the platform orders on a page."""
    venue: dict[str, str] = {}
    requested: dict[str, float] = {}
    if not order_ids:
        return venue, requested
    for event in session.query(rdb.LedgerEvent).filter(
        rdb.LedgerEvent.run_id == run_id,
        rdb.LedgerEvent.platform_order_id.in_(set(order_ids)),
        rdb.LedgerEvent.kind.in_(("ORDER_INTENT", "ORDER_SUBMITTED", "ORDER_FILLED")),
    ):
        payload = event.payload or {}
        if event.kind == "ORDER_INTENT":
            requested[event.platform_order_id] = payload.get("reference_price")
        elif payload.get("orderid"):
            venue[event.platform_order_id] = str(payload["orderid"])
    return venue, requested


def _positions_before(session, run_id: int, seq: int) -> dict[tuple[int, str], int]:
    """Each book's position on each symbol just before ``seq``, from the fills."""
    rows = (
        session.query(rdb.LedgerFill.instance, rdb.LedgerFill.symbol, func.sum(rdb.LedgerFill.qty))
        .filter(rdb.LedgerFill.run_id == run_id, rdb.LedgerFill.seq < seq)
        .group_by(rdb.LedgerFill.instance, rdb.LedgerFill.symbol)
    )
    return {(instance, symbol): int(qty or 0) for instance, symbol, qty in rows}


def _entries(ctx: _Run, name: str, row, venue: dict, requested: dict, held: dict) -> list[dict]:
    if name == "event":
        return _event_entries(ctx, row, venue)
    if name == "order":
        order_id = ctx.order_id(row.seq)
        return [
            ctx.entry(
                row.seq, "ORDER_FILLED", row.ts, platform_order_id=order_id,
                execution_order_id="simulated", symbol=row.symbol, side=_side(row.qty),
                quantity=abs(row.qty), requested_price=requested.get(order_id),
                fill_price=row.price, fees=row.fees, slippage=row.slippage,
                detail=f"net order for portfolio {row.portfolio}",
            )
        ]  # fmt: skip
    if name == "fill":
        order_id = ctx.order_id(row.order_seq)
        common = {
            "platform_order_id": order_id,
            "execution_order_id": "simulated" if ctx.backtest and order_id else venue.get(order_id),
            "fill_id": row.seq,
            "symbol": row.symbol,
            "side": _side(row.qty),
            "quantity": abs(row.qty),
            "requested_price": requested.get(order_id),
            "fill_price": row.price,
        }
        detail = (
            row.reason
            if row.order_seq is not None
            else f"{row.reason}, crossed inside the portfolio"
        )
        out = [ctx.entry(row.seq, "FILL", row.ts, row.instance, fees=row.fees,
                         slippage=row.slippage, detail=detail, **common)]  # fmt: skip
        if row.fees:
            out.append(ctx.entry(row.seq, "FEE", row.ts, row.instance, fees=row.fees, **common))
        if row.slippage:
            out.append(
                ctx.entry(
                    row.seq, "SLIPPAGE", row.ts, row.instance, slippage=row.slippage, **common
                )
            )
        key = (row.instance, row.symbol)
        held[key] = held.get(key, 0) + row.qty
        out.append(
            ctx.entry(row.seq, "POSITION_UPDATE", row.ts, row.instance, fill_id=row.seq,
                      symbol=row.symbol, quantity=held[key], detail="position after this fill")
        )  # fmt: skip
        return out
    if name == "trade":
        return [
            ctx.entry(
                row.seq, "REALIZED_PNL", row.exit_ts, row.instance, trade_id=row.seq,
                symbol=row.symbol, side="BUY" if row.side == "LONG" else "SELL",
                quantity=row.qty, fill_price=row.exit_price, fees=row.fees,
                slippage=row.slippage, gross_pnl=row.gross_pnl, net_pnl=row.net_pnl,
                detail=f"{row.side.lower()} from {row.entry_price:.2f} to {row.exit_price:.2f}",
            )
        ]  # fmt: skip
    return [
        ctx.entry(
            row.seq, "UNREALIZED_PNL", row.ts, row.instance, symbol=row.symbol,
            side=_side(row.qty), quantity=abs(row.qty), fill_price=row.avg_price,
            gross_pnl=row.unrealized_pnl, net_pnl=row.unrealized_pnl,
            detail=f"held at {row.avg_price:.2f}, marked at {row.mark:.2f}",
        )
    ]  # fmt: skip


def _event_entries(ctx: _Run, row, venue: dict) -> list[dict]:
    payload = row.payload or {}
    at = row.at.isoformat() if row.at else None
    common = {"at": at, "platform_order_id": row.platform_order_id}
    kind = row.kind
    if kind == "SIGNAL":
        weights = payload.get("weights") or {}
        detail = ", ".join(f"{s} {w:+.4f}" if w is not None else f"{s} invalid"
                           for s, w in weights.items()) or "flat"  # fmt: skip
        return [ctx.entry(row.seq, kind, row.bar_ts, row.instance, signal_id=row.seq,
                          detail=detail, **common)]  # fmt: skip
    if kind in ("TARGET_POSITION", "SQUARE_OFF"):
        change = payload.get("target", 0) - payload.get("previous", 0)
        detail = f"{payload.get('previous')} to {payload.get('target')} ({payload.get('reason')})"
        if payload.get("requested") is not None:
            detail += f", reduced from {payload['requested']} by the exposure limit"
        return [
            ctx.entry(row.seq, kind, row.bar_ts, row.instance, signal_id=payload.get("signal"),
                      symbol=payload.get("symbol"), side=_side(change), quantity=abs(change),
                      detail=detail, **common)
        ]  # fmt: skip
    if kind == "ORDER_INTENT":
        net = payload.get("net", 0)
        entries = [
            ctx.entry(row.seq, kind, row.bar_ts, symbol=payload.get("symbol"), side=_side(net),
                      quantity=abs(net), requested_price=payload.get("reference_price"),
                      detail=f"portfolio {payload.get('portfolio')}", **common)
        ]  # fmt: skip
        if ctx.backtest:
            entries.append(
                ctx.entry(row.seq, "ORDER_SUBMITTED", row.bar_ts, symbol=payload.get("symbol"),
                          side=_side(net), quantity=abs(net), execution_order_id="simulated",
                          detail="accepted by the simulated broker", **common)
            )  # fmt: skip
        return entries
    if kind in ("ORDER_SUBMITTED", "ORDER_FILLED", "ORDER_REJECTED", "ORDER_UNFILLED"):
        execution = payload.get("orderid") or venue.get(row.platform_order_id)
        reason = payload.get("reason") or payload.get("rejection_reason")
        return [
            ctx.entry(row.seq, kind, row.bar_ts, execution_order_id=execution,
                      fill_price=payload.get("price"),
                      requested_price=payload.get("reference_price"),
                      detail=reason, **common)
        ]  # fmt: skip
    detail = payload.get("event") or payload.get("message")
    if kind == "MARK":
        detail = "equity " + ", ".join(f"{v:,.2f}" for v in payload.get("books", []))
    return [ctx.entry(row.seq, kind, row.bar_ts, detail=detail, **common)]


def journal_csv(run_id: int, **filters) -> Iterator[str]:
    """The whole filtered journal as CSV, a page at a time."""
    cursor = 0
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS)
    writer.writeheader()
    while True:
        page = journal(run_id, cursor=cursor, **filters)
        for item in page["items"]:
            writer.writerow(item)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        if page["next_cursor"] is None:
            return
        cursor = page["next_cursor"]


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


def _check(name: str, problems: list[str], applies: bool = True, note: str = "") -> dict:
    if not applies:
        return {"name": name, "passed": None, "detail": note, "problems": []}
    return {
        "name": name,
        "passed": not problems,
        "detail": note if not problems else f"{len(problems)} found",
        "problems": problems[:20],
    }


def reconcile(run_id: int) -> dict:
    """Every check the PRD asks the ledger to pass (section 18), each with its findings."""
    session = rdb.db_session
    ctx = _Run(session, run_id)
    run = ctx.run
    finished = run.status in rdb.FINAL_STATUSES
    checks = [
        _check_trades(session, run_id),
        _check_contributions(session, run, finished),
        _check_portfolio(run, finished),
        _check_orders(session, ctx),
        _check_costs(session, run_id),
        _check_positions(session, run_id),
    ]
    if not ctx.backtest:
        checks.append(_check_order_states(session, run_id, finished))
    return {"checks": checks, "passed": all(c["passed"] is not False for c in checks)}


def _check_trades(session, run_id: int) -> dict:
    T = rdb.LedgerTrade
    drift = func.abs(T.net_pnl - (T.gross_pnl - T.fees - T.slippage))
    bad = session.query(T.seq).filter(T.run_id == run_id, drift > TOLERANCE).all()
    return _check(
        "Each trade's net P&L is its gross P&L less its fees and slippage",
        [f"trade {seq}" for (seq,) in bad],
    )


def _check_contributions(session, run, finished: bool) -> dict:
    name = "Each strategy's trades add up to its result"
    if not finished or not run.attribution:
        return _check(name, [], applies=False, note="Checked once the run has finished.")
    closed = dict(
        session.query(rdb.LedgerTrade.instance, func.sum(rdb.LedgerTrade.net_pnl))
        .filter(rdb.LedgerTrade.run_id == run.id)
        .group_by(rdb.LedgerTrade.instance)
        .all()
    )
    still_open = _open_positions(session, run.id)
    problems = []
    for row in run.attribution:
        position = row["instance"]
        if any(instance == position for instance, _ in still_open):
            continue
        trades = float(closed.get(position) or 0.0)
        if not math.isclose(trades, row["net_pnl"], abs_tol=1e-4):
            problems.append(f"{row['label']}: trades {trades:,.2f}, result {row['net_pnl']:,.2f}")
    note = ""
    if still_open:
        note = "Strategies still holding positions are not compared."
    return _check(name, problems, note=note)


def _check_portfolio(run, finished: bool) -> dict:
    name = "The strategies add up to the portfolio"
    if not finished or not run.attribution or not run.metrics:
        return _check(name, [], applies=False, note="Checked once the run has finished.")
    parts = sum(row["net_pnl"] for row in run.attribution)
    whole = run.metrics["net_pnl"]
    problems = [] if math.isclose(parts, whole, abs_tol=1e-4) else [
        f"strategies {parts:,.2f}, portfolio {whole:,.2f}"
    ]  # fmt: skip
    return _check(name, problems)


def _check_orders(session, ctx: _Run) -> dict:
    """Every net order equals the book fills it was netted from."""
    run_id = ctx.run.id
    portfolio_of = {
        row["instance"]: row["portfolio"] for row in (ctx.run.attribution or [])
    } or _portfolios_from_events(session, run_id)
    F = rdb.LedgerFill
    if portfolio_of:
        portfolio = case(portfolio_of, value=F.instance, else_=-1)
    else:
        portfolio = F.instance
    netted = {
        (ts, symbol, p): int(qty)
        for ts, symbol, p, qty in session.query(F.ts, F.symbol, portfolio, func.sum(F.qty))
        .filter(F.run_id == run_id)
        .group_by(F.ts, F.symbol, portfolio)
    }
    # Compared per (time, symbol, portfolio): an order that waited for a price
    # can fill in the same bar as a later one, and crossed fills sum to zero.
    Order = rdb.LedgerOrder
    problems = []
    for ts, symbol, p, qty in (
        session.query(Order.ts, Order.symbol, Order.portfolio, func.sum(Order.qty))
        .filter(Order.run_id == run_id)
        .group_by(Order.ts, Order.symbol, Order.portfolio)
    ):
        books = netted.get((ts, symbol, p))
        if books != int(qty):
            problems.append(f"{symbol} at {ts}: orders {int(qty)}, books {books}")
    return _check("Every order equals the book fills it was netted from", problems)


def _portfolios_from_events(session, run_id: int) -> dict[int, int]:
    """Instance to portfolio, from the order intents, for a run still in progress."""
    out = {}
    for (payload,) in session.query(rdb.LedgerEvent.payload).filter_by(
        run_id=run_id, kind="ORDER_INTENT"
    ):
        for position in (payload or {}).get("instances", {}):
            out[int(position)] = int(payload["portfolio"])
    return out


def _check_costs(session, run_id: int) -> dict:
    F = rdb.LedgerFill
    allocated = dict(
        session.query(F.order_seq, func.sum(F.fees + F.slippage))
        .filter(F.run_id == run_id, F.order_seq.isnot(None))
        .group_by(F.order_seq)
        .all()
    )
    problems = []
    for order in session.query(rdb.LedgerOrder).filter_by(run_id=run_id):
        charged = order.fees + order.slippage
        given = float(allocated.get(order.seq) or 0.0)
        if not math.isclose(charged, given, abs_tol=TOLERANCE):
            problems.append(f"order {order.seq}: charged {charged:.6f}, allocated {given:.6f}")
    return _check("Every order's costs are allocated to its books in full", problems)


def _open_positions(session, run_id: int) -> list[tuple[int, str]]:
    F = rdb.LedgerFill
    return [
        (instance, symbol)
        for instance, symbol, qty in session.query(F.instance, F.symbol, func.sum(F.qty))
        .filter(F.run_id == run_id)
        .group_by(F.instance, F.symbol)
        if qty
    ]


def _check_positions(session, run_id: int) -> dict:
    """One pass over fills and snapshots in sequence order."""
    F, P = rdb.LedgerFill, rdb.LedgerPosition
    fills = (
        session.query(F.seq, F.instance, F.symbol, F.qty)
        .filter(F.run_id == run_id)
        .order_by(F.seq)
        .all()
    )
    snapshots = session.query(P).filter_by(run_id=run_id).order_by(P.seq).all()
    held: dict[tuple[int, str], int] = {}
    problems, n = [], 0
    for snap in snapshots:
        while n < len(fills) and fills[n].seq < snap.seq:
            key = (fills[n].instance, fills[n].symbol)
            held[key] = held.get(key, 0) + fills[n].qty
            n += 1
        rebuilt = held.get((snap.instance, snap.symbol), 0)
        if rebuilt != snap.qty:
            problems.append(
                f"{snap.symbol} for strategy {snap.instance} at {snap.ts}: "
                f"snapshot {snap.qty}, fills {rebuilt}"
            )
    return _check("Positions rebuilt from the fills match every snapshot", problems)


def _check_order_states(session, run_id: int, finished: bool) -> dict:
    E = rdb.LedgerEvent
    intents = {
        oid
        for (oid,) in session.query(E.platform_order_id).filter_by(
            run_id=run_id, kind="ORDER_INTENT"
        )
    }
    settled = {
        oid
        for (oid,) in session.query(E.platform_order_id).filter(
            E.run_id == run_id, E.kind.in_(TERMINAL)
        )
    }
    waiting = sorted(intents - settled)
    if not finished:
        return _check(
            "Every order reached a final state",
            [],
            applies=False,
            note=f"{len(waiting)} order(s) still open while the session runs.",
        )
    return _check("Every order reached a final state", [f"{oid} has none" for oid in waiting])

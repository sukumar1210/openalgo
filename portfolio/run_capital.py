"""
A Run's money rules: who gets how much capital each day, and how much a copy
may hold. Pure functions over plain values: no database, no clock, no broker,
so a Run trading in the Sandbox and a Run being backtested get the same
answers (services/run_service.py and portfolio/run_sim.py both call these).

**Allocation.** Each member is allocated a fixed amount in rupees or a share
of the Run's capital. Allocations need not add up to the Run's capital; the
Run's return is measured on what was actually allocated.

**The P&L policy** decides each member's capital at a day's open, from what
its account closed the last day with:

* ``reset``: every day starts from the allocation again. A profit is swept
  out and a loss topped up, both as recorded transfers.
* ``own``: each member keeps its own result. A winner's capital grows, a
  loser's shrinks, and neither touches the other's.
* ``pooled``: the members' closing balances are added up and shared out again
  in proportion to their allocations, so the whole Run compounds together.

**The limit** caps how much a member may hold, as its total exposure across
every symbol (quantity times price):

* ``capital``: the member's capital that day, so under ``own`` and ``pooled``
  it grows with profits and shrinks with losses;
* ``fixed``: the allocation, whatever the account holds.

**Copies.** A Run trades each distinct strategy configuration once, as a
source, at a reference capital. A member holds the source's position scaled
to its own capital, rounded toward zero to whole units (``platform``
sizing), or the source's own quantity when the strategy sizes itself
(``self``), and never more than its limit allows.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import ROUND_DOWN, Decimal

POLICIES = ("reset", "own", "pooled")
LIMITS = ("capital", "fixed")
SIZINGS = ("platform", "self")
CENT = Decimal("0.01")


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


def allocation(run_capital, kind: str, value) -> Decimal:
    """A member's allocation: ``value`` rupees (``fixed``) or ``value`` percent of the Run."""
    if kind == "fixed":
        return _money(value)
    if kind == "percent":
        return _money(Decimal(str(run_capital)) * Decimal(str(value)) / 100)
    raise ValueError(f"unknown allocation kind {kind!r}")


def day_open(policy: str, allocations: Mapping[str, Decimal], closing: Mapping[str, Decimal]):
    """Each member's capital at a day's open, and the transfer that gets it there.

    Args:
        policy: One of ``POLICIES``.
        allocations: Each member's allocation.
        closing: What each member's account held at the last close; a member
            missing from it has not traded yet and opens at its allocation.

    Returns:
        ``{member: (capital, transfer)}``; the transfer is what moves into the
        account (negative moves out), so ``closing + transfer == capital``.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    held = {m: _money(closing.get(m, allocations[m])) for m in allocations}
    if policy == "reset":
        capital = {m: _money(a) for m, a in allocations.items()}
    elif policy == "own":
        capital = dict(held)
    else:
        total = sum(held.values(), Decimal("0"))
        weight = sum((Decimal(str(a)) for a in allocations.values()), Decimal("0"))
        capital = {
            m: _money(total * Decimal(str(a)) / weight) if weight else Decimal("0.00")
            for m, a in allocations.items()
        }
        # Rounding to paise must not create or destroy money: the last member
        # takes the remainder.
        if capital:
            last = list(capital)[-1]
            capital[last] += total - sum(capital.values(), Decimal("0"))
    return {m: (capital[m], capital[m] - held[m]) for m in allocations}


def limit_for(limit: str, capital: Decimal, allocated: Decimal) -> Decimal:
    """How much exposure a member may hold today."""
    if limit == "capital":
        return _money(capital)
    if limit == "fixed":
        return _money(allocated)
    raise ValueError(f"unknown limit {limit!r}")


def copy_target(
    source_quantity: int,
    *,
    scale: Decimal,
    sizing: str,
    price: Decimal,
    limit: Decimal,
    exposure_elsewhere: Decimal = Decimal("0"),
) -> int:
    """The position a member should hold given the source's, signed.

    Args:
        source_quantity: The source's net position in the instrument.
        scale: Member capital over the source's reference capital.
        sizing: ``platform`` scales the source's quantity; ``self`` keeps it.
        price: The price the copy fills at.
        limit: The member's exposure limit today.
        exposure_elsewhere: What the member already holds in other symbols.

    Returns:
        The signed quantity, rounded toward zero, within the limit.
    """
    if sizing not in SIZINGS:
        raise ValueError(f"unknown sizing {sizing!r}")
    wanted = Decimal(source_quantity) * (Decimal(str(scale)) if sizing == "platform" else 1)
    quantity = int(abs(wanted).to_integral_value(rounding=ROUND_DOWN))
    price = Decimal(str(price))
    if price > 0:
        room = Decimal(str(limit)) - Decimal(str(exposure_elsewhere))
        quantity = min(quantity, max(int((room / price).to_integral_value(rounding=ROUND_DOWN)), 0))
    return quantity if source_quantity >= 0 else -quantity


def fingerprint(
    version_hash: str,
    *,
    exchange: str,
    schedule: Mapping,
    symbols: Sequence[str],
    settings: Mapping,
    sizing: str,
    capital_dependent: bool = False,
    capital=None,
) -> str:
    """What makes two members trade identically, as a sha256.

    Everything that can change a strategy's decisions is in it; its capital is
    not, because a copy scales the source's quantity to each member's capital.
    A strategy whose decisions depend on its capital gets its capital in it
    too, and so a source of its own per member.
    """
    payload = {
        "version": version_hash,
        "exchange": exchange,
        "schedule": dict(schedule),
        "symbols": list(symbols),
        "settings": dict(settings),
        "sizing": sizing,
    }
    if capital_dependent:
        payload["capital"] = str(_money(capital))
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()

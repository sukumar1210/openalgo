"""
Run metrics, for a whole portfolio and for each strategy book.

Two families, kept apart on purpose. Trade statistics (win rate, profit factor,
average trade, costs) come from the ledger's round trips. Risk statistics come
from the marked-to-market equity curve, never from a curve rebuilt from closed
trades: that reconstruction has points only on exit dates, so it credits a
position held through a drawdown with no risk until the day it closes.

The return-series formulas (CAGR, Sharpe, Sortino) are delegated to
``portfolio.analytics.summary`` and therefore to openstatz, so they are the same
formulas the Portfolio Backtester reports. Max drawdown is computed here on the
bar-level curve, because a daily resample would hide an intraday drawdown that
recovered before the close, which is exactly the drawdown an intraday strategy
has.

CAGR is withheld below a one-year window. Annualising ten days of returns is
extrapolation, not measurement, and a number that exists will eventually be
quoted without its caveat.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research.strategy import IST_OFFSET_SECONDS

#: Shortest window, in calendar days, that gets an annualised return.
MIN_ANNUALISABLE_DAYS = 365


def _finite(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def max_drawdown(equity: np.ndarray) -> float:
    """Worst peak-to-trough fall as a negative fraction, from the bar-level curve."""
    if len(equity) == 0:
        return 0.0
    peaks = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        falls = np.where(peaks > 0, equity / peaks - 1.0, 0.0)
    return float(falls.min())


def daily_returns(timestamps: np.ndarray, equity: np.ndarray) -> pd.Series:
    days = pd.to_datetime(timestamps + IST_OFFSET_SECONDS, unit="s").normalize()
    closes = pd.Series(equity, index=days).groupby(level=0).last()
    return closes.pct_change().dropna()


def summarize(
    timestamps: np.ndarray,
    equity: np.ndarray,
    capital: float,
    trades: list[dict],
    traded_value: float,
    exposure: np.ndarray | None = None,
    buying_power: float | None = None,
) -> dict:
    """Trade and risk statistics for one equity curve.

    ``exposure`` is the gross value held at each bar; with ``buying_power``
    (capital times leverage, the most the book or portfolio may hold) it gives
    how much of the capital was at work.
    """
    net = [t["net_pnl"] for t in trades]
    wins = [p for p in net if p > 0]
    losses = [p for p in net if p < 0]
    fees = sum(t["fees"] for t in trades)
    slippage = sum(t["slippage"] for t in trades)
    pnl = float(equity[-1] - capital) if len(equity) else 0.0
    out = {
        "capital": capital,
        "net_pnl": pnl,
        "gross_pnl": sum(t["gross_pnl"] for t in trades),
        "return_pct": pnl / capital * 100 if capital else None,
        "trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses else None,
        "avg_trade": sum(net) / len(trades) if trades else None,
        "fees": fees,
        "slippage": slippage,
        "costs": fees + slippage,
        "turnover": traded_value / capital if capital else None,
        "final_equity": float(equity[-1]) if len(equity) else capital,
        # From the capital it started with: a curve that begins after a first
        # fill has already paid that fill's charges.
        "max_drawdown_pct": max_drawdown(np.concatenate(([capital], equity)) if capital else equity)
        * 100,
        "avg_exposure_pct": None,
        "max_exposure_pct": None,
        "capital_used_pct": None,
        "time_in_market_pct": None,
        "cagr_pct": None,
        "sharpe": None,
        "sortino": None,
        "days": 0,
    }
    if exposure is not None and len(exposure) and capital:
        out["avg_exposure_pct"] = float(np.mean(exposure)) / capital * 100
        out["max_exposure_pct"] = float(np.max(exposure)) / capital * 100
        out["time_in_market_pct"] = float(np.mean(exposure > 0)) * 100
        if buying_power:
            out["capital_used_pct"] = float(np.mean(exposure)) / buying_power * 100
    returns = daily_returns(timestamps, equity)
    out["days"] = int(len(returns)) + 1 if len(equity) else 0
    if len(returns) >= 2 and returns.std() > 0:
        from portfolio.analytics import summary

        stats = summary(returns)
        span_days = (returns.index[-1] - returns.index[0]).days
        if span_days >= MIN_ANNUALISABLE_DAYS:
            out["cagr_pct"] = _finite(stats.get("cagr", float("nan")) * 100)
        out["sharpe"] = _finite(stats.get("sharpe"))
        out["sortino"] = _finite(stats.get("sortino"))
    return out


def return_correlation(timestamps: np.ndarray, curves: dict[int, np.ndarray]) -> list[dict]:
    """Pairwise correlation of strategies' daily returns; empty below three days."""
    frame = pd.DataFrame({k: daily_returns(timestamps, v) for k, v in curves.items()})
    if len(frame) < 3:
        return []
    corr = frame.corr()
    keys = list(curves)
    out = []
    for n, a in enumerate(keys):
        for b in keys[n + 1 :]:
            out.append({"a": a, "b": b, "value": _finite(corr.loc[a, b])})
    return out

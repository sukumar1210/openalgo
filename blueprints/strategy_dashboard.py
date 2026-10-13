# blueprints/strategy_dashboard.py
"""
The strategy dashboard: every strategy's Sandbox trading, compared.

Session-authenticated: these are the logged-in user's own strategies, read
from the browser. The rules are services/strategy_performance_service.py;
this layer is parameters and status codes.
"""

from datetime import date

from flask import Blueprint, jsonify, request, session

from services import strategy_performance_service as performance
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

strategy_dashboard_bp = Blueprint("strategy_dashboard", __name__, url_prefix="/strategy-dashboard")


def _day(name: str) -> str | None:
    value = (request.args.get(name) or "").strip()
    if value:
        date.fromisoformat(value)  # ValueError for anything that is not a date
    return value or None


@strategy_dashboard_bp.route("/api/data")
@check_session_validity
def api_data():
    """Every strategy's daily P&L and totals; ``combine`` (keys, comma separated) adds them up."""
    try:
        start, end = _day("from"), _day("to")
    except ValueError:
        return jsonify({"status": "error", "message": "Dates must look like 2026-01-31."}), 400
    combine = [k for k in (request.args.get("combine") or "").split(",") if k]
    try:
        data = performance.performance(session.get("user"), start, end, combine)
    except Exception:
        logger.exception("Strategy dashboard: could not build the performance data")
        return jsonify(
            {"status": "error", "message": "Could not load strategy performance. Try again."}
        ), 500
    return jsonify({"status": "success", "data": data})


@strategy_dashboard_bp.route("/api/ledger")
@check_session_validity
def api_ledger():
    """One strategy's fills, newest first."""
    key = (request.args.get("key") or "").strip()
    try:
        fills = performance.ledger(session.get("user"), key)
    except Exception:
        logger.exception("Strategy dashboard: could not build a ledger")
        return jsonify({"status": "error", "message": "Could not load the ledger. Try again."}), 500
    return jsonify({"status": "success", "data": fills})

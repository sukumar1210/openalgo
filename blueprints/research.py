"""Research blueprint: strategy registry, config versions, experiments and runs.

Session-authenticated JSON for the /research pages. UI-only, so nothing here is
exposed under /api/v1: a backtest is driven by the operator from the browser,
and keeping it off the API-key surface keeps it off TradingView webhooks and
every other external caller too.

The handlers only translate. Every decision lives in services/research_service,
and every refusal comes back as ``{"status": "error", "message": ...}`` with a
message written for the trader.
"""

import os

from flask import Blueprint, jsonify, request

from limiter import limiter
from services import research_service as svc
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

research_bp = Blueprint("research_bp", __name__, url_prefix="/research/api")

RESEARCH_READ_LIMIT = os.getenv("RESEARCH_READ_LIMIT", "120 per minute")
RESEARCH_WRITE_LIMIT = os.getenv("RESEARCH_WRITE_LIMIT", "30 per minute")


def _error(message: str, status: int = 400):
    return jsonify({"status": "error", "message": message}), status


@research_bp.route("/strategies", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def list_strategies():
    try:
        result = svc.list_strategies()
    except Exception:
        logger.exception("Research: listing strategies failed")
        return _error("The strategy list could not be loaded. Please try again.", 500)
    return jsonify({"status": "success", **result})


@research_bp.route("/strategies/<int:strategy_id>", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_strategy(strategy_id: int):
    try:
        item = svc.get_strategy(strategy_id)
    except svc.ResearchError as exc:
        return _error(str(exc), 404)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/strategies/<int:strategy_id>/configs", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def save_config(strategy_id: int):
    data = request.get_json(silent=True) or {}
    config = data.get("config")
    if not isinstance(config, dict):
        return _error("The configuration is missing.")
    parent_id = data.get("parent_id")
    if parent_id is not None and not isinstance(parent_id, int):
        return _error("The configuration this was based on is not valid.")
    try:
        item, created = svc.save_config(strategy_id, config, parent_id, data.get("note"))
    except svc.ResearchError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Research: saving a configuration failed")
        return _error("The configuration could not be saved. Please try again.", 500)
    return jsonify({"status": "success", "item": item, "created": created}), (
        201 if created else 200
    )


@research_bp.route("/experiments", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def list_experiments():
    return jsonify({"status": "success", "items": svc.list_experiments()})


@research_bp.route("/experiments", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def create_experiment():
    data = request.get_json(silent=True) or {}
    try:
        item = svc.create_experiment(data.get("name"), data.get("description"))
    except svc.ResearchError as exc:
        return _error(str(exc))
    return jsonify({"status": "success", "item": item}), 201


@research_bp.route("/experiments/<int:experiment_id>", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_experiment(experiment_id: int):
    try:
        item = svc.get_experiment(experiment_id)
    except svc.ResearchError as exc:
        return _error(str(exc), 404)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/experiments/<int:experiment_id>/scenarios", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def create_scenario(experiment_id: int):
    data = request.get_json(silent=True) or {}
    try:
        item = svc.create_scenario(experiment_id, data)
    except svc.ResearchError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Research: creating a scenario failed")
        return _error("The scenario could not be saved. Please try again.", 500)
    return jsonify({"status": "success", "item": item}), 201


@research_bp.route("/experiments/<int:experiment_id>/runs", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def run_experiment(experiment_id: int):
    try:
        items = svc.enqueue_experiment(experiment_id)
    except svc.ResearchError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Research: queuing an experiment failed")
        return _error("The experiment could not be started. Please try again.", 500)
    return jsonify({"status": "success", "items": items})


@research_bp.route("/scenarios/<int:scenario_id>/runs", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def run_scenario(scenario_id: int):
    try:
        item, created = svc.enqueue_run(scenario_id)
    except svc.ResearchError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Research: queuing a run failed")
        return _error("The backtest could not be started. Please try again.", 500)
    return jsonify({"status": "success", "item": item, "created": created})


@research_bp.route("/runs/<int:run_id>", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_run(run_id: int):
    try:
        item = svc.get_run(run_id)
    except svc.ResearchError as exc:
        return _error(str(exc), 404)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/runs/<int:run_id>/cancel", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def cancel_run(run_id: int):
    try:
        item = svc.cancel_run(run_id)
    except svc.ResearchError as exc:
        return _error(str(exc), 404)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/runs/<int:run_id>/trades", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_run_trades(run_id: int):
    instance = request.args.get("instance", type=int)
    offset = request.args.get("offset", default=0, type=int)
    return jsonify({"status": "success", **svc.get_run_trades(run_id, instance, offset)})


@research_bp.route("/runs/<int:run_id>/equity", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_run_equity(run_id: int):
    try:
        item = svc.get_run_equity(run_id)
    except svc.ResearchError as exc:
        return _error(str(exc), 404)
    except Exception:
        logger.exception("Research: reading an equity curve failed")
        return _error("The equity curve could not be read. The server log has the details.", 500)
    return jsonify({"status": "success", "item": item})

# blueprints/runs.py
"""
Runs: strategies trading together in the Sandbox with their own money.

Session-authenticated JSON for the Runs pages. The rules are
services/run_service.py; this layer is parameters and status codes. A refusal
comes back as a 400 carrying the service's own words, which are written for
the trader.
"""

from flask import Blueprint, jsonify, request, session

from services import run_service
from services.run_service import RunError
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

runs_bp = Blueprint("runs", __name__, url_prefix="/runs")

#: What a definition is made of, as the create and edit forms send it.
DEFINITION = ("capital", "mode", "policy", "limit", "members", "loss_limit_pct")
ACTIONS = {
    "start": run_service.start,
    "pause": run_service.pause,
    "kill": run_service.kill,
    "stop": run_service.stop,
}


#: What a page is told when a request fails for a reason the trader cannot act on.
CHANGE_FAILED = "The Run could not be changed just now. Try again."
READ_FAILED = "Your Runs could not be read just now. Try again."


def _answer(work, status=200, failed=CHANGE_FAILED):
    try:
        return jsonify({"status": "success", "data": work()}), status
    except RunError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception:
        logger.exception("Runs: a request failed")
        return jsonify({"status": "error", "message": failed}), 500


def _definition() -> dict:
    body = request.get_json(silent=True) or {}
    return {key: body.get(key) for key in DEFINITION}


@runs_bp.route("/api/pnl")
@check_session_validity
def pnl():
    """Daily net P&L for what the P&L page shows: Runs, Runs' strategies, the main account."""
    from datetime import date

    def listed(name):
        return [v for v in (request.args.get(name) or "").split(",") if v]

    start, end = request.args.get("from") or None, request.args.get("to") or None
    try:
        for value in (start, end):
            if value:
                date.fromisoformat(value)
    except ValueError:
        return jsonify({"status": "error", "message": "Dates must look like 2026-01-31."}), 400
    runs = listed("runs")
    members = [tuple(m.split(":", 1)) for m in listed("members")]
    if any(len(m) != 2 for m in members) or not all(
        run_id.isdecimal() for run_id in runs + [m[0] for m in members]
    ):
        return jsonify({"status": "error", "message": "That Run does not exist."}), 400
    return _answer(
        lambda: run_service.pnl(
            session.get("user"),
            runs=runs,
            members=members,
            main=request.args.get("main") == "1",
            strategies=listed("strategies"),
            start=start,
            end=end,
        ),
        failed="The P&L could not be worked out just now. Try again.",
    )


@runs_bp.route("/api/runs")
@check_session_validity
def list_runs():
    return _answer(lambda: run_service.list_runs(session.get("user")), failed=READ_FAILED)


@runs_bp.route("/api/runs", methods=["POST"])
@check_session_validity
def create_run():
    name = (request.get_json(silent=True) or {}).get("name")
    return _answer(lambda: run_service.create(session.get("user"), name, **_definition()), 201)


@runs_bp.route("/api/runs/<int:run_id>")
@check_session_validity
def get_run(run_id):
    return _answer(lambda: run_service.get(run_id, session.get("user")), failed=READ_FAILED)


@runs_bp.route("/api/runs/<int:run_id>", methods=["PUT"])
@check_session_validity
def edit_run(run_id):
    return _answer(lambda: run_service.edit(run_id, session.get("user"), **_definition()))


@runs_bp.route("/api/runs/<int:run_id>/<action>", methods=["POST"])
@check_session_validity
def act(run_id, action):
    work = ACTIONS.get(action)
    if work is None:
        return jsonify({"status": "error", "message": "Not something a Run can do."}), 404
    return _answer(lambda: work(run_id, session.get("user")))


@runs_bp.route("/api/runs/<int:run_id>", methods=["DELETE"])
@check_session_validity
def delete_run(run_id):
    return _answer(lambda: run_service.delete(run_id, session.get("user")))

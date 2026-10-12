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

from flask import Blueprint, Response, jsonify, request, session, stream_with_context

from limiter import limiter
from services import research_hosted_service as hosted
from services import research_ledger_service as ledger
from services import research_live_service as live
from services import research_sandbox_service as sandbox
from services import research_service as svc
from utils import stream_registry
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


@research_bp.route("/strategies/files", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def upload_strategy_file():
    """Add a strategy as one Python file, as the Python strategy host takes a script."""
    upload = request.files.get("file")
    if upload is None:
        return _error("Choose a strategy file to upload.")
    content = upload.read(svc.MAX_FILE_BYTES + 1)
    replace = request.form.get("replace") == "true"
    try:
        item = svc.upload_strategy_file(upload.filename, content, replace)
    except svc.ResearchError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Research: a strategy file upload failed")
        return _error("The file could not be saved. Please try again.", 500)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/scripts/<script_id>", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def python_strategy_research(script_id: str):
    """A Python strategy's backtests, Sandbox days and results, for its tabs at /python.

    Records any finished Sandbox day first, so a day the script traded is on
    the page the next time it is opened even if the daily job did not run.
    """
    try:
        item = hosted.overview(script_id)
    except hosted.HostedError as exc:
        return _error(str(exc), 404)
    except Exception:
        logger.exception("Research: reading the Python strategy %s failed", script_id)
        return _error("The Python strategy's results could not be read. Please try again.", 500)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/scripts/<script_id>/backtest", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def python_strategy_backtest(script_id: str):
    """Backtest a Python strategy, unchanged, on its own settings."""
    data = request.get_json(silent=True) or {}
    try:
        item = hosted.backtest(
            session.get("user"), script_id, str(data.get("start") or ""), str(data.get("end") or "")
        )
    except (hosted.HostedError, svc.ResearchError) as exc:
        return _error(str(exc))
    except Exception:
        logger.exception(
            "Research: a backtest of the Python strategy %s could not start", script_id
        )
        return _error("The backtest could not be started. Please try again.", 500)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/strategies/<int:strategy_id>", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_strategy(strategy_id: int):
    try:
        item = svc.get_strategy(strategy_id)
    except svc.ResearchError as exc:
        return _error(str(exc), 404)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/strategies/<int:strategy_id>/backtest", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def backtest_strategy(strategy_id: int):
    """Backtest one strategy straight from the registry (PRD section 32)."""
    data = request.get_json(silent=True) or {}
    try:
        item = svc.quick_backtest(strategy_id, data, session.get("user"))
    except svc.ResearchError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Research: a registry backtest could not be started")
        return _error("The backtest could not be started. Please try again.", 500)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/strategies/<int:strategy_id>/scenario", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def strategy_scenario(strategy_id: int):
    """The one-strategy scenario a registry deployment runs."""
    data = request.get_json(silent=True) or {}
    try:
        item = svc.quick_scenario(strategy_id, data)
    except svc.ResearchError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Research: a registry scenario could not be saved")
        return _error("The scenario could not be saved. Please try again.", 500)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/strategies/<int:strategy_id>/<any(pause, stop):action>", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def control_strategy(strategy_id: int, action: str):
    """Pause or stop every session and deployment a strategy is trading in."""
    return _sandbox(
        lambda: {"items": sandbox.control_strategy(session.get("user"), strategy_id, action)}
    )


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
        item = svc.create_experiment(
            data.get("name"), data.get("description"), data.get("objective")
        )
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
        items = svc.enqueue_experiment(experiment_id, session.get("user"))
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
        item, created = svc.enqueue_run(scenario_id, session.get("user"))
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


@research_bp.route("/runs/<int:run_id>/log", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def run_log(run_id: int):
    """The end of a run's or session's log, as Python Strategies shows a script's."""
    try:
        item = svc.run_log(run_id)
    except svc.ResearchError as exc:
        return _error(str(exc), 404)
    except Exception:
        logger.exception("Research: reading the log of run=%s failed", run_id)
        return _error("The log could not be read. Please try again.", 500)
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


# --------------------------------------------------------------------------- #
# The ledger: one journal, its download, and its reconciliation
# --------------------------------------------------------------------------- #


def _ledger_filters() -> dict:
    kinds = {k.strip().upper() for k in (request.args.get("kinds") or "").split(",") if k.strip()}
    return {
        "kinds": kinds or None,
        "instance": request.args.get("instance", type=int),
        "symbol": request.args.get("symbol") or None,
    }


@research_bp.route("/runs/<int:run_id>/ledger", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_run_ledger(run_id: int):
    cursor = request.args.get("cursor", default=0, type=int)
    try:
        page = ledger.journal(run_id, cursor=cursor, **_ledger_filters())
    except ledger.RunNotFound as exc:
        return _error(str(exc), 404)
    except ledger.LedgerError as exc:
        return _error(str(exc))
    return jsonify({"status": "success", **page})


@research_bp.route("/runs/<int:run_id>/ledger.csv", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def download_run_ledger(run_id: int):
    """The journal as CSV, streamed a page at a time so a large run never holds
    the worker. Admitted and released through the stream registry like every
    streamed response; the release is registered twice on purpose (see
    ``utils/stream_registry``)."""
    filters = _ledger_filters()
    try:
        ledger.journal(run_id, limit=1, **filters)
    except ledger.RunNotFound as exc:
        return _error(str(exc), 404)
    except ledger.LedgerError as exc:
        return _error(str(exc))
    ticket = stream_registry.admit("research_ledger_csv", None)

    def rows():
        try:
            for chunk in ledger.journal_csv(run_id, **filters):
                if stream_registry.should_stop():
                    return
                yield chunk
        finally:
            ticket.release()

    response = Response(stream_with_context(rows()), mimetype="text/csv")
    response.headers["Content-Disposition"] = (
        f"attachment; filename=research_run_{run_id}_ledger.csv"
    )
    response.call_on_close(ticket.release)
    return response


@research_bp.route("/runs/<int:run_id>/reconciliation", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_run_reconciliation(run_id: int):
    try:
        item = ledger.reconcile(run_id)
    except ledger.RunNotFound as exc:
        return _error(str(exc), 404)
    except Exception:
        logger.exception("Research: reconciling run %s failed", run_id)
        return _error("The ledger could not be checked. The server log has the details.", 500)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/runs/<int:first_id>/compare/<int:second_id>", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def compare_runs(first_id: int, second_id: int):
    try:
        item = ledger.compare_runs(first_id, second_id)
    except ledger.RunNotFound as exc:
        return _error(str(exc), 404)
    except ledger.LedgerError as exc:
        return _error(str(exc))
    return jsonify({"status": "success", "item": item})


# --------------------------------------------------------------------------- #
# Live: the overview, the kill switch, reconciliation, approvals
# --------------------------------------------------------------------------- #


def _live(fn, status: int = 200):
    try:
        result = fn()
    except live.LiveRefused as exc:
        return _error(str(exc), exc.status)
    except Exception:
        logger.exception("Research Live request failed")
        return _error(
            "The Live request could not be completed. The server log has the details.", 500
        )
    return jsonify({"status": "success", "item": result}), status


@research_bp.route("/live", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def live_overview():
    return _live(lambda: live.live_overview(session.get("user")))


@research_bp.route("/live/kill", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def live_kill_switch():
    data = request.get_json(silent=True) or {}
    return _live(
        lambda: live.set_kill_switch(
            session.get("user"), bool(data.get("engaged")), data.get("reason")
        )
    )


@research_bp.route("/live/reconciliation", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def live_reconciliation():
    return _live(lambda: live.reconcile_live(session.get("user")))


@research_bp.route("/versions/<int:version_id>/properties", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def version_properties(version_id: int):
    """Record a version's training period and details; descriptive only."""
    data = request.get_json(silent=True) or {}
    try:
        item = svc.set_version_properties(session.get("user"), version_id, data)
    except svc.ResearchError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Research: version properties could not be saved")
        return _error("The properties could not be saved. Please try again.", 500)
    return jsonify({"status": "success", "item": item})


@research_bp.route("/versions/<int:version_id>/live", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def live_approval(version_id: int):
    data = request.get_json(silent=True) or {}
    return _live(
        lambda: live.set_live_approval(session.get("user"), version_id, bool(data.get("approved")))
    )


# --------------------------------------------------------------------------- #
# Sandbox deployments and sessions
# --------------------------------------------------------------------------- #


def _sandbox(fn, status: int = 200):
    try:
        result = fn()
    except sandbox.SandboxRefused as exc:
        return _error(str(exc), exc.status)
    except Exception:
        logger.exception("Research Sandbox request failed")
        return _error("The Sandbox request could not be completed. Please try again.", 500)
    return jsonify({"status": "success", "item": result}), status


@research_bp.route("/deployments", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def list_deployments():
    return _sandbox(lambda: sandbox.list_deployments(session.get("user")))


@research_bp.route("/deployments", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def create_deployment():
    data = request.get_json(silent=True) or {}
    return _sandbox(lambda: sandbox.create_deployment(session.get("user"), data), 201)


@research_bp.route("/deployments/<int:deployment_id>", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_deployment(deployment_id: int):
    return _sandbox(lambda: sandbox.get_deployment(session.get("user"), deployment_id))


@research_bp.route("/deployments/<int:deployment_id>/<action>", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def deployment_action(deployment_id: int, action: str):
    user = session.get("user")

    def act():
        if action == "stop":
            return sandbox.stop_deployment(user, deployment_id)
        if action == "activate":
            return sandbox.activate_deployment(user, deployment_id)
        if action == "start-now":
            return sandbox.start_now(user, deployment_id)
        raise sandbox.SandboxRefused("Unknown action.", 404)

    return _sandbox(act)


@research_bp.route("/operations", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def operations():
    return _sandbox(svc.operations)


@research_bp.route("/sessions/stop-all", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def stop_all_sessions():
    return _sandbox(lambda: sandbox.stop_all_sessions(session.get("user")))


@research_bp.route("/sessions/<int:run_id>", methods=["GET"])
@check_session_validity
@limiter.limit(RESEARCH_READ_LIMIT)
def get_sandbox_session(run_id: int):
    return _sandbox(lambda: sandbox.get_session(session.get("user"), run_id))


@research_bp.route("/sessions/<int:run_id>/<action>", methods=["POST"])
@check_session_validity
@limiter.limit(RESEARCH_WRITE_LIMIT)
def control_sandbox_session(run_id: int, action: str):
    return _sandbox(lambda: sandbox.control_session(session.get("user"), run_id, action))

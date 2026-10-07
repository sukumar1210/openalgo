"""Research Sandbox execution API: the only way a Sandbox session places an order.

Called by ``research/sandbox_worker.py`` with the deploying user's API key, the
same way a Python strategy calls ``/api/v1``. Every endpoint here acts on the
sandbox only and never reads the analyzer switch, so it cannot reach a broker
whatever mode the platform is in; see ``services/research_sandbox_service.py``.
"""

import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource

from limiter import limiter
from services import research_sandbox_service as svc
from utils.logging import get_logger

ORDER_RATE_LIMIT = os.getenv("ORDER_RATE_LIMIT", "10 per second")
API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")

api = Namespace("research_sandbox", description="Research Sandbox execution API")

logger = get_logger(__name__)


def _body() -> tuple[str, int, dict]:
    data = request.get_json(silent=True) or {}
    try:
        run_id = int(data.get("run_id"))
    except (TypeError, ValueError):
        raise svc.SandboxRefused("The session is missing.") from None
    return str(data.get("apikey") or ""), run_id, data


def _answer(fn):
    try:
        result = fn()
    except svc.SandboxRefused as exc:
        return make_response(jsonify({"status": "error", "message": str(exc)}), exc.status)
    except Exception:
        logger.exception("Research Sandbox API request failed")
        return make_response(
            jsonify(
                {
                    "status": "error",
                    "message": "The sandbox could not handle the order. The server log has the details.",
                }
            ),
            500,
        )
    return make_response(jsonify({"status": "success", "data": result}), 200)


@api.route("/placeorder", strict_slashes=False)
class PlaceSandboxOrder(Resource):
    @limiter.limit(ORDER_RATE_LIMIT)
    def post(self):
        """Place one MARKET order in the sandbox for a Research Sandbox session."""

        def run():
            api_key, run_id, data = _body()
            return svc.place_sandbox_order(api_key, run_id, data)

        return _answer(run)


@api.route("/orderstatus", strict_slashes=False)
class SandboxOrderStatus(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Status of a session's order, by its platform order id."""

        def run():
            api_key, run_id, data = _body()
            return svc.sandbox_order_status(api_key, run_id, str(data.get("platform_order_id")))

        return _answer(run)


@api.route("/cancelorder", strict_slashes=False)
class CancelSandboxOrder(Resource):
    @limiter.limit(ORDER_RATE_LIMIT)
    def post(self):
        """Cancel a session's order if it is still open."""

        def run():
            api_key, run_id, data = _body()
            return svc.cancel_sandbox_order(api_key, run_id, str(data.get("platform_order_id")))

        return _answer(run)

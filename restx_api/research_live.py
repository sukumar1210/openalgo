"""Research Live execution API: the only way a research session places a broker order.

Called by ``research/sandbox_worker.py`` for a LIVE session only, with the
deploying user's API key, the same way a Python strategy calls ``/api/v1``.
Every rule a live order must pass is in ``services/research_live_service.py``;
a Sandbox or Shadow session is refused here by name, whatever it sends.
"""

import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource

from limiter import limiter
from services import research_live_service as svc
from utils.logging import get_logger

ORDER_RATE_LIMIT = os.getenv("ORDER_RATE_LIMIT", "10 per second")
API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")

api = Namespace("research_live", description="Research Live execution API")

logger = get_logger(__name__)


def _body() -> tuple[str, int, dict]:
    data = request.get_json(silent=True) or {}
    try:
        run_id = int(data.get("run_id"))
    except (TypeError, ValueError):
        raise svc.LiveRefused("The session is missing.") from None
    return str(data.get("apikey") or ""), run_id, data


def _answer(fn):
    try:
        result = fn()
    except svc.LiveRefused as exc:
        return make_response(jsonify({"status": "error", "message": str(exc)}), exc.status)
    except Exception:
        logger.exception("Research Live API request failed")
        return make_response(
            jsonify(
                {
                    "status": "error",
                    "message": "The live order could not be handled. The server log has the details.",
                }
            ),
            500,
        )
    return make_response(jsonify({"status": "success", "data": result}), 200)


@api.route("/placeorder", strict_slashes=False)
class PlaceLiveOrder(Resource):
    @limiter.limit(ORDER_RATE_LIMIT)
    def post(self):
        """Place one MARKET order with the broker for a Research Live session."""

        def run():
            api_key, run_id, data = _body()
            return svc.place_live_order(api_key, run_id, data)

        return _answer(run)


@api.route("/orderstatus", strict_slashes=False)
class LiveOrderStatus(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Status of a live session's order, by its platform order id."""

        def run():
            api_key, run_id, data = _body()
            return svc.live_order_status(api_key, run_id, str(data.get("platform_order_id")))

        return _answer(run)


@api.route("/cancelorder", strict_slashes=False)
class CancelLiveOrder(Resource):
    @limiter.limit(ORDER_RATE_LIMIT)
    def post(self):
        """Cancel a live session's order if the broker still has it open."""

        def run():
            api_key, run_id, data = _body()
            return svc.cancel_live_order(api_key, run_id, str(data.get("platform_order_id")))

        return _answer(run)

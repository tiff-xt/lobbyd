"""Structured logging and the request id that ties log lines together."""

import logging
import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lobbyd.platform import metrics, tracing

REQUEST_ID_HEADER = "X-Request-ID"
# Accept a caller's id only if it is short and plain, so it is safe to log and echo.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def configure_logging(*, json: bool) -> None:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            tracing.add_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


class RequestContextMiddleware:
    """Gives every request an id, returns it in a header, and logs one line per request.

    Written as plain ASGI (not BaseHTTPMiddleware) so context variables set here
    are visible to the handler and it adds no extra task per request.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.log = structlog.get_logger("lobbyd.request")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status = 500
        started = time.perf_counter()
        metrics.HTTP_IN_FLIGHT.inc()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed = time.perf_counter() - started
            metrics.HTTP_IN_FLIGHT.dec()
            # The route's template, set by the router once it has matched.
            # Paths that match nothing share one label, so a scanner probing
            # random URLs cannot create a series per URL.
            route = getattr(scope.get("route"), "path", "unmatched")
            if route != "/metrics":
                metrics.HTTP_REQUESTS.labels(scope["method"], route, str(status)).inc()
                metrics.HTTP_DURATION.labels(scope["method"], route).observe(elapsed)
                self.log.info(
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status,
                    duration_ms=round(elapsed * 1000, 1),
                )

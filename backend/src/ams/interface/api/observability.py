"""Request correlation and structured JSON logging for the API boundary."""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any
from uuid import uuid4

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

TRACE_HEADER = "X-Trace-ID"


def configure_logging(log_level: str = "INFO") -> None:
    """Configure stdlib and structlog to emit context-aware JSON records."""

    logging.basicConfig(level=log_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[log_level]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_actor(actor_id: object) -> None:
    """Attach an authenticated actor identifier to the current request context."""

    bind_contextvars(actor_id=str(actor_id))


class RequestObservabilityMiddleware:
    """Give each HTTP request one trace ID and correlated completion log."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._logger = structlog.get_logger("ams.api")

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        """Bind request context and add the trace ID to every HTTP response."""

        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        clear_contextvars()
        trace_id = str(uuid4())
        scope.setdefault("state", {})["trace_id"] = trace_id
        bind_contextvars(trace_id=trace_id, method=scope["method"])
        started_at = perf_counter()
        status_code = 500

        async def send_with_trace(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                trace_header = TRACE_HEADER.lower().encode()
                headers = [
                    header
                    for header in message.get("headers", [])
                    if header[0].lower() != trace_header
                ]
                headers.append((TRACE_HEADER.lower().encode(), trace_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self._app(scope, receive, send_with_trace)
        finally:
            route = scope.get("route")
            route_path = getattr(route, "path", scope.get("path", "unknown"))
            bind_contextvars(route=route_path)
            self._logger.info(
                "http_request_completed",
                status_code=status_code,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
            clear_contextvars()

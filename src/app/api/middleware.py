"""Pure ASGI middleware: correlation id and access log for every HTTP request."""

import re
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.infrastructure.observability.correlation import bind_correlation_id, clear_correlation_id
from app.infrastructure.observability.logging import get_logger

REQUEST_ID_HEADER = "x-request-id"
# Accept a caller's id only if it is short and harmless to log
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_QUIET_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})

log = get_logger("app.api.access")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.encode())
        candidate = incoming.decode("latin-1") if incoming else None
        correlation_id = bind_correlation_id(
            candidate if candidate and _VALID_REQUEST_ID.fullmatch(candidate) else None
        )
        status_code = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), correlation_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            if scope["path"] not in _QUIET_PATHS:
                log.info(
                    "http.request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            clear_correlation_id()

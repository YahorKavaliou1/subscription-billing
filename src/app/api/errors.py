"""Mapping of errors to RFC 9457 problem responses.

Domain errors carry business meaning; the API decides only which HTTP status fits them.
"""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas.common import Problem
from app.application.ports import ConcurrentUpdateError, DuplicateKeyError
from app.domain.errors import (
    ConflictError,
    DomainError,
    InvalidStateTransitionError,
    InvalidValueError,
    NotFoundError,
)
from app.infrastructure.observability.logging import get_logger

PROBLEM_JSON = "application/problem+json"
log = get_logger(__name__)


class UnauthorizedError(Exception):
    """Missing or invalid API key."""


_STATUS_BY_ERROR: list[tuple[type[Exception], HTTPStatus]] = [
    (NotFoundError, HTTPStatus.NOT_FOUND),
    (ConflictError, HTTPStatus.CONFLICT),
    (InvalidStateTransitionError, HTTPStatus.CONFLICT),
    (InvalidValueError, HTTPStatus.UNPROCESSABLE_ENTITY),
    (DomainError, HTTPStatus.BAD_REQUEST),
    # Race lost even after the use case retried: the client may simply retry
    (DuplicateKeyError, HTTPStatus.CONFLICT),
    (ConcurrentUpdateError, HTTPStatus.CONFLICT),
]


def problem_response(
    request: Request,
    status: HTTPStatus,
    detail: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = Problem(
        title=status.phrase,
        status=status.value,
        detail=detail,
        instance=request.url.path,
        errors=errors,
    )
    return JSONResponse(
        body.model_dump(exclude_none=True),
        status_code=status.value,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


async def _handle_known_error(request: Request, exc: Exception) -> JSONResponse:
    status = next(s for error_type, s in _STATUS_BY_ERROR if isinstance(exc, error_type))
    log.info("request.rejected", status=status.value, error=type(exc).__name__, detail=str(exc))
    return problem_response(request, status, str(exc))


async def _handle_unauthorized(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(
        request,
        HTTPStatus.UNAUTHORIZED,
        "Missing or invalid X-API-Key header",
        headers={"WWW-Authenticate": "ApiKey"},
    )


async def _handle_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return problem_response(
        request, HTTPStatus.UNPROCESSABLE_ENTITY, "Request validation failed", errors
    )


async def _handle_http(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return problem_response(request, HTTPStatus(exc.status_code), str(exc.detail))


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("request.failed")
    return problem_response(request, HTTPStatus.INTERNAL_SERVER_ERROR)


def register_error_handlers(app: FastAPI) -> None:
    for error_type, _ in _STATUS_BY_ERROR:
        app.add_exception_handler(error_type, _handle_known_error)
    app.add_exception_handler(UnauthorizedError, _handle_unauthorized)
    app.add_exception_handler(RequestValidationError, _handle_validation)
    app.add_exception_handler(StarletteHTTPException, _handle_http)
    app.add_exception_handler(Exception, _handle_unexpected)

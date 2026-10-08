"""Correlation id shared by HTTP requests, outbox events and consumers.

The id is stored in structlog's contextvars, so every log record emitted
in the same asyncio task automatically carries it.
"""

import uuid

import structlog

CORRELATION_ID_KEY = "correlation_id"


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind the given (or a freshly generated) id to the current context and return it."""
    value = correlation_id or new_correlation_id()
    structlog.contextvars.bind_contextvars(**{CORRELATION_ID_KEY: value})
    return value


def get_correlation_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get(CORRELATION_ID_KEY)
    return value if isinstance(value, str) else None


def clear_correlation_id() -> None:
    structlog.contextvars.unbind_contextvars(CORRELATION_ID_KEY)

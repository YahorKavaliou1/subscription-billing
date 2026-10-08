"""Translation of database errors into application port errors."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import IntegrityError

from app.application.ports import DuplicateKeyError

UNIQUE_VIOLATION = "23505"


def _sqlstate(error: IntegrityError) -> str | None:
    # asyncpg's exception is wrapped by SQLAlchemy's adapter; check both levels
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        sqlstate = getattr(candidate, "sqlstate", None)
        if isinstance(sqlstate, str):
            return sqlstate
    return None


def _constraint_name(error: IntegrityError) -> str | None:
    for candidate in (getattr(error.orig, "__cause__", None), error.orig):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


@asynccontextmanager
async def translate_integrity_errors() -> AsyncIterator[None]:
    """Unique violations become DuplicateKeyError; other integrity errors propagate."""
    try:
        yield
    except IntegrityError as error:
        if _sqlstate(error) == UNIQUE_VIOLATION:
            raise DuplicateKeyError(_constraint_name(error)) from error
        raise

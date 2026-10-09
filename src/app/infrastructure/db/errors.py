"""Translation of database errors into application port errors and HTTP-level categories."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm.exc import StaleDataError

from app.application.ports import ConcurrentUpdateError, DuplicateKeyError

UNIQUE_VIOLATION = "23505"
# The transaction was aborted only because of concurrent transactions; retrying succeeds
TRANSIENT_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
    }
)

# PostgreSQL is unreachable (down, restarting, network) or every pooled connection is busy.
# A temporary condition, unlike a bug or a constraint violation.
DATABASE_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    OperationalError,
    InterfaceError,
    PoolTimeoutError,  # no free connection in the pool within pool_timeout
    OSError,  # includes ConnectionRefusedError raised while opening a connection
    TimeoutError,
)


def sqlstate(error: DBAPIError) -> str | None:
    # asyncpg's exception is wrapped by SQLAlchemy's adapter; check both levels
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        value = getattr(candidate, "sqlstate", None)
        if isinstance(value, str):
            return value
    return None


def is_transient(error: DBAPIError) -> bool:
    """Deadlock or serialization failure: safe to retry the whole request."""
    return sqlstate(error) in TRANSIENT_SQLSTATES


def _constraint_name(error: IntegrityError) -> str | None:
    for candidate in (getattr(error.orig, "__cause__", None), error.orig):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


@asynccontextmanager
async def translate_write_errors() -> AsyncIterator[None]:
    """Unique violations become DuplicateKeyError, lost optimistic locks ConcurrentUpdateError.

    Other errors propagate unchanged.
    """
    try:
        yield
    except IntegrityError as error:
        if sqlstate(error) == UNIQUE_VIOLATION:
            raise DuplicateKeyError(_constraint_name(error)) from error
        raise
    except StaleDataError as error:
        # The row's version changed since it was read (subscriptions.version)
        raise ConcurrentUpdateError(str(error)) from error

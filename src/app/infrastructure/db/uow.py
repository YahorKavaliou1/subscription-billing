"""Unit of Work over one AsyncSession: one use case, one database transaction."""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import UnitOfWorkFactory
from app.infrastructure.db.errors import translate_write_errors
from app.infrastructure.db.repositories import (
    SqlInboxRepository,
    SqlNotificationRepository,
    SqlOutboxRepository,
    SqlPaymentRepository,
    SqlSubscriptionRepository,
)


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self.subscriptions = SqlSubscriptionRepository(self._session)
        self.notifications = SqlNotificationRepository(self._session)
        self.payments = SqlPaymentRepository(self._session)
        self.outbox = SqlOutboxRepository(self._session)
        self.inbox = SqlInboxRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            # No-op after a successful commit; discards everything otherwise
            await session.rollback()
        finally:
            await session.close()
            self._session = None

    async def commit(self) -> None:
        async with translate_write_errors():
            await self._require_session().commit()

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Unit of work is not active; use it as `async with`")
        return self._session


def make_uow_factory(session_factory: async_sessionmaker[AsyncSession]) -> UnitOfWorkFactory:
    return lambda: SqlAlchemyUnitOfWork(session_factory)

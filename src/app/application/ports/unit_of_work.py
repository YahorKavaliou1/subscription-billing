"""Unit of Work: one database transaction around one use case."""

from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self

from app.application.ports.repositories import (
    NotificationRepository,
    OutboxRepository,
    PaymentRepository,
    SubscriptionRepository,
)


class UnitOfWork(Protocol):
    """Usage::

        async with uow_factory() as uow:
            ...
            await uow.commit()

    Leaving the block without commit() rolls everything back, so business data
    and outbox events are always saved together or not at all.
    """

    # Read-only properties: implementations may expose more specific repository types
    @property
    def subscriptions(self) -> SubscriptionRepository: ...

    @property
    def notifications(self) -> NotificationRepository: ...

    @property
    def payments(self) -> PaymentRepository: ...

    @property
    def outbox(self) -> OutboxRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None:
        """Raise DuplicateKeyError if a unique constraint is violated."""
        ...


UnitOfWorkFactory = Callable[[], UnitOfWork]

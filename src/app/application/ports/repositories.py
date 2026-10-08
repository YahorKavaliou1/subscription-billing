"""Persistence ports. Implemented in infrastructure/db, faked in unit tests."""

import uuid
from collections.abc import Sequence
from typing import Protocol

from app.application.dto import OutboxEventInfo, OutboxMessage
from app.domain.entities import Notification, Payment, Subscription


class SubscriptionRepository(Protocol):
    async def get(self, subscription_id: str, *, for_update: bool = False) -> Subscription | None:
        """`for_update` locks the row until the end of the transaction."""
        ...

    async def add(self, subscription: Subscription) -> None: ...

    async def update(self, subscription: Subscription) -> None: ...


class NotificationRepository(Protocol):
    async def list_for_subscription(self, subscription_id: str) -> list[Notification]:
        """All notifications of the subscription, ordered by scheduled_for."""
        ...

    async def get_for_payment(self, payment_id: uuid.UUID) -> Notification | None: ...

    async def add_many(self, notifications: Sequence[Notification]) -> None: ...

    async def update_many(self, notifications: Sequence[Notification]) -> None: ...


class PaymentRepository(Protocol):
    async def get(self, payment_id: uuid.UUID) -> Payment | None: ...

    async def get_by_provider_payment(self, provider_payment: str) -> Payment | None: ...

    async def add(self, payment: Payment) -> None: ...


class OutboxRepository(Protocol):
    async def add(self, message: OutboxMessage) -> uuid.UUID:
        """Store an event in the current transaction; return its id (= broker message_id)."""
        ...

    async def get_latest_for_aggregate(
        self, aggregate_type: str, aggregate_id: str
    ) -> OutboxEventInfo | None: ...

"""Persistence ports. Implemented in infrastructure/db, faked in unit tests."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.application.dto import OutboxEventInfo, OutboxMessage, PendingEvent
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

    async def get(self, notification_id: uuid.UUID) -> Notification | None: ...

    async def claim_due(self, limit: int, now: datetime) -> list[Notification]:
        """Lock up to `limit` scheduled notifications whose time has come, earliest first.

        Rows locked by another scheduler are skipped (FOR UPDATE SKIP LOCKED).
        """
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

    # --- Relay side ----------------------------------------------------------------

    async def claim_pending(self, limit: int, now: datetime) -> list[PendingEvent]:
        """Lock up to `limit` pending events that are due, oldest first.

        Rows locked by another relay are skipped, so several relays never
        publish the same event concurrently.
        """
        ...

    async def mark_published(self, event_id: uuid.UUID, at: datetime) -> None: ...

    async def mark_failed(self, event_id: uuid.UUID, error: str, retry_at: datetime | None) -> None:
        """Count a failed attempt; `retry_at=None` means give up (status dead)."""
        ...


class InboxRepository(Protocol):
    """Messages already handled by a consumer (deduplication of at-least-once delivery)."""

    async def add(self, consumer: str, message_id: str) -> bool:
        """Record the message; return False if this consumer has already recorded it."""
        ...

    async def exists(self, consumer: str, message_id: str) -> bool: ...

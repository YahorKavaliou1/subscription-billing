"""Input commands and output views of the use cases.

Views are plain immutable data, independent of both the ORM and the HTTP schemas.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.entities import Notification, Payment, Subscription
from app.domain.enums import (
    NotificationEvent,
    NotificationStatus,
    OutboxStatus,
    PaymentStatus,
    SubscriptionStatus,
)

# --- Commands ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpsertSubscriptionCommand:
    subscription_id: str
    user_id: str
    day_count: int
    expected_expires_on: datetime


@dataclass(frozen=True, slots=True)
class RegisterPaymentCommand:
    subscription_id: str
    provider_payment: str
    amount: Decimal
    currency: str
    status: PaymentStatus


# --- Outbox --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """Event to be published; written in the same transaction as the business data."""

    aggregate_type: str
    aggregate_id: str
    event_type: str
    routing_key: str
    payload: dict[str, Any]
    headers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboxEventInfo:
    """Delivery state of an outbox event, for diagnostics."""

    id: uuid.UUID
    event_type: str
    status: OutboxStatus
    attempts: int
    created_at: datetime
    published_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class PendingEvent:
    """Outbox event claimed by the relay for publishing."""

    id: uuid.UUID
    event_type: str
    routing_key: str
    payload: dict[str, Any]
    headers: dict[str, Any]
    attempts: int
    created_at: datetime


# --- Views ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NotificationView:
    event_name: NotificationEvent
    scheduled_for: datetime
    sent_at: datetime | None
    status: NotificationStatus
    attempts: int
    last_error: str | None

    @classmethod
    def from_entity(cls, notification: Notification) -> "NotificationView":
        return cls(
            event_name=notification.event_name,
            scheduled_for=notification.scheduled_for,
            sent_at=notification.sent_at,
            status=notification.status,
            attempts=notification.attempts,
            last_error=notification.last_error,
        )


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    subscription_id: str
    user_id: str
    day_count: int
    expected_expires_on: datetime
    status: SubscriptionStatus
    notifications: list[NotificationView]

    @classmethod
    def build(
        cls, subscription: Subscription, notifications: list[Notification]
    ) -> "SubscriptionView":
        ordered = sorted(notifications, key=lambda n: (n.scheduled_for, n.event_name))
        return cls(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            day_count=subscription.day_count,
            expected_expires_on=subscription.expected_expires_on,
            status=subscription.status,
            notifications=[NotificationView.from_entity(n) for n in ordered],
        )


@dataclass(frozen=True, slots=True)
class UpsertSubscriptionResult:
    subscription: SubscriptionView
    created: bool


@dataclass(frozen=True, slots=True)
class PaymentView:
    id: uuid.UUID
    subscription_id: str
    provider_payment: str
    amount: Decimal
    currency: str
    status: PaymentStatus
    created_at: datetime | None

    @classmethod
    def from_entity(cls, payment: Payment) -> "PaymentView":
        return cls(
            id=payment.id,
            subscription_id=payment.subscription_id,
            provider_payment=payment.provider_payment,
            amount=payment.money.amount,
            currency=payment.money.currency,
            status=payment.status,
            created_at=payment.created_at,
        )


@dataclass(frozen=True, slots=True)
class RegisterPaymentResult:
    payment: PaymentView
    event_id: uuid.UUID | None
    created: bool  # False: idempotent replay of an already registered payment


@dataclass(frozen=True, slots=True)
class PaymentStatusView:
    """Payment state plus delivery facts (task item 4).

    `event` answers "was the event published to the broker",
    `notification` answers "was the user notified".
    """

    payment: PaymentView
    event: OutboxEventInfo | None
    notification: NotificationView | None

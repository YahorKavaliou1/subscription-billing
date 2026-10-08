"""Status enums shared by the domain and the persistence layer."""

from enum import StrEnum


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


class NotificationStatus(StrEnum):
    SCHEDULED = "scheduled"  # waiting for scheduled_for
    ENQUEUED = "enqueued"  # event written to the outbox
    SENT = "sent"  # delivered to the notification channel
    FAILED = "failed"  # gave up after retries
    CANCELLED = "cancelled"  # superseded by a subscription update


class PaymentStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class NotificationEvent(StrEnum):
    """What a notification tells the user; also the event name in history."""

    EXPIRING_SOON = "subscription.expiring_soon"
    EXPIRED = "subscription.expired"
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"


class OutboxStatus(StrEnum):
    PENDING = "pending"  # not yet confirmed by the broker
    PUBLISHED = "published"  # broker confirmed the message
    DEAD = "dead"  # gave up after max attempts

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


class OutboxStatus(StrEnum):
    PENDING = "pending"  # not yet confirmed by the broker
    PUBLISHED = "published"  # broker confirmed the message
    DEAD = "dead"  # gave up after max attempts

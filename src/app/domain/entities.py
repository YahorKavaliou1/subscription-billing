"""Domain entities: state and the rules for changing it. No I/O, no framework imports."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.enums import (
    NotificationEvent,
    NotificationStatus,
    PaymentStatus,
    SubscriptionStatus,
)
from app.domain.errors import (
    InvalidStateTransitionError,
    InvalidValueError,
    SubscriptionOwnershipConflictError,
)
from app.domain.value_objects import Money, require_aware_utc, require_id

MAX_PROVIDER_PAYMENT_LENGTH = 128
MAX_ERROR_LENGTH = 2000


def _require_day_count(value: int) -> int:
    if value <= 0:
        raise InvalidValueError("day_count must be positive")
    return value


@dataclass(slots=True)
class Subscription:
    id: str
    user_id: str
    day_count: int
    expected_expires_on: datetime
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE

    def __post_init__(self) -> None:
        self.id = require_id(self.id, "subscription_id")
        self.user_id = require_id(self.user_id, "user_id")
        self.day_count = _require_day_count(self.day_count)
        self.expected_expires_on = require_aware_utc(
            self.expected_expires_on, "expected_expires_on"
        )

    def ensure_owned_by(self, user_id: str) -> None:
        if self.user_id != user_id.strip():
            raise SubscriptionOwnershipConflictError(self.id)

    def update(self, *, day_count: int, expected_expires_on: datetime) -> bool:
        """Apply new terms; return True if anything affecting the schedule changed."""
        day_count = _require_day_count(day_count)
        expires = require_aware_utc(expected_expires_on, "expected_expires_on")
        changed = (day_count, expires) != (self.day_count, self.expected_expires_on)
        self.day_count = day_count
        self.expected_expires_on = expires
        self.status = SubscriptionStatus.ACTIVE
        return changed

    def renew(self) -> None:
        """Extend the subscription by one period after a successful payment."""
        self.expected_expires_on += timedelta(days=self.day_count)
        self.status = SubscriptionStatus.ACTIVE

    def expire(self) -> None:
        self.status = SubscriptionStatus.EXPIRED


@dataclass(slots=True)
class Payment:
    subscription_id: str
    provider_payment: str
    money: Money
    status: PaymentStatus
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        self.subscription_id = require_id(self.subscription_id, "subscription_id")
        self.provider_payment = require_id(
            self.provider_payment, "provider_payment", MAX_PROVIDER_PAYMENT_LENGTH
        )

    @property
    def event(self) -> NotificationEvent:
        if self.status is PaymentStatus.SUCCEEDED:
            return NotificationEvent.PAYMENT_SUCCEEDED
        return NotificationEvent.PAYMENT_FAILED

    def is_same_request(self, other: "Payment") -> bool:
        """Idempotency check: a retried request must carry exactly the same data."""
        return (
            self.provider_payment == other.provider_payment
            and self.subscription_id == other.subscription_id
            and self.money == other.money
            and self.status == other.status
        )


@dataclass(slots=True)
class Notification:
    subscription_id: str
    event_name: NotificationEvent
    scheduled_for: datetime
    dedup_key: str
    status: NotificationStatus = NotificationStatus.SCHEDULED
    payment_id: uuid.UUID | None = None
    sent_at: datetime | None = None
    attempts: int = 0
    last_error: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        self.scheduled_for = require_aware_utc(self.scheduled_for, "scheduled_for")

    @classmethod
    def for_payment(cls, payment: Payment, now: datetime) -> "Notification":
        """Immediate notification about a payment result; one per payment."""
        event = payment.event
        return cls(
            subscription_id=payment.subscription_id,
            event_name=event,
            scheduled_for=now,
            dedup_key=f"payment:{payment.id}:{event.value}",
            status=NotificationStatus.ENQUEUED,
            payment_id=payment.id,
        )

    # --- State transitions -------------------------------------------------------------
    # scheduled --enqueue--> enqueued --mark_sent--> sent
    #     |                     |---record_failure--> enqueued (retry) / failed
    #     '--cancel--> cancelled

    def cancel(self) -> None:
        self._require(NotificationStatus.SCHEDULED, "cancel")
        self.status = NotificationStatus.CANCELLED

    def enqueue(self) -> None:
        self._require(NotificationStatus.SCHEDULED, "enqueue")
        self.status = NotificationStatus.ENQUEUED

    def mark_sent(self, at: datetime) -> None:
        self._require(NotificationStatus.ENQUEUED, "mark as sent")
        self.status = NotificationStatus.SENT
        self.sent_at = require_aware_utc(at, "sent_at")
        self.attempts += 1
        self.last_error = None

    def record_failure(self, error: str, *, final: bool) -> None:
        """Register a failed send; `final` means retries are exhausted."""
        self._require(NotificationStatus.ENQUEUED, "record a failure for")
        self.attempts += 1
        self.last_error = error[:MAX_ERROR_LENGTH]
        if final:
            self.status = NotificationStatus.FAILED

    @property
    def is_pending(self) -> bool:
        return self.status is NotificationStatus.SCHEDULED

    def _require(self, expected: NotificationStatus, action: str) -> None:
        if self.status is not expected:
            raise InvalidStateTransitionError(
                f"Cannot {action} notification {self.id} in status {self.status.value}"
            )

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.entities import Notification, Payment, Subscription
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
from app.domain.value_objects import Money

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def make_subscription(**overrides: object) -> Subscription:
    values: dict[str, object] = {
        "id": "sub-1",
        "user_id": "user-1",
        "day_count": 30,
        "expected_expires_on": EXPIRES,
    }
    return Subscription(**(values | overrides))  # type: ignore[arg-type]


def make_payment(**overrides: object) -> Payment:
    values: dict[str, object] = {
        "subscription_id": "sub-1",
        "provider_payment": "pay-1",
        "money": Money(Decimal("9.99"), "EUR"),
        "status": PaymentStatus.SUCCEEDED,
    }
    return Payment(**(values | overrides))  # type: ignore[arg-type]


def make_notification(status: NotificationStatus = NotificationStatus.SCHEDULED) -> Notification:
    return Notification(
        subscription_id="sub-1",
        event_name=NotificationEvent.EXPIRING_SOON,
        scheduled_for=EXPIRES - timedelta(days=3),
        dedup_key="k",
        status=status,
    )


class TestSubscription:
    def test_valid_subscription(self) -> None:
        subscription = make_subscription(id=" sub-1 ")

        assert subscription.id == "sub-1"
        assert subscription.status is SubscriptionStatus.ACTIVE

    @pytest.mark.parametrize(
        "overrides",
        [
            {"day_count": 0},
            {"day_count": -5},
            {"id": ""},
            {"user_id": "  "},
            {"expected_expires_on": datetime(2026, 11, 1)},  # noqa: DTZ001  # naive
        ],
    )
    def test_invariants(self, overrides: dict[str, object]) -> None:
        with pytest.raises(InvalidValueError):
            make_subscription(**overrides)

    def test_ownership(self) -> None:
        subscription = make_subscription()

        subscription.ensure_owned_by("user-1")
        with pytest.raises(SubscriptionOwnershipConflictError):
            subscription.ensure_owned_by("user-2")

    def test_update_reports_changes(self) -> None:
        subscription = make_subscription()

        assert subscription.update(day_count=30, expected_expires_on=EXPIRES) is False
        assert subscription.update(day_count=60, expected_expires_on=EXPIRES) is True
        assert subscription.day_count == 60

    def test_update_reactivates_expired_subscription(self) -> None:
        subscription = make_subscription()
        subscription.expire()

        subscription.update(day_count=30, expected_expires_on=EXPIRES + timedelta(days=30))

        assert subscription.status is SubscriptionStatus.ACTIVE

    def test_update_validates_input(self) -> None:
        subscription = make_subscription()

        with pytest.raises(InvalidValueError):
            subscription.update(day_count=0, expected_expires_on=EXPIRES)
        assert subscription.day_count == 30  # unchanged after a rejected update

    def test_renew_extends_by_day_count(self) -> None:
        subscription = make_subscription(day_count=30)
        subscription.expire()

        subscription.renew()

        assert subscription.expected_expires_on == EXPIRES + timedelta(days=30)
        assert subscription.status is SubscriptionStatus.ACTIVE


class TestPayment:
    def test_event_follows_status(self) -> None:
        assert make_payment().event is NotificationEvent.PAYMENT_SUCCEEDED
        failed = make_payment(status=PaymentStatus.FAILED)
        assert failed.event is NotificationEvent.PAYMENT_FAILED

    def test_same_request_ignores_generated_id(self) -> None:
        assert make_payment().is_same_request(make_payment())

    @pytest.mark.parametrize(
        "overrides",
        [
            {"money": Money(Decimal("10"), "EUR")},
            {"money": Money(Decimal("9.99"), "USD")},
            {"status": PaymentStatus.FAILED},
            {"subscription_id": "sub-2"},
        ],
    )
    def test_different_request(self, overrides: dict[str, object]) -> None:
        assert not make_payment().is_same_request(make_payment(**overrides))

    def test_provider_payment_is_required(self) -> None:
        with pytest.raises(InvalidValueError):
            make_payment(provider_payment=" ")


class TestNotification:
    def test_happy_path(self) -> None:
        notification = make_notification()

        notification.enqueue()
        notification.mark_sent(NOW)

        assert notification.status is NotificationStatus.SENT
        assert notification.sent_at == NOW
        assert notification.attempts == 1

    def test_retry_then_give_up(self) -> None:
        notification = make_notification(NotificationStatus.ENQUEUED)

        notification.record_failure("timeout", final=False)
        status_after_retry = notification.status
        notification.record_failure("timeout", final=True)

        assert status_after_retry is NotificationStatus.ENQUEUED
        assert notification.status is NotificationStatus.FAILED
        assert notification.attempts == 2
        assert notification.last_error == "timeout"

    def test_error_text_is_truncated(self) -> None:
        notification = make_notification(NotificationStatus.ENQUEUED)

        notification.record_failure("x" * 10_000, final=True)

        assert notification.last_error is not None
        assert len(notification.last_error) == 2000

    def test_cancel_only_waiting(self) -> None:
        make_notification().cancel()

        with pytest.raises(InvalidStateTransitionError):
            make_notification(NotificationStatus.SENT).cancel()

    @pytest.mark.parametrize(
        "status",
        [NotificationStatus.SENT, NotificationStatus.FAILED, NotificationStatus.CANCELLED],
    )
    def test_final_states_are_terminal(self, status: NotificationStatus) -> None:
        notification = make_notification(status)

        with pytest.raises(InvalidStateTransitionError):
            notification.enqueue()
        with pytest.raises(InvalidStateTransitionError):
            notification.mark_sent(NOW)

    def test_cannot_send_before_enqueue(self) -> None:
        with pytest.raises(InvalidStateTransitionError):
            make_notification().mark_sent(NOW)

    def test_for_payment(self) -> None:
        payment = make_payment(status=PaymentStatus.FAILED)

        notification = Notification.for_payment(payment, NOW)

        assert notification.event_name is NotificationEvent.PAYMENT_FAILED
        assert notification.status is NotificationStatus.ENQUEUED
        assert notification.payment_id == payment.id
        assert notification.dedup_key == f"payment:{payment.id}:payment.failed"
        # Same payment always yields the same key: redelivery cannot duplicate it
        assert Notification.for_payment(payment, NOW).dedup_key == notification.dedup_key

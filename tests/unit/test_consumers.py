"""Consumer use cases on in-memory fakes: idempotency, renewal, delivery and failures."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.ports import OutgoingNotification
from app.application.use_cases.consumers import (
    NOTIFICATIONS_CONSUMER,
    RENEWAL_CONSUMER,
    DeliverNotification,
    DeliveryFailedError,
    Outcome,
    RenewSubscriptionOnPayment,
)
from app.contracts.events import PaymentResultEvent, ReminderDueEvent
from app.domain.entities import Notification, Payment, Subscription
from app.domain.enums import NotificationEvent, NotificationStatus, PaymentStatus
from app.domain.policies import NotificationSchedulePolicy
from app.domain.value_objects import Money
from app.workers.consumers.common import delivery_attempt, event_type_of
from tests.unit.fakes import FakeClock, FakeDatabase

EXPIRES = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
POLICY = NotificationSchedulePolicy([3, 1, 0])


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[OutgoingNotification] = []
        self.fail = fail

    async def send(self, notification: OutgoingNotification) -> None:
        if self.fail:
            raise ConnectionError("smtp down")
        self.sent.append(notification)


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase(FakeClock())
    subscription = Subscription(
        id="sub-1", user_id="user-1", day_count=30, expected_expires_on=EXPIRES
    )
    database.state.subscriptions[subscription.id] = subscription
    for notification in POLICY.reschedule(subscription, [], database.clock.now()).to_create:
        database.state.notifications[notification.id] = notification
    return database


def add_payment(db: FakeDatabase, status: PaymentStatus = PaymentStatus.SUCCEEDED) -> Payment:
    payment = Payment(
        subscription_id="sub-1",
        provider_payment=f"pi_{uuid.uuid4().hex[:6]}",
        money=Money(Decimal("9.99"), "EUR"),
        status=status,
    )
    db.state.payments[payment.id] = payment
    return payment


def payment_event(payment: Payment) -> PaymentResultEvent:
    return PaymentResultEvent(
        payment_id=payment.id,
        subscription_id=payment.subscription_id,
        user_id="user-1",
        provider_payment=payment.provider_payment,
        amount=payment.money.amount,
        currency=payment.money.currency,
        status=payment.status,
        occurred_at=datetime.now(UTC),
    )


class TestRenewal:
    async def test_renews_and_reschedules(self, db: FakeDatabase) -> None:
        renew = RenewSubscriptionOnPayment(db.uow, POLICY, db.clock)

        outcome = await renew.execute("m-1", payment_event(add_payment(db)))

        assert outcome is Outcome.PROCESSED
        new_expiry = EXPIRES + timedelta(days=30)
        assert db.state.subscriptions["sub-1"].expected_expires_on == new_expiry
        statuses = [n.status for n in db.state.notifications.values()]
        assert statuses.count(NotificationStatus.CANCELLED) == 3
        assert statuses.count(NotificationStatus.SCHEDULED) == 3
        assert (RENEWAL_CONSUMER, "m-1") in db.state.inbox

    async def test_redelivery_does_not_renew_twice(self, db: FakeDatabase) -> None:
        renew = RenewSubscriptionOnPayment(db.uow, POLICY, db.clock)
        event = payment_event(add_payment(db))

        await renew.execute("m-1", event)
        outcome = await renew.execute("m-1", event)

        assert outcome is Outcome.DUPLICATE
        expires = db.state.subscriptions["sub-1"].expected_expires_on
        assert expires == EXPIRES + timedelta(days=30)

    async def test_two_payments_renew_twice(self, db: FakeDatabase) -> None:
        renew = RenewSubscriptionOnPayment(db.uow, POLICY, db.clock)

        await renew.execute("m-1", payment_event(add_payment(db)))
        await renew.execute("m-2", payment_event(add_payment(db)))

        expires = db.state.subscriptions["sub-1"].expected_expires_on
        assert expires == EXPIRES + timedelta(days=60)

    async def test_failed_payment_is_ignored(self, db: FakeDatabase) -> None:
        renew = RenewSubscriptionOnPayment(db.uow, POLICY, db.clock)

        outcome = await renew.execute("m-1", payment_event(add_payment(db, PaymentStatus.FAILED)))

        assert outcome is Outcome.SKIPPED
        assert db.state.subscriptions["sub-1"].expected_expires_on == EXPIRES


class TestPaymentNotification:
    async def test_creates_sends_and_marks_sent(self, db: FakeDatabase) -> None:
        sender = RecordingSender()
        payment = add_payment(db, PaymentStatus.FAILED)

        outcome = await DeliverNotification(db.uow, sender, db.clock).for_payment(
            "m-1", payment_event(payment), final_attempt=False
        )

        assert outcome is Outcome.PROCESSED
        [sent] = sender.sent
        assert sent.event_name is NotificationEvent.PAYMENT_FAILED
        assert sent.user_id == "user-1"
        assert sent.data == {"payment_id": str(payment.id), "amount": "9.99", "currency": "EUR"}
        notification = next(n for n in db.state.notifications.values() if n.payment_id)
        assert notification.status is NotificationStatus.SENT
        assert notification.sent_at == db.clock.now()
        assert sent.notification_id == str(notification.id)
        assert (NOTIFICATIONS_CONSUMER, "m-1") in db.state.inbox

    async def test_redelivery_sends_nothing(self, db: FakeDatabase) -> None:
        sender = RecordingSender()
        deliver = DeliverNotification(db.uow, sender, db.clock)
        event = payment_event(add_payment(db))

        await deliver.for_payment("m-1", event, final_attempt=False)
        # Same message again, and the same payment under a new message id (relay re-publish)
        assert await deliver.for_payment("m-1", event, final_attempt=False) is Outcome.DUPLICATE
        assert await deliver.for_payment("m-2", event, final_attempt=False) is Outcome.DUPLICATE

        assert len(sender.sent) == 1

    async def test_failure_is_recorded_and_retried(self, db: FakeDatabase) -> None:
        deliver = DeliverNotification(db.uow, RecordingSender(fail=True), db.clock)
        event = payment_event(add_payment(db))

        with pytest.raises(DeliveryFailedError):
            await deliver.for_payment("m-1", event, final_attempt=False)

        notification = next(n for n in db.state.notifications.values() if n.payment_id)
        assert notification.status is NotificationStatus.ENQUEUED
        assert notification.attempts == 1
        assert notification.last_error == "ConnectionError('smtp down')"
        assert (NOTIFICATIONS_CONSUMER, "m-1") not in db.state.inbox  # redelivery will retry

        # The channel recovers on the next delivery
        retry = DeliverNotification(db.uow, RecordingSender(), db.clock)
        assert await retry.for_payment("m-1", event, final_attempt=False) is Outcome.PROCESSED
        notification = next(n for n in db.state.notifications.values() if n.payment_id)
        assert notification.status is NotificationStatus.SENT
        assert notification.attempts == 2

    async def test_last_attempt_marks_failed(self, db: FakeDatabase) -> None:
        deliver = DeliverNotification(db.uow, RecordingSender(fail=True), db.clock)

        with pytest.raises(DeliveryFailedError):
            await deliver.for_payment("m-1", payment_event(add_payment(db)), final_attempt=True)

        notification = next(n for n in db.state.notifications.values() if n.payment_id)
        assert notification.status is NotificationStatus.FAILED
        assert (NOTIFICATIONS_CONSUMER, "m-1") in db.state.inbox

    async def test_unknown_payment_is_skipped(self, db: FakeDatabase) -> None:
        ghost = Payment(
            subscription_id="sub-1",
            provider_payment="ghost",
            money=Money(Decimal(1), "EUR"),
            status=PaymentStatus.SUCCEEDED,
        )
        deliver = DeliverNotification(db.uow, RecordingSender(), db.clock)

        assert (
            await deliver.for_payment("m-1", payment_event(ghost), final_attempt=False)
            is Outcome.SKIPPED
        )


class TestReminder:
    def reminder(self, db: FakeDatabase) -> Notification:
        return min(db.state.notifications.values(), key=lambda n: n.scheduled_for)

    def event(self, notification: Notification) -> ReminderDueEvent:
        return ReminderDueEvent(
            notification_id=notification.id,
            subscription_id=notification.subscription_id,
            event_name=notification.event_name,
            scheduled_for=notification.scheduled_for,
        )

    async def test_sends_enqueued_reminder(self, db: FakeDatabase) -> None:
        sender = RecordingSender()
        reminder = self.reminder(db)
        reminder.enqueue()

        outcome = await DeliverNotification(db.uow, sender, db.clock).for_reminder(
            "m-1", self.event(reminder), final_attempt=False
        )

        assert outcome is Outcome.PROCESSED
        assert sender.sent[0].event_name is NotificationEvent.EXPIRING_SOON
        assert sender.sent[0].data == {"expected_expires_on": EXPIRES.isoformat()}
        assert db.state.notifications[reminder.id].status is NotificationStatus.SENT

    async def test_cancelled_reminder_is_not_sent(self, db: FakeDatabase) -> None:
        sender = RecordingSender()
        reminder = self.reminder(db)
        reminder.cancel()

        outcome = await DeliverNotification(db.uow, sender, db.clock).for_reminder(
            "m-1", self.event(reminder), final_attempt=False
        )

        assert outcome is Outcome.SKIPPED
        assert sender.sent == []


@pytest.mark.parametrize(
    ("headers", "attempt"),
    [
        ({}, 1),
        ({"x-death": [{"queue": "q", "reason": "rejected", "count": 2}]}, 3),
        (
            {
                "x-death": [
                    {"queue": "q.retry", "reason": "expired", "count": 4},
                    {"queue": "q", "reason": "rejected", "count": 4},
                    {"queue": "other", "reason": "rejected", "count": 9},
                ]
            },
            5,
        ),
    ],
)
def test_delivery_attempt_counts_rejections_from_own_queue(
    headers: dict[str, object], attempt: int
) -> None:
    assert delivery_attempt(headers, "q") == attempt


def test_event_type_prefers_message_type_over_routing_key() -> None:
    # After a retry the routing key is the queue name, the type property is unchanged
    assert event_type_of("payment.failed", {}, "notifications.send") == "payment.failed"
    assert event_type_of(None, {"event_type": "payment.failed"}, "x") == "payment.failed"
    assert event_type_of(None, {}, "payment.succeeded") == "payment.succeeded"

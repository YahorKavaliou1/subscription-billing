import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.application.dto import RegisterPaymentCommand
from app.application.ports import DuplicateKeyError
from app.application.use_cases import GetPaymentStatus, RegisterPayment
from app.domain.entities import Notification, Payment, Subscription
from app.domain.enums import NotificationStatus, OutboxStatus, PaymentStatus
from app.domain.errors import (
    InvalidValueError,
    PaymentIdempotencyConflictError,
    PaymentNotFoundError,
    SubscriptionNotFoundError,
)
from app.domain.value_objects import Money
from tests.unit.fakes import FakeClock, FakeDatabase

EXPIRES = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(clock: FakeClock) -> FakeDatabase:
    database = FakeDatabase(clock)
    database.state.subscriptions["sub-1"] = Subscription(
        id="sub-1", user_id="user-1", day_count=30, expected_expires_on=EXPIRES
    )
    return database


@pytest.fixture
def register(db: FakeDatabase, clock: FakeClock) -> RegisterPayment:
    return RegisterPayment(db.uow, clock)


def command(**overrides: object) -> RegisterPaymentCommand:
    values: dict[str, object] = {
        "subscription_id": "sub-1",
        "provider_payment": "pay-1",
        "amount": Decimal("9.99"),
        "currency": "EUR",
        "status": PaymentStatus.SUCCEEDED,
    }
    return RegisterPaymentCommand(**(values | overrides))  # type: ignore[arg-type]


class TestRegisterPayment:
    async def test_payment_and_event_are_committed_together(
        self, register: RegisterPayment, db: FakeDatabase, clock: FakeClock
    ) -> None:
        result = await register.execute(command())

        assert result.created is True
        [payment] = db.state.payments.values()
        [event] = db.state.outbox
        assert db.commits == 1
        assert result.event_id == event.id
        assert payment.id == result.payment.id
        assert event.message.event_type == "payment.succeeded"
        assert event.message.routing_key == "payment.succeeded"
        assert event.message.aggregate_id == str(payment.id)
        assert event.message.payload == {
            "schema_version": 1,
            "payment_id": str(payment.id),
            "subscription_id": "sub-1",
            "user_id": "user-1",
            "provider_payment": "pay-1",
            "amount": "9.99",
            "currency": "EUR",
            "status": "succeeded",
            "occurred_at": clock.now().isoformat(),
        }

    async def test_failed_payment_emits_failed_event(
        self, register: RegisterPayment, db: FakeDatabase
    ) -> None:
        await register.execute(command(status=PaymentStatus.FAILED))

        assert db.state.outbox[0].message.event_type == "payment.failed"

    async def test_commit_failure_saves_neither_payment_nor_event(
        self, register: RegisterPayment, db: FakeDatabase
    ) -> None:
        db.fail_next_commit_with = ConnectionError("db is gone")

        with pytest.raises(ConnectionError):
            await register.execute(command())

        assert db.state.payments == {}
        assert db.state.outbox == []

    async def test_retry_with_same_data_is_idempotent(
        self, register: RegisterPayment, db: FakeDatabase
    ) -> None:
        first = await register.execute(command())

        second = await register.execute(command(amount=Decimal("9.990")))

        assert second.created is False
        assert second.payment.id == first.payment.id
        assert second.event_id == first.event_id
        assert len(db.state.payments) == 1
        assert len(db.state.outbox) == 1

    async def test_retry_with_different_data_is_rejected(
        self, register: RegisterPayment, db: FakeDatabase
    ) -> None:
        await register.execute(command())

        with pytest.raises(PaymentIdempotencyConflictError):
            await register.execute(command(amount=Decimal("19.99")))

        assert len(db.state.outbox) == 1

    async def test_concurrent_duplicate_returns_the_winner(
        self, register: RegisterPayment, db: FakeDatabase
    ) -> None:
        winner = Payment(
            subscription_id="sub-1",
            provider_payment="pay-1",
            money=Money(Decimal("9.99"), "EUR"),
            status=PaymentStatus.SUCCEEDED,
        )

        def concurrent_insert(database: FakeDatabase) -> None:
            # The same provider payment was committed by another request first
            database.state.payments[winner.id] = winner
            raise DuplicateKeyError("uq_payments_provider_payment")

        db.on_next_commit = concurrent_insert

        result = await register.execute(command())

        assert result.created is False
        assert result.payment.id == winner.id
        assert list(db.state.payments) == [winner.id]

    async def test_unknown_subscription(self, register: RegisterPayment, db: FakeDatabase) -> None:
        with pytest.raises(SubscriptionNotFoundError):
            await register.execute(command(subscription_id="missing"))

        assert db.commits == 0

    @pytest.mark.parametrize(
        "overrides",
        [{"amount": Decimal(0)}, {"currency": "EURO"}, {"provider_payment": ""}],
    )
    async def test_invalid_input(
        self, register: RegisterPayment, db: FakeDatabase, overrides: dict[str, object]
    ) -> None:
        with pytest.raises(InvalidValueError):
            await register.execute(command(**overrides))

        assert db.commits == 0


class TestGetPaymentStatus:
    async def test_pending_event_and_no_notification_yet(
        self, register: RegisterPayment, db: FakeDatabase
    ) -> None:
        registered = await register.execute(command())

        status = await GetPaymentStatus(db.uow).by_id(registered.payment.id)

        assert status.payment.status is PaymentStatus.SUCCEEDED
        assert status.payment.amount == Decimal("9.99")
        assert status.event is not None
        assert status.event.status is OutboxStatus.PENDING
        assert status.event.published_at is None
        assert status.notification is None

    async def test_published_event_and_sent_notification(
        self, register: RegisterPayment, db: FakeDatabase, clock: FakeClock
    ) -> None:
        registered = await register.execute(command())
        # Simulate the relay and the notification worker
        event = db.state.outbox[0]
        event.status, event.published_at, event.attempts = OutboxStatus.PUBLISHED, clock.now(), 1
        payment = db.state.payments[registered.payment.id]
        notification = Notification.for_payment(payment, clock.now())
        notification.mark_sent(clock.now())
        db.state.notifications[notification.id] = notification

        status = await GetPaymentStatus(db.uow).by_provider_payment("pay-1")

        assert status.event is not None
        assert status.event.status is OutboxStatus.PUBLISHED
        assert status.event.published_at == clock.now()
        assert status.notification is not None
        assert status.notification.status is NotificationStatus.SENT
        assert status.notification.sent_at == clock.now()

    async def test_unknown_payment(self, db: FakeDatabase) -> None:
        with pytest.raises(PaymentNotFoundError):
            await GetPaymentStatus(db.uow).by_id(uuid.uuid4())
        with pytest.raises(PaymentNotFoundError):
            await GetPaymentStatus(db.uow).by_provider_payment("missing")

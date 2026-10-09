"""Use cases against real PostgreSQL: atomicity, idempotency and concurrent requests."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.dto import (
    OutboxMessage,
    RegisterPaymentCommand,
    RegisterPaymentResult,
    UpsertSubscriptionCommand,
)
from app.application.ports import Clock, SystemClock, UnitOfWorkFactory
from app.application.use_cases import (
    GetPaymentStatus,
    GetSubscription,
    RegisterPayment,
    UpsertSubscription,
)
from app.domain.entities import Notification
from app.domain.enums import NotificationStatus, OutboxStatus, PaymentStatus
from app.domain.errors import PaymentIdempotencyConflictError, SubscriptionNotFoundError
from app.domain.policies import NotificationSchedulePolicy
from app.infrastructure.db.models import (
    NotificationModel,
    OutboxEventModel,
    PaymentModel,
    SubscriptionModel,
)
from app.infrastructure.db.session import create_session_factory
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork, make_uow_factory
from app.infrastructure.observability.correlation import bind_correlation_id, clear_correlation_id

pytestmark = pytest.mark.integration

CLOCK: Clock = SystemClock()
POLICY = NotificationSchedulePolicy([3, 1, 0])
EXPIRES = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)


def subscription_command(**overrides: Any) -> UpsertSubscriptionCommand:
    values: dict[str, Any] = {
        "subscription_id": "sub-1",
        "user_id": "user-1",
        "day_count": 30,
        "expected_expires_on": EXPIRES,
    }
    return UpsertSubscriptionCommand(**(values | overrides))


def payment_command(**overrides: Any) -> RegisterPaymentCommand:
    values: dict[str, Any] = {
        "subscription_id": "sub-1",
        "provider_payment": "pay-1",
        "amount": Decimal("9.99"),
        "currency": "EUR",
        "status": PaymentStatus.SUCCEEDED,
    }
    return RegisterPaymentCommand(**(values | overrides))


async def count(engine: AsyncEngine, model: type[Any]) -> int:
    async with engine.connect() as connection:
        return int(await connection.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
async def subscription(uow_factory: UnitOfWorkFactory) -> None:
    await UpsertSubscription(uow_factory, POLICY, CLOCK).execute(subscription_command())


class TestSubscriptions:
    async def test_create_and_read_back(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine
    ) -> None:
        created = await UpsertSubscription(uow_factory, POLICY, CLOCK).execute(
            subscription_command()
        )

        view = await GetSubscription(uow_factory).execute("sub-1")

        assert created.created is True
        assert view.expected_expires_on == EXPIRES
        assert [n.status for n in view.notifications] == [NotificationStatus.SCHEDULED] * 3
        assert view.notifications == created.subscription.notifications
        assert await count(engine, NotificationModel) == 3

    async def test_reschedule_cancels_and_creates(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine, subscription: None
    ) -> None:
        upsert = UpsertSubscription(uow_factory, POLICY, CLOCK)
        moved = EXPIRES + timedelta(days=10)

        await upsert.execute(subscription_command(expected_expires_on=moved))
        # Moving back reuses dedup keys of cancelled reminders (partial unique index)
        await upsert.execute(subscription_command())

        view = await GetSubscription(uow_factory).execute("sub-1")
        statuses = [n.status for n in view.notifications]
        assert statuses.count(NotificationStatus.CANCELLED) == 6
        assert statuses.count(NotificationStatus.SCHEDULED) == 3
        assert await count(engine, NotificationModel) == 9

    async def test_repeated_request_writes_nothing(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine, subscription: None
    ) -> None:
        async with engine.connect() as connection:
            version_before = await connection.scalar(select(SubscriptionModel.version))

        await UpsertSubscription(uow_factory, POLICY, CLOCK).execute(subscription_command())

        async with engine.connect() as connection:
            version_after = await connection.scalar(select(SubscriptionModel.version))
        assert version_after == version_before
        assert await count(engine, NotificationModel) == 3

    async def test_concurrent_creates_produce_one_subscription(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine
    ) -> None:
        upsert = UpsertSubscription(uow_factory, POLICY, CLOCK)

        results = await asyncio.gather(*(upsert.execute(subscription_command()) for _ in range(5)))

        assert sum(r.created for r in results) == 1
        assert await count(engine, SubscriptionModel) == 1
        assert await count(engine, NotificationModel) == 3

    async def test_concurrent_updates_are_serialized(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine, subscription: None
    ) -> None:
        upsert = UpsertSubscription(uow_factory, POLICY, CLOCK)
        dates = [EXPIRES + timedelta(days=d) for d in range(1, 6)]

        await asyncio.gather(
            *(upsert.execute(subscription_command(expected_expires_on=d)) for d in dates)
        )

        view = await GetSubscription(uow_factory).execute("sub-1")
        scheduled = [n for n in view.notifications if n.status is NotificationStatus.SCHEDULED]
        # Whichever update won last, exactly one consistent schedule remains for it
        assert view.expected_expires_on in dates
        assert [n.scheduled_for for n in scheduled] == [
            view.expected_expires_on - timedelta(days=3),
            view.expected_expires_on - timedelta(days=1),
            view.expected_expires_on,
        ]


class TestPayments:
    async def test_payment_and_event_are_stored_together(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine, subscription: None
    ) -> None:
        bind_correlation_id("req-42")
        try:
            result = await RegisterPayment(uow_factory, CLOCK).execute(payment_command())
        finally:
            clear_correlation_id()

        async with engine.connect() as connection:
            payment = (await connection.execute(select(PaymentModel))).one()
            event = (await connection.execute(select(OutboxEventModel))).one()
        assert payment.amount == Decimal("9.99")
        assert payment.id == result.payment.id
        assert event.id == result.event_id
        assert event.status == OutboxStatus.PENDING
        assert event.aggregate_id == str(payment.id)
        assert event.payload["amount"] == "9.99"
        assert event.headers == {"correlation_id": "req-42"}

    async def test_event_without_a_request_starts_its_own_trace(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine, subscription: None
    ) -> None:
        await RegisterPayment(uow_factory, CLOCK).execute(payment_command())

        async with engine.connect() as connection:
            event = (await connection.execute(select(OutboxEventModel))).one()
        assert len(event.headers["correlation_id"]) == 32

    async def test_failure_after_payment_insert_rolls_back_everything(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        subscription: None,
    ) -> None:
        class BrokenOutbox:
            async def add(self, message: OutboxMessage) -> None:
                raise RuntimeError("crash between payment and event")

        class CrashingUnitOfWork(SqlAlchemyUnitOfWork):
            async def __aenter__(self) -> "CrashingUnitOfWork":
                await super().__aenter__()
                self.outbox = BrokenOutbox()  # type: ignore[assignment]
                return self

        with pytest.raises(RuntimeError, match="crash"):
            await RegisterPayment(lambda: CrashingUnitOfWork(session_factory), CLOCK).execute(
                payment_command()
            )

        # The payment row was already flushed, but never committed
        assert await count(engine, PaymentModel) == 0
        assert await count(engine, OutboxEventModel) == 0

    async def test_idempotent_replay_and_conflict(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine, subscription: None
    ) -> None:
        register = RegisterPayment(uow_factory, CLOCK)
        first = await register.execute(payment_command())

        replay = await register.execute(payment_command(amount=Decimal("9.9900")))
        with pytest.raises(PaymentIdempotencyConflictError):
            await register.execute(payment_command(status=PaymentStatus.FAILED))

        assert replay.created is False
        assert replay.payment.id == first.payment.id
        assert replay.event_id == first.event_id
        assert await count(engine, PaymentModel) == 1
        assert await count(engine, OutboxEventModel) == 1

    async def test_concurrent_duplicates_store_one_payment(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine, subscription: None
    ) -> None:
        register = RegisterPayment(uow_factory, CLOCK)

        results: list[RegisterPaymentResult] = await asyncio.gather(
            *(register.execute(payment_command()) for _ in range(5))
        )

        assert sum(r.created for r in results) == 1
        assert len({r.payment.id for r in results}) == 1
        assert len({r.event_id for r in results}) == 1
        assert await count(engine, PaymentModel) == 1
        assert await count(engine, OutboxEventModel) == 1

    async def test_burst_of_retries_does_not_exhaust_the_pool(
        self, database_url: str, uow_factory: UnitOfWorkFactory, subscription: None
    ) -> None:
        """A provider retrying one webhook many times at once: each retry needs one connection.

        With a 2-connection pool, a retry that held one connection while waiting for a second
        one would deadlock the pool and time out.
        """
        await RegisterPayment(uow_factory, CLOCK).execute(payment_command())
        small_engine = create_async_engine(
            database_url, pool_size=2, max_overflow=0, pool_timeout=5
        )
        try:
            register = RegisterPayment(
                make_uow_factory(create_session_factory(small_engine)), CLOCK
            )
            results = await asyncio.gather(
                *(register.execute(payment_command()) for _ in range(20))
            )
        finally:
            await small_engine.dispose()

        assert not any(r.created for r in results)
        assert len({r.payment.id for r in results}) == 1

    async def test_unknown_subscription(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine
    ) -> None:
        with pytest.raises(SubscriptionNotFoundError):
            await RegisterPayment(uow_factory, CLOCK).execute(payment_command())

        assert await count(engine, PaymentModel) == 0

    async def test_status_reflects_delivery(
        self,
        uow_factory: UnitOfWorkFactory,
        session_factory: async_sessionmaker[AsyncSession],
        subscription: None,
    ) -> None:
        registered = await RegisterPayment(uow_factory, CLOCK).execute(payment_command())
        status = GetPaymentStatus(uow_factory)

        before = await status.by_id(registered.payment.id)
        assert before.event is not None
        assert before.event.status is OutboxStatus.PENDING
        assert before.notification is None

        # Simulate the relay and the notification worker
        now = CLOCK.now()
        async with uow_factory() as uow:
            payment = await uow.payments.get(registered.payment.id)
            assert payment is not None
            notification = Notification.for_payment(payment, now)
            notification.mark_sent(now)
            await uow.notifications.add_many([notification])
            await uow.commit()
        async with session_factory() as session, session.begin():
            await session.execute(
                update(OutboxEventModel).values(
                    status=OutboxStatus.PUBLISHED, published_at=now, attempts=1
                )
            )

        after = await status.by_provider_payment("pay-1")
        assert after.event is not None
        assert after.event.status is OutboxStatus.PUBLISHED
        assert after.event.published_at == now
        assert after.notification is not None
        assert after.notification.status is NotificationStatus.SENT
        assert after.notification.sent_at == now

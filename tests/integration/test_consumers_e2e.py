"""Full pipeline on real PostgreSQL and RabbitMQ:

payment → outbox → relay → RabbitMQ (topology from definitions.json) → consumers.

Retry queues use a 200 ms TTL here instead of 60 s, so retries finish within the test.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import aio_pika
import pytest
from faststream.rabbit import Channel, ExchangeType, RabbitBroker, RabbitExchange
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.dto import RegisterPaymentCommand, UpsertSubscriptionCommand
from app.application.ports import OutgoingNotification, SystemClock, UnitOfWorkFactory
from app.application.relay import OutboxRelay
from app.application.use_cases import (
    GetPaymentStatus,
    GetSubscription,
    RegisterPayment,
    UpsertSubscription,
)
from app.application.use_cases.consumers import DeliverNotification, RenewSubscriptionOnPayment
from app.domain.enums import NotificationStatus, OutboxStatus, PaymentStatus
from app.domain.policies import NotificationSchedulePolicy
from app.infrastructure.broker.rabbit import RabbitEventPublisher
from app.infrastructure.broker.topology import declare_topology, load_definitions, queue_names
from app.infrastructure.db.models import InboxMessageModel, OutboxEventModel
from app.workers.consumers import notifications, renewal

pytestmark = pytest.mark.integration

CLOCK = SystemClock()
POLICY = NotificationSchedulePolicy([3, 1, 0])
MAX_ATTEMPTS = 3
EXPIRES = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[OutgoingNotification] = []
        self.fail = False

    async def send(self, notification: OutgoingNotification) -> None:
        if self.fail:
            raise ConnectionError("channel down")
        self.sent.append(notification)


async def eventually(check: Callable[[], Awaitable[bool]], within: float = 15) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.1)


@pytest.fixture
async def topology(rabbitmq_url: str) -> dict[str, Any]:
    """Fresh queues from definitions.json, with fast retries."""
    definitions = load_definitions()
    for queue in definitions["queues"]:
        if "x-message-ttl" in queue["arguments"]:
            queue["arguments"]["x-message-ttl"] = 200

    connection = await aio_pika.connect(rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        for name in queue_names(definitions):
            await channel.queue_delete(name)

    broker = RabbitBroker(rabbitmq_url)
    await broker.connect()
    await declare_topology(broker, definitions)
    await broker.stop()
    return definitions


@pytest.fixture
def sender() -> RecordingSender:
    return RecordingSender()


@pytest.fixture
async def consumers(
    rabbitmq_url: str,
    topology: dict[str, Any],
    uow_factory: UnitOfWorkFactory,
    sender: RecordingSender,
) -> AsyncIterator[None]:
    """Both consumers running in this process, as the worker containers do."""
    broker = RabbitBroker(
        rabbitmq_url, default_channel=Channel(publisher_confirms=True, on_return_raises=True)
    )
    notifications.register(
        broker,
        DeliverNotification(uow_factory, sender, CLOCK),
        max_attempts=MAX_ATTEMPTS,
        prefetch=10,
    )
    renewal.register(
        broker,
        RenewSubscriptionOnPayment(uow_factory, POLICY, CLOCK),
        max_attempts=MAX_ATTEMPTS,
        prefetch=10,
    )
    await broker.start()
    yield
    await broker.stop()


@pytest.fixture
async def relay(
    rabbitmq_url: str, topology: dict[str, Any], uow_factory: UnitOfWorkFactory
) -> AsyncIterator[OutboxRelay]:
    broker = RabbitBroker(
        rabbitmq_url, default_channel=Channel(publisher_confirms=True, on_return_raises=True)
    )
    await broker.connect()
    # Same exchange as in production (declared by the topology fixture)
    exchange = RabbitExchange("billing.events", type=ExchangeType.TOPIC, durable=True)
    yield OutboxRelay(uow_factory, RabbitEventPublisher(broker, exchange), CLOCK)
    await broker.stop()


async def pay(
    uow_factory: UnitOfWorkFactory, status: PaymentStatus, provider_payment: str
) -> uuid.UUID:
    await UpsertSubscription(uow_factory, POLICY, CLOCK).execute(
        UpsertSubscriptionCommand(
            subscription_id="sub-1", user_id="user-1", day_count=30, expected_expires_on=EXPIRES
        )
    )
    result = await RegisterPayment(uow_factory, CLOCK).execute(
        RegisterPaymentCommand(
            subscription_id="sub-1",
            provider_payment=provider_payment,
            amount=Decimal("9.99"),
            currency="EUR",
            status=status,
        )
    )
    return result.payment.id


async def queue_depth(rabbitmq_url: str, name: str) -> int:
    connection = await aio_pika.connect(rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(name, passive=True)
        return int(queue.declaration_result.message_count or 0)


@pytest.mark.usefixtures("consumers")
async def test_successful_payment_notifies_user_and_renews_subscription(
    uow_factory: UnitOfWorkFactory, relay: OutboxRelay, sender: RecordingSender
) -> None:
    payment_id = await pay(uow_factory, PaymentStatus.SUCCEEDED, "pi_e2e_ok")

    assert (await relay.run_once()).published == 1

    status = GetPaymentStatus(uow_factory)

    async def notified() -> bool:
        view = await status.by_id(payment_id)
        return view.notification is not None and view.notification.sent_at is not None

    async def renewed() -> bool:
        view = await GetSubscription(uow_factory).execute("sub-1")
        return view.expected_expires_on == EXPIRES + timedelta(days=30)

    await eventually(notified)
    await eventually(renewed)

    view = await status.by_id(payment_id)
    assert view.event is not None
    assert view.event.status is OutboxStatus.PUBLISHED
    assert view.notification is not None
    assert view.notification.status is NotificationStatus.SENT
    assert [n.event_name.value for n in sender.sent] == ["payment.succeeded"]

    subscription = await GetSubscription(uow_factory).execute("sub-1")
    scheduled = [n for n in subscription.notifications if n.status is NotificationStatus.SCHEDULED]
    assert [n.scheduled_for for n in scheduled][-1] == EXPIRES + timedelta(days=30)


@pytest.mark.usefixtures("consumers")
async def test_redelivered_event_changes_nothing(
    uow_factory: UnitOfWorkFactory,
    relay: OutboxRelay,
    sender: RecordingSender,
    engine: AsyncEngine,
) -> None:
    await pay(uow_factory, PaymentStatus.SUCCEEDED, "pi_e2e_dup")
    await relay.run_once()

    async def inbox_count() -> int:
        async with engine.connect() as connection:
            return int(
                await connection.scalar(select(func.count()).select_from(InboxMessageModel)) or 0
            )

    async def both_processed() -> bool:
        return await inbox_count() == 2

    await eventually(both_processed)

    # Simulate a relay crash after publishing but before marking: the event goes out again
    async with engine.begin() as connection:
        await connection.execute(update(OutboxEventModel).values(status=OutboxStatus.PENDING))
    assert (await relay.run_once()).published == 1
    await asyncio.sleep(1.5)

    subscription = await GetSubscription(uow_factory).execute("sub-1")
    assert subscription.expected_expires_on == EXPIRES + timedelta(days=30)  # renewed once
    assert len(sender.sent) == 1  # notified once
    assert await inbox_count() == 2


@pytest.mark.usefixtures("consumers")
async def test_failing_channel_is_retried_then_parked(
    uow_factory: UnitOfWorkFactory,
    relay: OutboxRelay,
    sender: RecordingSender,
    rabbitmq_url: str,
) -> None:
    sender.fail = True
    payment_id = await pay(uow_factory, PaymentStatus.FAILED, "pi_e2e_fail")
    await relay.run_once()

    async def parked() -> bool:
        return await queue_depth(rabbitmq_url, "notifications.send.parking") == 1

    await eventually(parked)

    view = await GetPaymentStatus(uow_factory).by_id(payment_id)
    assert view.notification is not None
    assert view.notification.status is NotificationStatus.FAILED
    assert view.notification.attempts == MAX_ATTEMPTS
    assert view.notification.last_error == "ConnectionError('channel down')"
    assert await queue_depth(rabbitmq_url, "notifications.send") == 0


@pytest.mark.usefixtures("consumers")
async def test_poison_message_is_parked_immediately(rabbitmq_url: str) -> None:
    connection = await aio_pika.connect(rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        exchange = await channel.get_exchange("billing.events")
        await exchange.publish(
            aio_pika.Message(
                body=json.dumps({"unexpected": True}).encode(), message_id=str(uuid.uuid4())
            ),
            routing_key="payment.succeeded",
        )

    async def both_parked() -> bool:
        return (
            await queue_depth(rabbitmq_url, "notifications.send.parking") == 1
            and await queue_depth(rabbitmq_url, "subscriptions.renewal.parking") == 1
        )

    await eventually(both_parked, within=5)

"""Outbox relay against real PostgreSQL and real RabbitMQ.

RabbitMQ comes from TEST_RABBITMQ_URL if set, otherwise from testcontainers.
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aio_pika.abc import AbstractIncomingMessage, AbstractRobustQueue
from faststream.rabbit import Channel, ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.dto import PendingEvent, RegisterPaymentCommand, UpsertSubscriptionCommand
from app.application.ports import SystemClock, UnitOfWorkFactory
from app.application.relay import OutboxRelay, RelaySettings
from app.application.use_cases import RegisterPayment, UpsertSubscription
from app.domain.enums import OutboxStatus, PaymentStatus
from app.domain.policies import NotificationSchedulePolicy
from app.infrastructure.broker.rabbit import RabbitEventPublisher
from app.infrastructure.db.models import OutboxEventModel
from app.infrastructure.observability.correlation import bind_correlation_id, clear_correlation_id

pytestmark = pytest.mark.integration

CLOCK = SystemClock()


@pytest.fixture(scope="session")
def rabbitmq_url() -> Iterator[str]:
    url = os.environ.get("TEST_RABBITMQ_URL")
    if url:
        yield url
        return

    from testcontainers.rabbitmq import RabbitMqContainer

    with RabbitMqContainer("rabbitmq:3.13-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(container.port)
        yield f"amqp://guest:guest@{host}:{port}/"


@pytest.fixture
async def broker(rabbitmq_url: str) -> AsyncIterator[RabbitBroker]:
    broker = RabbitBroker(
        rabbitmq_url, default_channel=Channel(publisher_confirms=True, on_return_raises=True)
    )
    await broker.connect()
    yield broker
    await broker.stop()


@pytest.fixture
async def exchange_and_queue(
    broker: RabbitBroker,
) -> AsyncIterator[tuple[RabbitExchange, AbstractRobustQueue]]:
    """A private exchange and queue per test, bound like notifications.send."""
    suffix = uuid.uuid4().hex[:8]
    exchange = RabbitExchange(f"test.events.{suffix}", type=ExchangeType.TOPIC, durable=False)
    await broker.declare_exchange(exchange)
    queue = await broker.declare_queue(RabbitQueue(f"test.queue.{suffix}", auto_delete=True))
    await queue.bind(exchange.name, routing_key="payment.*")
    yield exchange, queue
    await queue.delete(if_unused=False, if_empty=False)


async def read_messages(queue: AbstractRobustQueue, expected: int) -> list[AbstractIncomingMessage]:
    messages: list[AbstractIncomingMessage] = []
    for _ in range(50):
        message = await queue.get(no_ack=True, fail=False)
        if message is None:
            if len(messages) >= expected:
                break
            await asyncio.sleep(0.1)
            continue
        messages.append(message)
    return messages


async def register_payment(uow_factory: UnitOfWorkFactory, provider_payment: str) -> None:
    await UpsertSubscription(uow_factory, NotificationSchedulePolicy([3, 1, 0]), CLOCK).execute(
        UpsertSubscriptionCommand(
            subscription_id="sub-1",
            user_id="user-1",
            day_count=30,
            expected_expires_on=datetime.now(UTC) + timedelta(days=30),
        )
    )
    await RegisterPayment(uow_factory, CLOCK).execute(
        RegisterPaymentCommand(
            subscription_id="sub-1",
            provider_payment=provider_payment,
            amount=Decimal("9.99"),
            currency="EUR",
            status=PaymentStatus.SUCCEEDED,
        )
    )


async def outbox_rows(engine: AsyncEngine) -> list[OutboxEventModel]:
    async with engine.connect() as connection:
        return list((await connection.execute(select(OutboxEventModel))).all())  # type: ignore[arg-type]


async def test_payment_event_reaches_the_queue(
    uow_factory: UnitOfWorkFactory,
    engine: AsyncEngine,
    broker: RabbitBroker,
    exchange_and_queue: tuple[RabbitExchange, AbstractRobustQueue],
) -> None:
    exchange, queue = exchange_and_queue
    bind_correlation_id("req-relay-1")
    try:
        await register_payment(uow_factory, "pi_relay_1")
    finally:
        clear_correlation_id()
    relay = OutboxRelay(uow_factory, RabbitEventPublisher(broker, exchange), CLOCK)

    result = await relay.run_once()

    assert (result.published, result.failed) == (1, 0)
    [row] = await outbox_rows(engine)
    assert row.status == OutboxStatus.PUBLISHED
    assert row.published_at is not None
    assert row.attempts == 1

    [message] = await read_messages(queue, expected=1)
    assert message.message_id == str(row.id)
    assert message.correlation_id == "req-relay-1"
    assert message.type == "payment.succeeded"
    assert message.delivery_mode == 2  # persistent
    assert message.headers["event_type"] == "payment.succeeded"
    body = json.loads(message.body)
    assert body["provider_payment"] == "pi_relay_1"
    assert body["amount"] == "9.99"


async def test_unroutable_event_is_retried_not_lost(
    uow_factory: UnitOfWorkFactory, engine: AsyncEngine, broker: RabbitBroker
) -> None:
    await register_payment(uow_factory, "pi_relay_2")
    # Exchange without any bound queue: the broker returns the message
    orphan = RabbitExchange(f"test.orphan.{uuid.uuid4().hex[:8]}", type=ExchangeType.TOPIC)
    await broker.declare_exchange(orphan)
    relay = OutboxRelay(uow_factory, RabbitEventPublisher(broker, orphan), CLOCK)

    result = await relay.run_once()

    assert (result.published, result.failed) == (0, 1)
    [row] = await outbox_rows(engine)
    assert row.status == OutboxStatus.PENDING
    assert row.attempts == 1
    assert "PublishError" in (row.last_error or "")


async def test_concurrent_relays_publish_each_event_once(
    uow_factory: UnitOfWorkFactory, engine: AsyncEngine
) -> None:
    for i in range(20):
        await register_payment(uow_factory, f"pi_concurrent_{i}")

    class SlowRecorder:
        def __init__(self) -> None:
            self.ids: list[uuid.UUID] = []

        async def publish(self, event: PendingEvent) -> None:
            await asyncio.sleep(0.01)  # keep row locks held while others try to claim
            self.ids.append(event.id)

    recorders = [SlowRecorder() for _ in range(4)]
    relays = [
        OutboxRelay(uow_factory, recorder, CLOCK, RelaySettings(batch_size=5))
        for recorder in recorders
    ]

    for _ in range(3):
        await asyncio.gather(*(relay.run_once() for relay in relays))

    published = [event_id for recorder in recorders for event_id in recorder.ids]
    assert len(published) == 20
    assert len(set(published)) == 20  # no event was published twice
    assert sum(1 for recorder in recorders if recorder.ids) > 1  # work was shared
    assert all(row.status == OutboxStatus.PUBLISHED for row in await outbox_rows(engine))

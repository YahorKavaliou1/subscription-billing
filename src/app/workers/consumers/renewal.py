"""Renewal consumer: `python -m app.workers.consumers.renewal`.

Consumes `subscriptions.renewal` (`payment.succeeded`): extends the subscription by
`day_count` days and reschedules its reminders.
"""

import asyncio
from typing import Any

from faststream import AckPolicy
from faststream.rabbit import Channel, RabbitBroker, RabbitQueue
from faststream.rabbit.annotations import RabbitMessage

from app.application.ports import SystemClock
from app.application.use_cases.consumers import RenewSubscriptionOnPayment
from app.config import Settings, get_settings
from app.contracts.events import PaymentResultEvent
from app.domain.policies import NotificationSchedulePolicy
from app.infrastructure.broker.rabbit import create_broker
from app.infrastructure.db.session import create_engine, create_session_factory
from app.infrastructure.db.uow import make_uow_factory
from app.infrastructure.observability.logging import configure_logging
from app.infrastructure.observability.metrics import start_metrics_server
from app.workers.consumers.common import ReliableConsumer, run_until_stopped

QUEUE = "subscriptions.renewal"


def register(
    broker: RabbitBroker,
    renew: RenewSubscriptionOnPayment,
    *,
    max_attempts: int,
    prefetch: int,
    queue: str = QUEUE,
) -> None:
    consumer = ReliableConsumer(broker, queue, max_attempts)

    async def process(
        event_type: str, message_id: str, payload: dict[str, Any], final: bool
    ) -> str:
        return await renew.execute(message_id, PaymentResultEvent.model_validate(payload))

    @broker.subscriber(
        RabbitQueue(queue, declare=False),
        ack_policy=AckPolicy.MANUAL,
        channel=Channel(prefetch_count=prefetch),
    )
    async def on_message(body: Any, message: RabbitMessage) -> None:
        # `body` is FastStream's decoded copy; the handler validates raw bytes itself
        await consumer.handle(message, process)


def build(settings: Settings) -> tuple[RabbitBroker, Any]:
    engine = create_engine(settings)
    broker = create_broker(settings)
    renew = RenewSubscriptionOnPayment(
        make_uow_factory(create_session_factory(engine)),
        NotificationSchedulePolicy(settings.notification_offsets_days),
        SystemClock(),
    )
    register(
        broker,
        renew,
        max_attempts=settings.consumer_max_delivery_attempts,
        prefetch=settings.consumer_prefetch_count,
    )
    return broker, engine


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    broker, engine = build(settings)
    start_metrics_server(settings.metrics_port)
    try:
        await run_until_stopped(broker, "renewal_worker")
    finally:
        await engine.dispose()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()

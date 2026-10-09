"""Notification consumer: `python -m app.workers.consumers.notifications`.

Consumes `notifications.send`: payment results (`payment.*`) and due reminders
(`notification.*`), and delivers them through the configured NotificationSender.
"""

import asyncio
from typing import Any

from faststream import AckPolicy
from faststream.rabbit import Channel, RabbitBroker, RabbitQueue
from faststream.rabbit.annotations import RabbitMessage

from app.application.ports import SystemClock
from app.application.use_cases.consumers import DeliverNotification
from app.config import Settings, get_settings
from app.contracts.events import PaymentResultEvent, ReminderDueEvent
from app.infrastructure.broker.rabbit import create_broker
from app.infrastructure.db.session import create_engine, create_session_factory
from app.infrastructure.db.uow import make_uow_factory
from app.infrastructure.observability.logging import configure_logging
from app.infrastructure.observability.metrics import start_metrics_server
from app.infrastructure.senders import create_sender
from app.workers.consumers.common import PoisonMessageError, ReliableConsumer, run_until_stopped

QUEUE = "notifications.send"


def register(
    broker: RabbitBroker,
    deliver: DeliverNotification,
    *,
    max_attempts: int,
    prefetch: int,
    queue: str = QUEUE,
) -> None:
    consumer = ReliableConsumer(broker, queue, max_attempts)

    async def process(
        event_type: str, message_id: str, payload: dict[str, Any], final: bool
    ) -> str:
        if event_type.startswith("payment."):
            event = PaymentResultEvent.model_validate(payload)
            return await deliver.for_payment(message_id, event, final_attempt=final)
        if event_type.startswith("notification."):
            reminder = ReminderDueEvent.model_validate(payload)
            return await deliver.for_reminder(message_id, reminder, final_attempt=final)
        raise PoisonMessageError(f"unexpected event type {event_type!r}")

    # declare=False: the queue (quorum, dead-lettering) is defined in definitions.json
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
    deliver = DeliverNotification(
        make_uow_factory(create_session_factory(engine)), create_sender(settings), SystemClock()
    )
    register(
        broker,
        deliver,
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
        await run_until_stopped(broker, "notification_worker")
    finally:
        await engine.dispose()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()

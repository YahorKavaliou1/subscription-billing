"""RabbitMQ adapter (FastStream / aio-pika) for publishing outbox events."""

import asyncio

import aiormq.exceptions
from faststream.rabbit import Channel, ExchangeType, RabbitBroker, RabbitExchange

from app.application.dto import PendingEvent
from app.application.ports import BrokerUnavailableError
from app.config import Settings
from app.infrastructure.observability.correlation import CORRELATION_ID_KEY

PUBLISH_TIMEOUT_SECONDS = 10.0

# Failures of the connection itself, as opposed to the broker rejecting one message
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    OSError,
    TimeoutError,
    asyncio.TimeoutError,
    aiormq.exceptions.AMQPConnectionError,
    aiormq.exceptions.ChannelInvalidStateError,
)


def events_exchange(settings: Settings) -> RabbitExchange:
    return RabbitExchange(settings.rabbitmq_exchange, type=ExchangeType.TOPIC, durable=True)


def create_broker(settings: Settings) -> RabbitBroker:
    return RabbitBroker(
        settings.rabbitmq_url.get_secret_value(),
        # publisher_confirms: publish() returns only after the broker stored the message.
        # on_return_raises: a message no queue is bound for raises instead of vanishing.
        default_channel=Channel(publisher_confirms=True, on_return_raises=True),
    )


class RabbitEventPublisher:
    def __init__(
        self,
        broker: RabbitBroker,
        exchange: RabbitExchange,
        timeout: float = PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        self._broker = broker
        self._exchange = exchange
        self._timeout = timeout

    async def publish(self, event: PendingEvent) -> None:
        headers = {
            **event.headers,
            "event_type": event.event_type,
            "schema_version": event.payload.get("schema_version", 1),
        }
        correlation_id = event.headers.get(CORRELATION_ID_KEY)
        try:
            await self._broker.publish(
                event.payload,
                exchange=self._exchange,
                routing_key=event.routing_key,
                mandatory=True,
                persist=True,  # delivery_mode=2: survives a broker restart
                message_id=str(event.id),  # consumers deduplicate by it
                correlation_id=str(correlation_id) if correlation_id else None,
                message_type=event.event_type,
                headers=headers,
                content_type="application/json",
                timestamp=event.created_at,
                timeout=self._timeout,
            )
        except _CONNECTION_ERRORS as exc:
            raise BrokerUnavailableError(repr(exc)) from exc

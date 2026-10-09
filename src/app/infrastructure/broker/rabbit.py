"""RabbitMQ adapter (FastStream / aio-pika) for publishing outbox events."""

import asyncio
import logging
from typing import Any

import aiormq.exceptions
import structlog
from faststream.rabbit import Channel, ExchangeType, RabbitBroker, RabbitExchange

from app.application.dto import PendingEvent
from app.application.ports import BrokerUnavailableError
from app.config import Settings
from app.infrastructure.observability.correlation import CORRELATION_ID_KEY
from app.infrastructure.observability.logging import get_logger

PUBLISH_TIMEOUT_SECONDS = 10.0
log = get_logger("app.broker")

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
        # A plain stdlib logger instead of FastStream's own colored handler, so its
        # records go through configure_logging() and come out as JSON
        logger=logging.getLogger("faststream.access.rabbit"),
        # FastStream's per-message "Received"/"Processed" lines duplicate our
        # message.* logs; keep them for APP_LOG_LEVEL=DEBUG only
        log_level=logging.DEBUG,
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
        raw_correlation_id = event.headers.get(CORRELATION_ID_KEY)
        correlation_id = str(raw_correlation_id) if raw_correlation_id else None
        context = {
            "event_id": str(event.id),
            "event_type": event.event_type,
            "attempt": event.attempts + 1,
        }
        # Log under the id of the request that produced the event: the same id is in
        # the API access log and in the consumers' logs
        with structlog.contextvars.bound_contextvars(
            **({CORRELATION_ID_KEY: correlation_id} if correlation_id else {})
        ):
            try:
                await self._send(event, headers, correlation_id)
            except BrokerUnavailableError:
                raise  # one warning per batch is logged by the relay worker
            except Exception as exc:
                log.warning("event.publish_rejected", error=repr(exc), **context)
                raise
            log.info("event.published", routing_key=event.routing_key, **context)

    async def _send(
        self, event: PendingEvent, headers: dict[str, Any], correlation_id: str | None
    ) -> None:
        try:
            await self._broker.publish(
                event.payload,
                exchange=self._exchange,
                routing_key=event.routing_key,
                mandatory=True,
                persist=True,  # delivery_mode=2: survives a broker restart
                message_id=str(event.id),  # consumers deduplicate by it
                correlation_id=correlation_id,
                message_type=event.event_type,
                headers=headers,
                content_type="application/json",
                timestamp=event.created_at,
                timeout=self._timeout,
            )
        except _CONNECTION_ERRORS as exc:
            raise BrokerUnavailableError(repr(exc)) from exc

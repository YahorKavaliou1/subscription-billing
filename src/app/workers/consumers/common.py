"""Reliable message handling shared by all consumers.

Retry flow (topology in infra/rabbitmq/definitions.json):

    main queue --nack--> billing.retry --> <queue>.retry (TTL) --expired--> main queue
                 ...after max attempts: copy to billing.parking --> <queue>.parking, ack

The attempt number is derived from the `x-death` header RabbitMQ adds on each
dead-lettering, so no extra state is needed.
"""

import asyncio
import json
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any

from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange
from faststream.rabbit.message import RabbitMessage
from pydantic import ValidationError

from app.infrastructure.observability.correlation import bind_correlation_id, clear_correlation_id
from app.infrastructure.observability.logging import get_logger
from app.infrastructure.observability.metrics import (
    CONSUMER_MESSAGES,
    CONSUMER_PROCESSING_DURATION,
)

PARKING_EXCHANGE = "billing.parking"
log = get_logger("app.workers.consumers")

# (event_type, message_id, payload, final_attempt) -> outcome label
Processor = Callable[[str, str, dict[str, Any], bool], Awaitable[str]]


class PoisonMessageError(Exception):
    """The message can never be processed (bad JSON, unknown type): park it right away."""


def event_type_of(message_type: str | None, headers: Mapping[str, Any], routing_key: str) -> str:
    """The event type survives retries; the routing key does not.

    After a round trip through the retry queue RabbitMQ delivers the message with
    x-dead-letter-routing-key (the queue name), so dispatching by routing key would
    break on the second attempt.
    """
    return message_type or str(headers.get("event_type") or "") or routing_key


def delivery_attempt(headers: Mapping[str, Any], queue: str) -> int:
    """1 for the first delivery, +1 for every earlier rejection from this queue."""
    rejections = 0
    for death in headers.get("x-death") or []:
        if death.get("queue") == queue and death.get("reason") == "rejected":
            rejections += int(death.get("count", 0))
    return rejections + 1


class ReliableConsumer:
    def __init__(self, broker: RabbitBroker, queue: str, max_attempts: int) -> None:
        self._broker = broker
        self._queue = queue
        self._max_attempts = max_attempts
        self._parking = RabbitExchange(PARKING_EXCHANGE, type=ExchangeType.DIRECT, declare=False)

    async def handle(self, message: RabbitMessage, process: Processor) -> None:
        raw = message.raw_message
        headers = dict(raw.headers or {})
        attempt = delivery_attempt(headers, self._queue)
        final = attempt >= self._max_attempts
        event_type = event_type_of(raw.type, headers, raw.routing_key or "")
        bind_correlation_id(raw.correlation_id or None)
        context = {"message_id": raw.message_id, "event_type": event_type, "attempt": attempt}
        started = time.perf_counter()
        result = "processed"
        try:
            if not raw.message_id:
                raise PoisonMessageError("message without message_id")
            try:
                payload = json.loads(raw.body)
            except ValueError as exc:
                raise PoisonMessageError(f"invalid JSON: {exc}") from exc
            outcome = await process(event_type, raw.message_id, payload, final)
        except (PoisonMessageError, ValidationError) as exc:
            await self._park(message, headers, repr(exc))
            result = "parked"
            log.error("message.parked", reason="poison", error=repr(exc), **context)
        except Exception as exc:
            if final:
                await self._park(message, headers, repr(exc))
                result = "parked"
                log.error("message.parked", reason="max_attempts", error=repr(exc), **context)
            else:
                # Dead-lettered to the retry queue, comes back after its TTL
                await message.nack(requeue=False)
                result = "retry"
                log.warning("message.retry_scheduled", error=repr(exc), **context)
        else:
            await message.ack()
            result = str(outcome)  # processed / duplicate / skipped
            log.info("message.processed", outcome=outcome, **context)
        finally:
            CONSUMER_MESSAGES.labels(queue=self._queue, result=result).inc()
            CONSUMER_PROCESSING_DURATION.labels(queue=self._queue).observe(
                time.perf_counter() - started
            )
            clear_correlation_id()

    async def _park(self, message: RabbitMessage, headers: dict[str, Any], error: str) -> None:
        raw = message.raw_message
        await self._broker.publish(
            raw.body,
            exchange=self._parking,
            routing_key=self._queue,
            persist=True,
            message_id=raw.message_id,
            correlation_id=raw.correlation_id,
            message_type=raw.type,
            content_type=raw.content_type,
            headers={
                **headers,
                "x-parked-error": error[:1000],
                "event_type": event_type_of(raw.type, headers, raw.routing_key or ""),
            },
        )
        # Only after the copy is safely in the parking queue
        await message.ack()


async def run_until_stopped(broker: RabbitBroker, name: str) -> None:
    """Connect (with retries), consume until SIGTERM/SIGINT, then stop gracefully."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    delay = 1.0
    while not stopping.is_set():
        try:
            await broker.start()
            break
        except Exception as exc:
            log.warning(f"{name}.broker_connect_failed", error=repr(exc), retry_in=delay)
            with suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=delay)
            delay = min(delay * 2, 30.0)

    if not stopping.is_set():
        log.info(f"{name}.started")
        await stopping.wait()
    log.info(f"{name}.stopping")
    # Waits for in-flight handlers before closing the connection
    await broker.stop()

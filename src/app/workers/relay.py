"""Outbox relay process: `python -m app.workers.relay`.

Polls the outbox table and publishes pending events to RabbitMQ. Several instances
can run at once: rows are claimed with FOR UPDATE SKIP LOCKED.
"""

import asyncio
import signal
from contextlib import suppress

from faststream.rabbit import RabbitBroker

from app.application.ports import SystemClock
from app.application.relay import OutboxRelay, RelayResult, RelaySettings
from app.config import Settings, get_settings
from app.infrastructure.broker.rabbit import (
    RabbitEventPublisher,
    create_broker,
    events_exchange,
)
from app.infrastructure.db.session import create_engine, create_session_factory
from app.infrastructure.db.uow import make_uow_factory
from app.infrastructure.observability.logging import configure_logging, get_logger

log = get_logger("app.workers.relay")

BROKER_RETRY_MAX_SECONDS = 30.0


class RelayWorker:
    def __init__(self, relay: OutboxRelay, settings: Settings) -> None:
        self._relay = relay
        self._settings = settings
        self._stopping = asyncio.Event()

    @property
    def stopping(self) -> asyncio.Event:
        return self._stopping

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        broker_backoff = 1.0
        while not self._stopping.is_set():
            try:
                result = await self._relay.run_once()
            except Exception:
                # Database hiccup or a bug: log and keep the process alive
                log.exception("relay.iteration_failed")
                await self._sleep(self._settings.relay_poll_interval_seconds * 10)
                continue

            self._log(result)
            if result.broker_unavailable:
                await self._sleep(broker_backoff)
                broker_backoff = min(broker_backoff * 2, BROKER_RETRY_MAX_SECONDS)
                continue
            broker_backoff = 1.0
            # A full batch means more events are waiting: continue without a pause
            if result.claimed < self._settings.relay_batch_size:
                await self._sleep(self._settings.relay_poll_interval_seconds)

    async def _sleep(self, seconds: float) -> None:
        # Wakes up immediately on shutdown
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    @staticmethod
    def _log(result: RelayResult) -> None:
        if result.broker_unavailable:
            log.warning("relay.broker_unavailable", published=result.published)
        if result.published or result.failed:
            log.info(
                "relay.batch",
                claimed=result.claimed,
                published=result.published,
                failed=result.failed,
                dead=result.dead,
            )
        if result.dead:
            log.error("relay.events_dead", count=result.dead)


async def connect_with_retry(broker: RabbitBroker, stopping: asyncio.Event) -> bool:
    delay = 1.0
    while not stopping.is_set():
        try:
            await broker.connect()
            return True
        except Exception as exc:
            log.warning("relay.broker_connect_failed", error=repr(exc), retry_in=delay)
            with suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=delay)
            delay = min(delay * 2, BROKER_RETRY_MAX_SECONDS)
    return False


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)

    engine = create_engine(settings)
    broker = create_broker(settings)
    exchange = events_exchange(settings)
    relay = OutboxRelay(
        make_uow_factory(create_session_factory(engine)),
        RabbitEventPublisher(broker, exchange),
        SystemClock(),
        RelaySettings(
            batch_size=settings.relay_batch_size,
            max_attempts=settings.relay_max_attempts,
            backoff_base_seconds=settings.relay_backoff_base_seconds,
            backoff_max_seconds=settings.relay_backoff_max_seconds,
        ),
    )
    worker = RelayWorker(relay, settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)

    try:
        if await connect_with_retry(broker, worker.stopping):
            # Idempotent: same definition as in infra/rabbitmq/definitions.json
            await broker.declare_exchange(exchange)
            log.info("relay.started", exchange=exchange.name)
            await worker.run()
    finally:
        log.info("relay.stopping")
        await broker.stop()
        await engine.dispose()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()

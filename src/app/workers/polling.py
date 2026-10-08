"""Base for workers that poll the database: outbox relay and reminder scheduler.

Unlike consumers (RabbitMQ pushes messages to them), these workers have to look for
work themselves, so they share one loop: iterate, sleep, survive errors, stop on SIGTERM.
"""

import asyncio
import signal
from abc import ABC, abstractmethod
from contextlib import suppress

from app.infrastructure.observability.logging import get_logger

log = get_logger("app.workers")

ERROR_PAUSE_SECONDS = 5.0


class PollingWorker(ABC):
    name: str = "worker"

    def __init__(self) -> None:
        self._stopping = asyncio.Event()

    @property
    def stopping(self) -> asyncio.Event:
        return self._stopping

    def stop(self) -> None:
        self._stopping.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop)

    @abstractmethod
    async def iterate(self) -> float:
        """Do one unit of work; return how many seconds to wait before the next one (0 = none)."""

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                delay = await self.iterate()
            except Exception:
                # Database hiccup or a bug: log it and keep the process alive
                log.exception(f"{self.name}.iteration_failed")
                delay = ERROR_PAUSE_SECONDS
            if delay > 0:
                await self.sleep(delay)

    async def sleep(self, seconds: float) -> None:
        """Pause that ends immediately on shutdown."""
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

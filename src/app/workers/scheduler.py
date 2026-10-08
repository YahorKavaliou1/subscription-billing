"""Reminder scheduler process: `python -m app.workers.scheduler`.

Polls the notifications table for due reminders and hands them to the outbox.
Several instances can run at once: rows are claimed with FOR UPDATE SKIP LOCKED.
"""

import asyncio

from app.application.ports import SystemClock
from app.application.scheduler import ReminderScheduler
from app.config import Settings, get_settings
from app.infrastructure.db.session import create_engine, create_session_factory
from app.infrastructure.db.uow import make_uow_factory
from app.infrastructure.observability.logging import configure_logging, get_logger
from app.workers.polling import PollingWorker

log = get_logger("app.workers.scheduler")


class SchedulerWorker(PollingWorker):
    name = "scheduler"

    def __init__(self, scheduler: ReminderScheduler, settings: Settings) -> None:
        super().__init__()
        self._scheduler = scheduler
        self._settings = settings

    async def iterate(self) -> float:
        result = await self._scheduler.run_once()
        if result.enqueued:
            log.info(
                "scheduler.batch",
                enqueued=result.enqueued,
                expired_subscriptions=result.expired_subscriptions,
            )
        # A full batch means more reminders are due: continue without a pause
        if result.enqueued >= self._settings.scheduler_batch_size:
            return 0
        return self._settings.scheduler_poll_interval_seconds


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)

    engine = create_engine(settings)
    scheduler = ReminderScheduler(
        make_uow_factory(create_session_factory(engine)),
        SystemClock(),
        batch_size=settings.scheduler_batch_size,
    )
    worker = SchedulerWorker(scheduler, settings)
    worker.install_signal_handlers()
    try:
        log.info("scheduler.started", poll_interval=settings.scheduler_poll_interval_seconds)
        await worker.run()
    finally:
        log.info("scheduler.stopping")
        await engine.dispose()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()

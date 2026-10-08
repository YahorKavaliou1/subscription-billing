"""Reminder scheduler against real PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.dto import UpsertSubscriptionCommand
from app.application.ports import SystemClock, UnitOfWorkFactory
from app.application.scheduler import ReminderScheduler
from app.application.use_cases import GetSubscription, UpsertSubscription
from app.domain.enums import NotificationStatus
from app.domain.policies import NotificationSchedulePolicy
from app.infrastructure.db.models import OutboxEventModel

pytestmark = pytest.mark.integration

POLICY = NotificationSchedulePolicy([3, 1, 0])


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


async def create_subscriptions(uow_factory: UnitOfWorkFactory, count: int) -> datetime:
    expires = (datetime.now(UTC) + timedelta(days=10)).replace(microsecond=0)
    upsert = UpsertSubscription(uow_factory, POLICY, SystemClock())
    for i in range(count):
        await upsert.execute(
            UpsertSubscriptionCommand(
                subscription_id=f"sub-{i}",
                user_id="user-1",
                day_count=30,
                expected_expires_on=expires,
            )
        )
    return expires


async def test_due_reminders_are_enqueued(
    uow_factory: UnitOfWorkFactory, engine: AsyncEngine
) -> None:
    expires = await create_subscriptions(uow_factory, 1)
    # Moment when the T-3d reminder is due, T-1d is not
    scheduler = ReminderScheduler(uow_factory, FixedClock(expires - timedelta(days=2)))

    result = await scheduler.run_once()

    assert result.enqueued == 1
    view = await GetSubscription(uow_factory).execute("sub-0")
    assert [n.status for n in view.notifications] == [
        NotificationStatus.ENQUEUED,
        NotificationStatus.SCHEDULED,
        NotificationStatus.SCHEDULED,
    ]
    async with engine.connect() as connection:
        event_type = await connection.scalar(select(OutboxEventModel.event_type))
    assert event_type == "notification.subscription.expiring_soon"


async def test_concurrent_schedulers_enqueue_each_reminder_once(
    uow_factory: UnitOfWorkFactory, engine: AsyncEngine
) -> None:
    expires = await create_subscriptions(uow_factory, 10)  # 30 reminders
    later = FixedClock(expires + timedelta(seconds=1))  # all of them are due
    schedulers = [ReminderScheduler(uow_factory, later, batch_size=4) for _ in range(4)]

    total = 0
    for _ in range(5):
        results = await asyncio.gather(*(s.run_once() for s in schedulers))
        total += sum(r.enqueued for r in results)

    async with engine.connect() as connection:
        events = await connection.scalar(select(func.count()).select_from(OutboxEventModel))
        distinct = await connection.scalar(
            select(func.count(func.distinct(OutboxEventModel.aggregate_id)))
        )
    assert total == 30
    assert events == 30
    assert distinct == 30  # no reminder got two events

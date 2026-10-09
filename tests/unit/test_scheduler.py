"""Reminder scheduler and the polling loop, on in-memory fakes."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.application.scheduler import ReminderScheduler
from app.domain.entities import Subscription
from app.domain.enums import NotificationEvent, NotificationStatus, SubscriptionStatus
from app.domain.policies import NotificationSchedulePolicy
from app.workers.polling import PollingWorker
from tests.unit.fakes import FakeClock, FakeDatabase

START = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
EXPIRES = START + timedelta(days=10)
POLICY = NotificationSchedulePolicy([3, 1, 0])


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def db(clock: FakeClock) -> FakeDatabase:
    database = FakeDatabase(clock)
    subscription = Subscription(
        id="sub-1", user_id="user-1", day_count=30, expected_expires_on=EXPIRES
    )
    database.state.subscriptions[subscription.id] = subscription
    for notification in POLICY.reschedule(subscription, [], START).to_create:
        database.state.notifications[notification.id] = notification
    return database


def statuses(db: FakeDatabase) -> list[tuple[NotificationEvent, NotificationStatus]]:
    ordered = sorted(db.state.notifications.values(), key=lambda n: n.scheduled_for)
    return [(n.event_name, n.status) for n in ordered]


async def test_nothing_is_due_yet(db: FakeDatabase, clock: FakeClock) -> None:
    result = await ReminderScheduler(db.uow, clock).run_once()

    assert result.enqueued == 0
    assert db.state.outbox == []


async def test_due_reminder_is_enqueued_with_an_event(db: FakeDatabase, clock: FakeClock) -> None:
    clock.advance(timedelta(days=7))  # T-3d has come

    result = await ReminderScheduler(db.uow, clock).run_once()

    assert result.enqueued == 1
    assert statuses(db) == [
        (NotificationEvent.EXPIRING_SOON, NotificationStatus.ENQUEUED),
        (NotificationEvent.EXPIRING_SOON, NotificationStatus.SCHEDULED),
        (NotificationEvent.EXPIRED, NotificationStatus.SCHEDULED),
    ]
    [event] = db.state.outbox
    reminder = min(db.state.notifications.values(), key=lambda n: n.scheduled_for)
    assert event.message.event_type == "notification.subscription.expiring_soon"
    assert event.message.routing_key == "notification.subscription.expiring_soon"
    assert event.message.aggregate_type == "notification"
    assert event.message.payload == {
        "schema_version": 1,
        "notification_id": str(reminder.id),
        "subscription_id": "sub-1",
        "event_name": "subscription.expiring_soon",
        "scheduled_for": reminder.scheduled_for.isoformat(),
    }


async def test_each_reminder_is_enqueued_once(db: FakeDatabase, clock: FakeClock) -> None:
    clock.advance(timedelta(days=7))
    scheduler = ReminderScheduler(db.uow, clock)

    await scheduler.run_once()
    second = await scheduler.run_once()

    assert second.enqueued == 0
    assert len(db.state.outbox) == 1


async def test_cancelled_reminder_is_never_sent(db: FakeDatabase, clock: FakeClock) -> None:
    for notification in db.state.notifications.values():
        notification.cancel()
    clock.advance(timedelta(days=30))

    result = await ReminderScheduler(db.uow, clock).run_once()

    assert result.enqueued == 0


async def test_batch_size(db: FakeDatabase, clock: FakeClock) -> None:
    clock.advance(timedelta(days=30))  # all three are due
    scheduler = ReminderScheduler(db.uow, clock, batch_size=2)

    assert (await scheduler.run_once()).enqueued == 2
    assert (await scheduler.run_once()).enqueued == 1


async def test_expiry_reminder_expires_the_subscription(db: FakeDatabase, clock: FakeClock) -> None:
    clock.advance(timedelta(days=10))  # the expiry moment

    result = await ReminderScheduler(db.uow, clock).run_once()

    assert result.enqueued == 3  # T-3d and T-1d were overdue too
    assert result.expired_subscriptions == 1
    assert db.state.subscriptions["sub-1"].status is SubscriptionStatus.EXPIRED


async def test_renewed_subscription_is_not_expired(db: FakeDatabase, clock: FakeClock) -> None:
    clock.advance(timedelta(days=10))
    # Renewed in the meantime, but the old expiry reminder is still due
    db.state.subscriptions["sub-1"].renew(paid_at=clock.now())

    result = await ReminderScheduler(db.uow, clock).run_once()

    assert result.expired_subscriptions == 0
    assert db.state.subscriptions["sub-1"].status is SubscriptionStatus.ACTIVE


class CountingWorker(PollingWorker):
    name = "test"

    def __init__(self, fail_first: bool = False) -> None:
        super().__init__()
        self.calls = 0
        self.fail_first = fail_first

    async def iterate(self) -> float:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("boom")
        if self.calls >= 3:
            self.stop()
        return 0


async def test_polling_worker_survives_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.workers.polling.ERROR_PAUSE_SECONDS", 0.01)
    worker = CountingWorker(fail_first=True)

    await asyncio.wait_for(worker.run(), timeout=2)

    assert worker.calls == 3


async def test_polling_worker_sleep_ends_on_stop() -> None:
    worker = CountingWorker()
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, worker.stop)

    started = loop.time()
    await worker.sleep(60)

    assert loop.time() - started < 1

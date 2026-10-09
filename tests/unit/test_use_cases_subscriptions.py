from datetime import UTC, datetime, timedelta

import pytest

from app.application.dto import UpsertSubscriptionCommand
from app.application.ports import DuplicateKeyError
from app.application.use_cases import GetSubscription, UpsertSubscription
from app.domain.entities import Subscription
from app.domain.enums import NotificationEvent, NotificationStatus, SubscriptionStatus
from app.domain.errors import (
    InvalidValueError,
    SubscriptionNotFoundError,
    SubscriptionOwnershipConflictError,
)
from app.domain.policies import NotificationSchedulePolicy
from tests.unit.fakes import FakeClock, FakeDatabase

EXPIRES = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(clock: FakeClock) -> FakeDatabase:
    return FakeDatabase(clock)


@pytest.fixture
def upsert(db: FakeDatabase, clock: FakeClock) -> UpsertSubscription:
    return UpsertSubscription(db.uow, NotificationSchedulePolicy([3, 1, 0]), clock)


def command(**overrides: object) -> UpsertSubscriptionCommand:
    values: dict[str, object] = {
        "subscription_id": "sub-1",
        "user_id": "user-1",
        "day_count": 30,
        "expected_expires_on": EXPIRES,
    }
    return UpsertSubscriptionCommand(**(values | overrides))  # type: ignore[arg-type]


def statuses(db: FakeDatabase) -> list[tuple[datetime, NotificationStatus]]:
    return sorted((n.scheduled_for, n.status) for n in db.state.notifications.values())


async def test_create_schedules_reminders(upsert: UpsertSubscription, db: FakeDatabase) -> None:
    result = await upsert.execute(command())

    assert result.created is True
    view = result.subscription
    assert (view.subscription_id, view.user_id, view.day_count) == ("sub-1", "user-1", 30)
    assert [(n.event_name, n.scheduled_for, n.status) for n in view.notifications] == [
        (
            NotificationEvent.EXPIRING_SOON,
            EXPIRES - timedelta(days=3),
            NotificationStatus.SCHEDULED,
        ),
        (
            NotificationEvent.EXPIRING_SOON,
            EXPIRES - timedelta(days=1),
            NotificationStatus.SCHEDULED,
        ),
        (NotificationEvent.EXPIRED, EXPIRES, NotificationStatus.SCHEDULED),
    ]
    assert db.commits == 1
    assert len(db.state.notifications) == 3


async def test_repeated_request_is_idempotent(upsert: UpsertSubscription, db: FakeDatabase) -> None:
    await upsert.execute(command())
    before = statuses(db)

    result = await upsert.execute(command())

    assert result.created is False
    assert statuses(db) == before


async def test_update_reschedules_and_keeps_history(
    upsert: UpsertSubscription, db: FakeDatabase
) -> None:
    await upsert.execute(command())
    new_expiry = EXPIRES + timedelta(days=30)

    result = await upsert.execute(command(expected_expires_on=new_expiry, day_count=60))

    assert result.subscription.day_count == 60
    assert result.subscription.expected_expires_on == new_expiry
    cancelled = [n for n in result.subscription.notifications if n.status == "cancelled"]
    scheduled = [n for n in result.subscription.notifications if n.status == "scheduled"]
    assert len(cancelled) == 3
    assert [n.scheduled_for for n in scheduled] == [
        new_expiry - timedelta(days=3),
        new_expiry - timedelta(days=1),
        new_expiry,
    ]
    assert len(db.state.notifications) == 6


async def test_update_by_another_user_is_rejected(
    upsert: UpsertSubscription, db: FakeDatabase
) -> None:
    await upsert.execute(command())

    with pytest.raises(SubscriptionOwnershipConflictError):
        await upsert.execute(command(user_id="user-2", day_count=60))

    assert db.state.subscriptions["sub-1"].day_count == 30


async def test_invalid_input_saves_nothing(upsert: UpsertSubscription, db: FakeDatabase) -> None:
    with pytest.raises(InvalidValueError):
        await upsert.execute(command(day_count=0))

    assert db.commits == 0
    assert db.state.subscriptions == {}


async def test_concurrent_create_is_retried_as_update(
    upsert: UpsertSubscription, db: FakeDatabase
) -> None:
    def concurrent_insert(database: FakeDatabase) -> None:
        # Another request created the same subscription between our read and commit
        database.state.subscriptions["sub-1"] = Subscription(
            id="sub-1", user_id="user-1", day_count=30, expected_expires_on=EXPIRES
        )
        raise DuplicateKeyError("pk_subscriptions")

    db.on_next_commit = concurrent_insert

    result = await upsert.execute(command(day_count=60))

    assert result.created is False
    assert db.state.subscriptions["sub-1"].day_count == 60
    assert len(db.state.notifications) == 3


async def test_get_returns_state_and_history(
    upsert: UpsertSubscription, db: FakeDatabase, clock: FakeClock
) -> None:
    await upsert.execute(command())
    await upsert.execute(command(expected_expires_on=EXPIRES + timedelta(days=30)))

    view = await GetSubscription(db.uow).execute("sub-1")

    assert view.expected_expires_on == EXPIRES + timedelta(days=30)
    assert len(view.notifications) == 6
    times = [n.scheduled_for for n in view.notifications]
    assert times == sorted(times)


async def test_get_unknown_subscription(db: FakeDatabase) -> None:
    with pytest.raises(SubscriptionNotFoundError):
        await GetSubscription(db.uow).execute("missing")


async def test_past_expiry_date_creates_an_expired_subscription(
    upsert: UpsertSubscription, clock: FakeClock
) -> None:
    result = await upsert.execute(command(expected_expires_on=clock.now() - timedelta(days=1)))

    assert result.subscription.status is SubscriptionStatus.EXPIRED
    assert result.subscription.notifications == []  # nothing left to remind about


async def test_moving_the_date_into_the_past_expires_and_cancels_reminders(
    upsert: UpsertSubscription, db: FakeDatabase, clock: FakeClock
) -> None:
    await upsert.execute(command())

    result = await upsert.execute(command(expected_expires_on=clock.now() - timedelta(days=1)))

    assert result.subscription.status is SubscriptionStatus.EXPIRED
    assert {status for _, status in statuses(db)} == {NotificationStatus.CANCELLED}

"""Outbox relay logic on in-memory fakes: publishing, retries, give-up, broker outages."""

import uuid
from datetime import timedelta

import pytest

from app.application.dto import OutboxMessage, PendingEvent
from app.application.ports import BrokerUnavailableError
from app.application.relay import OutboxRelay, RelaySettings
from app.domain.enums import OutboxStatus
from tests.unit.fakes import FakeClock, FakeDatabase, StoredOutboxEvent

SETTINGS = RelaySettings(
    batch_size=10, max_attempts=3, backoff_base_seconds=1, backoff_max_seconds=60
)


class FakePublisher:
    """Records published events; can reject some or simulate an outage."""

    def __init__(self) -> None:
        self.published: list[PendingEvent] = []
        self.reject: set[str] = set()  # routing keys the broker rejects
        self.down_after: int | None = None  # broker goes down after N messages

    async def publish(self, event: PendingEvent) -> None:
        if self.down_after is not None and len(self.published) >= self.down_after:
            raise BrokerUnavailableError("connection refused")
        if event.routing_key in self.reject:
            raise RuntimeError("unroutable")
        self.published.append(event)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(clock: FakeClock) -> FakeDatabase:
    return FakeDatabase(clock)


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def relay(db: FakeDatabase, publisher: FakePublisher, clock: FakeClock) -> OutboxRelay:
    return OutboxRelay(db.uow, publisher, clock, SETTINGS)


def add_event(
    db: FakeDatabase, routing_key: str = "payment.succeeded", **kwargs: object
) -> uuid.UUID:
    event = StoredOutboxEvent(
        id=uuid.uuid4(),
        message=OutboxMessage(
            aggregate_type="payment",
            aggregate_id="p-1",
            event_type=routing_key,
            routing_key=routing_key,
            payload={"schema_version": 1},
            headers={"correlation_id": "req-1"},
        ),
        created_at=db.clock.now(),
        **kwargs,  # type: ignore[arg-type]
    )
    db.state.outbox.append(event)
    return event.id


def stored(db: FakeDatabase, event_id: uuid.UUID) -> StoredOutboxEvent:
    return next(e for e in db.state.outbox if e.id == event_id)


async def test_publishes_pending_events_and_marks_them(
    relay: OutboxRelay, db: FakeDatabase, publisher: FakePublisher, clock: FakeClock
) -> None:
    ids = [add_event(db) for _ in range(3)]

    result = await relay.run_once()

    assert (result.claimed, result.published, result.failed) == (3, 3, 0)
    assert [e.id for e in publisher.published] == ids
    assert publisher.published[0].headers == {"correlation_id": "req-1"}
    for event_id in ids:
        event = stored(db, event_id)
        assert event.status is OutboxStatus.PUBLISHED
        assert event.published_at == clock.now()
        assert event.attempts == 1


async def test_published_events_are_not_sent_again(
    relay: OutboxRelay, db: FakeDatabase, publisher: FakePublisher
) -> None:
    add_event(db)
    await relay.run_once()

    result = await relay.run_once()

    assert result.claimed == 0
    assert len(publisher.published) == 1


async def test_batch_size_is_respected(db: FakeDatabase, publisher: FakePublisher) -> None:
    for _ in range(5):
        add_event(db)
    relay = OutboxRelay(db.uow, publisher, db.clock, RelaySettings(batch_size=2))

    assert (await relay.run_once()).published == 2
    assert (await relay.run_once()).published == 2
    assert (await relay.run_once()).published == 1


async def test_rejected_message_is_retried_with_backoff(
    relay: OutboxRelay, db: FakeDatabase, publisher: FakePublisher, clock: FakeClock
) -> None:
    publisher.reject.add("bad.key")
    bad = add_event(db, "bad.key")
    good = add_event(db)

    result = await relay.run_once()

    assert (result.published, result.failed, result.dead) == (1, 1, 0)
    assert stored(db, good).status is OutboxStatus.PUBLISHED
    event = stored(db, bad)
    assert event.status is OutboxStatus.PENDING
    assert event.attempts == 1
    assert event.last_error == "RuntimeError('unroutable')"
    assert event.available_at == clock.now() + timedelta(seconds=1)

    # Not due yet: skipped on the next iteration
    assert (await relay.run_once()).claimed == 0

    clock.advance(timedelta(seconds=1))
    await relay.run_once()
    assert stored(db, bad).available_at == clock.now() + timedelta(seconds=2)


async def test_gives_up_after_max_attempts(
    relay: OutboxRelay, db: FakeDatabase, publisher: FakePublisher, clock: FakeClock
) -> None:
    publisher.reject.add("bad.key")
    bad = add_event(db, "bad.key")

    results = []
    for _ in range(SETTINGS.max_attempts):
        results.append(await relay.run_once())
        clock.advance(timedelta(minutes=10))

    assert results[-1].dead == 1
    event = stored(db, bad)
    assert event.status is OutboxStatus.DEAD
    assert event.attempts == SETTINGS.max_attempts
    assert (await relay.run_once()).claimed == 0


async def test_broker_outage_does_not_count_attempts(
    relay: OutboxRelay, db: FakeDatabase, publisher: FakePublisher
) -> None:
    first, second, third = (add_event(db) for _ in range(3))
    publisher.down_after = 1  # broker dies after the first message

    result = await relay.run_once()

    assert result.broker_unavailable is True
    assert result.published == 1
    assert stored(db, first).status is OutboxStatus.PUBLISHED
    for event_id in (second, third):
        event = stored(db, event_id)
        assert event.status is OutboxStatus.PENDING
        assert event.attempts == 0
        assert event.last_error is None

    publisher.down_after = None  # broker is back
    assert (await relay.run_once()).published == 2


@pytest.mark.parametrize(
    ("attempts", "seconds"), [(1, 1), (2, 2), (3, 4), (6, 32), (7, 60), (20, 60)]
)
def test_retry_delay_is_exponential_and_capped(attempts: int, seconds: int) -> None:
    assert SETTINGS.retry_delay(attempts) == timedelta(seconds=seconds)

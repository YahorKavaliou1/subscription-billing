"""In-memory implementations of the application ports for unit tests.

FakeDatabase keeps committed state. Each FakeUnitOfWork works on a deep copy
and writes it back only on commit, so a use case that fails half-way leaves
the database untouched, just like a real transaction.
"""

import copy
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

from app.application.dto import OutboxEventInfo, OutboxMessage
from app.application.ports import DuplicateKeyError
from app.domain.entities import Notification, Payment, Subscription
from app.domain.enums import OutboxStatus


@dataclass
class StoredOutboxEvent:
    id: uuid.UUID
    message: OutboxMessage
    created_at: datetime
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    published_at: datetime | None = None
    last_error: str | None = None

    def info(self) -> OutboxEventInfo:
        return OutboxEventInfo(
            id=self.id,
            event_type=self.message.event_type,
            status=self.status,
            attempts=self.attempts,
            created_at=self.created_at,
            published_at=self.published_at,
            last_error=self.last_error,
        )


@dataclass
class State:
    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    notifications: dict[uuid.UUID, Notification] = field(default_factory=dict)
    payments: dict[uuid.UUID, Payment] = field(default_factory=dict)
    outbox: list[StoredOutboxEvent] = field(default_factory=list)


class FakeClock:
    def __init__(self, now: datetime = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class FakeDatabase:
    def __init__(self, clock: FakeClock) -> None:
        self.state = State()
        self.clock = clock
        self.commits = 0
        # Test hooks: make the next commit fail, or run code right before it
        # (e.g. a concurrent transaction committing first, then raising DuplicateKeyError)
        self.fail_next_commit_with: Exception | None = None
        self.on_next_commit: Callable[[FakeDatabase], None] | None = None

    def uow(self) -> "FakeUnitOfWork":
        return FakeUnitOfWork(self)


class FakeSubscriptions:
    def __init__(self, state: State) -> None:
        self._state = state

    async def get(self, subscription_id: str, *, for_update: bool = False) -> Subscription | None:
        return self._state.subscriptions.get(subscription_id)

    async def add(self, subscription: Subscription) -> None:
        if subscription.id in self._state.subscriptions:
            raise DuplicateKeyError("pk_subscriptions")
        self._state.subscriptions[subscription.id] = subscription

    async def update(self, subscription: Subscription) -> None:
        self._state.subscriptions[subscription.id] = subscription


class FakeNotifications:
    def __init__(self, state: State) -> None:
        self._state = state

    async def list_for_subscription(self, subscription_id: str) -> list[Notification]:
        found = [
            n for n in self._state.notifications.values() if n.subscription_id == subscription_id
        ]
        return sorted(found, key=lambda n: n.scheduled_for)

    async def get_for_payment(self, payment_id: uuid.UUID) -> Notification | None:
        return next(
            (n for n in self._state.notifications.values() if n.payment_id == payment_id), None
        )

    async def add_many(self, notifications: Sequence[Notification]) -> None:
        for notification in notifications:
            self._state.notifications[notification.id] = notification

    async def update_many(self, notifications: Sequence[Notification]) -> None:
        await self.add_many(notifications)


class FakePayments:
    def __init__(self, state: State) -> None:
        self._state = state

    async def get(self, payment_id: uuid.UUID) -> Payment | None:
        return self._state.payments.get(payment_id)

    async def get_by_provider_payment(self, provider_payment: str) -> Payment | None:
        return next(
            (p for p in self._state.payments.values() if p.provider_payment == provider_payment),
            None,
        )

    async def add(self, payment: Payment) -> None:
        if await self.get_by_provider_payment(payment.provider_payment):
            raise DuplicateKeyError("uq_payments_provider_payment")
        self._state.payments[payment.id] = payment


class FakeOutbox:
    def __init__(self, state: State, clock: FakeClock) -> None:
        self._state = state
        self._clock = clock

    async def add(self, message: OutboxMessage) -> uuid.UUID:
        event = StoredOutboxEvent(id=uuid.uuid4(), message=message, created_at=self._clock.now())
        self._state.outbox.append(event)
        return event.id

    async def get_latest_for_aggregate(
        self, aggregate_type: str, aggregate_id: str
    ) -> OutboxEventInfo | None:
        matching = [
            e
            for e in self._state.outbox
            if (e.message.aggregate_type, e.message.aggregate_id) == (aggregate_type, aggregate_id)
        ]
        return matching[-1].info() if matching else None


class FakeUnitOfWork:
    def __init__(self, database: FakeDatabase) -> None:
        self._db = database
        self._working = copy.deepcopy(database.state)
        self.subscriptions = FakeSubscriptions(self._working)
        self.notifications = FakeNotifications(self._working)
        self.payments = FakePayments(self._working)
        self.outbox = FakeOutbox(self._working, database.clock)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Uncommitted changes are simply dropped (rollback)
        return None

    async def commit(self) -> None:
        if self._db.on_next_commit is not None:
            hook, self._db.on_next_commit = self._db.on_next_commit, None
            hook(self._db)
        if self._db.fail_next_commit_with is not None:
            error, self._db.fail_next_commit_with = self._db.fail_next_commit_with, None
            raise error
        self._db.state = copy.deepcopy(self._working)
        self._db.commits += 1

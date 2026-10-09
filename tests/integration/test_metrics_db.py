"""Backlog gauges read from real PostgreSQL."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.dto import RegisterPaymentCommand, UpsertSubscriptionCommand
from app.application.ports import SystemClock, UnitOfWorkFactory
from app.application.use_cases import RegisterPayment, UpsertSubscription
from app.domain.enums import NotificationStatus, OutboxStatus, PaymentStatus
from app.domain.policies import NotificationSchedulePolicy
from app.infrastructure.db.stats import read_backlog

pytestmark = pytest.mark.integration

CLOCK = SystemClock()


async def test_backlog_counts_by_status(
    uow_factory: UnitOfWorkFactory, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    empty = await read_backlog(session_factory)
    assert set(empty.outbox.values()) == {0}
    assert empty.oldest_pending_created_at is None

    expires = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)
    await UpsertSubscription(uow_factory, NotificationSchedulePolicy([3, 1, 0]), CLOCK).execute(
        UpsertSubscriptionCommand(
            subscription_id="sub-1", user_id="user-1", day_count=30, expected_expires_on=expires
        )
    )
    await RegisterPayment(uow_factory, CLOCK).execute(
        RegisterPaymentCommand(
            subscription_id="sub-1",
            provider_payment="pi_stats",
            amount=Decimal("9.99"),
            currency="EUR",
            status=PaymentStatus.SUCCEEDED,
        )
    )

    stats = await read_backlog(session_factory)

    assert stats.outbox[OutboxStatus.PENDING] == 1
    assert stats.outbox[OutboxStatus.DEAD] == 0
    assert stats.oldest_pending_created_at is not None
    assert stats.notifications[NotificationStatus.SCHEDULED] == 3
    assert stats.notifications[NotificationStatus.SENT] == 0

"""Backlog numbers for metrics: outbox and notifications by status."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import NotificationStatus, OutboxStatus
from app.infrastructure.db.models import NotificationModel, OutboxEventModel
from app.infrastructure.observability.metrics import (
    NOTIFICATIONS,
    OUTBOX_EVENTS,
    OUTBOX_OLDEST_PENDING_AGE,
)


@dataclass(frozen=True, slots=True)
class BacklogStats:
    outbox: dict[OutboxStatus, int]
    oldest_pending_created_at: datetime | None
    notifications: dict[NotificationStatus, int]


async def read_backlog(session_factory: async_sessionmaker[AsyncSession]) -> BacklogStats:
    async with session_factory() as session:
        outbox_rows = await session.execute(
            select(OutboxEventModel.status, func.count()).group_by(OutboxEventModel.status)
        )
        oldest_pending = await session.scalar(
            select(func.min(OutboxEventModel.created_at)).where(
                OutboxEventModel.status == OutboxStatus.PENDING
            )
        )
        notification_rows = await session.execute(
            select(NotificationModel.status, func.count()).group_by(NotificationModel.status)
        )
    outbox = dict.fromkeys(OutboxStatus, 0) | {
        OutboxStatus(status): count for status, count in outbox_rows
    }
    notifications = dict.fromkeys(NotificationStatus, 0) | {
        NotificationStatus(status): count for status, count in notification_rows
    }
    return BacklogStats(outbox, oldest_pending, notifications)


def publish_backlog(stats: BacklogStats, now: datetime) -> None:
    """Copy the numbers into the gauges. Every status is set, so stale values never linger."""
    for outbox_status, count in stats.outbox.items():
        OUTBOX_EVENTS.labels(status=outbox_status.value).set(count)
    for notification_status, count in stats.notifications.items():
        NOTIFICATIONS.labels(status=notification_status.value).set(count)
    oldest = stats.oldest_pending_created_at
    OUTBOX_OLDEST_PENDING_AGE.set(max((now - oldest).total_seconds(), 0) if oldest else 0)

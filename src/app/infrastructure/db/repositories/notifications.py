import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Notification
from app.domain.enums import NotificationStatus
from app.infrastructure.db import mappers
from app.infrastructure.db.models import NotificationModel
from app.infrastructure.db.repositories._common import flush


class SqlNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_subscription(self, subscription_id: str) -> list[Notification]:
        models = await self._session.scalars(
            select(NotificationModel)
            .where(NotificationModel.subscription_id == subscription_id)
            .order_by(NotificationModel.scheduled_for, NotificationModel.created_at)
        )
        return [mappers.notification_to_entity(m) for m in models]

    async def get(self, notification_id: uuid.UUID) -> Notification | None:
        model = await self._session.get(NotificationModel, notification_id)
        return mappers.notification_to_entity(model) if model else None

    async def claim_due(self, limit: int, now: datetime) -> list[Notification]:
        models = await self._session.scalars(
            select(NotificationModel)
            .where(
                NotificationModel.status == NotificationStatus.SCHEDULED,
                NotificationModel.scheduled_for <= now,
            )
            .order_by(NotificationModel.scheduled_for)
            .limit(limit)
            # Served by the partial index ix_notifications_due; locked rows are skipped
            .with_for_update(skip_locked=True)
        )
        return [mappers.notification_to_entity(m) for m in models]

    async def get_for_payment(self, payment_id: uuid.UUID) -> Notification | None:
        model = await self._session.scalar(
            select(NotificationModel)
            .where(NotificationModel.payment_id == payment_id)
            .order_by(NotificationModel.created_at.desc())
            .limit(1)
        )
        return mappers.notification_to_entity(model) if model else None

    async def add_many(self, notifications: Sequence[Notification]) -> None:
        if not notifications:
            return
        self._session.add_all([mappers.notification_to_model(n) for n in notifications])
        await flush(self._session)

    async def update_many(self, notifications: Sequence[Notification]) -> None:
        for notification in notifications:
            model = await self._session.get(NotificationModel, notification.id)
            if model is None:
                raise LookupError(f"Notification {notification.id} is not loaded")
            mappers.copy_notification_state(notification, model)
        if notifications:
            # Flush cancellations before new rows reuse their dedup keys
            await flush(self._session)

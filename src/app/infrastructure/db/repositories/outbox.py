import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto import OutboxEventInfo, OutboxMessage, PendingEvent
from app.domain.enums import OutboxStatus
from app.infrastructure.db.models import OutboxEventModel
from app.infrastructure.db.repositories._common import flush
from app.infrastructure.observability.correlation import (
    CORRELATION_ID_KEY,
    get_correlation_id,
    new_correlation_id,
)


class SqlOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, message: OutboxMessage) -> uuid.UUID:
        headers = dict(message.headers)
        # The id of the HTTP request that caused the event, so relay and consumers log
        # under it. Without a request (scheduler reminders) the event starts its own trace.
        headers.setdefault(CORRELATION_ID_KEY, get_correlation_id() or new_correlation_id())
        event_id = uuid.uuid4()
        self._session.add(
            OutboxEventModel(
                id=event_id,
                aggregate_type=message.aggregate_type,
                aggregate_id=message.aggregate_id,
                event_type=message.event_type,
                routing_key=message.routing_key,
                payload=message.payload,
                headers=headers,
            )
        )
        await flush(self._session)
        return event_id

    async def get_latest_for_aggregate(
        self, aggregate_type: str, aggregate_id: str
    ) -> OutboxEventInfo | None:
        model = await self._session.scalar(
            select(OutboxEventModel)
            .where(
                OutboxEventModel.aggregate_type == aggregate_type,
                OutboxEventModel.aggregate_id == aggregate_id,
            )
            .order_by(OutboxEventModel.created_at.desc())
            .limit(1)
        )
        if model is None:
            return None
        return OutboxEventInfo(
            id=model.id,
            event_type=model.event_type,
            status=model.status,
            attempts=model.attempts,
            created_at=model.created_at,
            published_at=model.published_at,
            last_error=model.last_error,
        )

    # --- Relay side ----------------------------------------------------------------

    async def claim_pending(self, limit: int, now: datetime) -> list[PendingEvent]:
        models = await self._session.scalars(
            select(OutboxEventModel)
            .where(
                OutboxEventModel.status == OutboxStatus.PENDING,
                OutboxEventModel.available_at <= now,
            )
            .order_by(OutboxEventModel.available_at, OutboxEventModel.created_at)
            .limit(limit)
            # Rows held by another relay are skipped instead of waited for
            .with_for_update(skip_locked=True)
        )
        return [
            PendingEvent(
                id=m.id,
                event_type=m.event_type,
                routing_key=m.routing_key,
                payload=m.payload,
                headers=m.headers,
                attempts=m.attempts,
                created_at=m.created_at,
            )
            for m in models
        ]

    async def mark_published(self, event_id: uuid.UUID, at: datetime) -> None:
        await self._session.execute(
            update(OutboxEventModel)
            .where(OutboxEventModel.id == event_id)
            .values(
                status=OutboxStatus.PUBLISHED,
                published_at=at,
                attempts=OutboxEventModel.attempts + 1,
                last_error=None,
            )
        )

    async def mark_failed(self, event_id: uuid.UUID, error: str, retry_at: datetime | None) -> None:
        values: dict[str, object] = {
            "attempts": OutboxEventModel.attempts + 1,
            "last_error": error,
        }
        if retry_at is None:
            values["status"] = OutboxStatus.DEAD
        else:
            values["available_at"] = retry_at
        await self._session.execute(
            update(OutboxEventModel).where(OutboxEventModel.id == event_id).values(**values)
        )

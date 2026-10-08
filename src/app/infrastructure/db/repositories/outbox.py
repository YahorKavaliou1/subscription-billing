import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto import OutboxEventInfo, OutboxMessage
from app.infrastructure.db.models import OutboxEventModel
from app.infrastructure.db.repositories._common import flush
from app.infrastructure.observability.correlation import CORRELATION_ID_KEY, get_correlation_id


class SqlOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, message: OutboxMessage) -> uuid.UUID:
        headers = dict(message.headers)
        correlation_id = get_correlation_id()
        if correlation_id and CORRELATION_ID_KEY not in headers:
            # Lets consumers log under the id of the HTTP request that caused the event
            headers[CORRELATION_ID_KEY] = correlation_id
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

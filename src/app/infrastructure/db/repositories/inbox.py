from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import InboxMessageModel


class SqlInboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, consumer: str, message_id: str) -> bool:
        # ON CONFLICT DO NOTHING instead of catching an error: the transaction stays usable.
        # A concurrent duplicate waits for the first transaction and then inserts nothing.
        result = await self._session.execute(
            insert(InboxMessageModel)
            .values(consumer=consumer, message_id=message_id)
            .on_conflict_do_nothing(index_elements=["consumer", "message_id"])
            .returning(InboxMessageModel.message_id)
        )
        return result.scalar_one_or_none() is not None

    async def exists(self, consumer: str, message_id: str) -> bool:
        found = await self._session.scalar(
            select(InboxMessageModel.message_id).where(
                InboxMessageModel.consumer == consumer,
                InboxMessageModel.message_id == message_id,
            )
        )
        return found is not None

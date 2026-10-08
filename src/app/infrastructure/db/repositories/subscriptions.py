from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Subscription
from app.infrastructure.db import mappers
from app.infrastructure.db.models import SubscriptionModel
from app.infrastructure.db.repositories._common import flush


class SqlSubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, subscription_id: str, *, for_update: bool = False) -> Subscription | None:
        query = select(SubscriptionModel).where(SubscriptionModel.id == subscription_id)
        if for_update:
            # Serializes concurrent updates of the same subscription
            query = query.with_for_update()
        model = await self._session.scalar(query)
        return mappers.subscription_to_entity(model) if model else None

    async def add(self, subscription: Subscription) -> None:
        self._session.add(mappers.subscription_to_model(subscription))
        await flush(self._session)

    async def update(self, subscription: Subscription) -> None:
        model = await self._session.get(SubscriptionModel, subscription.id)
        if model is None:
            raise LookupError(f"Subscription {subscription.id!r} is not loaded")
        mappers.copy_subscription(subscription, model)

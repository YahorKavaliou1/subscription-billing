import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Payment
from app.infrastructure.db import mappers
from app.infrastructure.db.models import PaymentModel
from app.infrastructure.db.repositories._common import flush


class SqlPaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, payment_id: uuid.UUID) -> Payment | None:
        model = await self._session.get(PaymentModel, payment_id)
        return mappers.payment_to_entity(model) if model else None

    async def get_by_provider_payment(self, provider_payment: str) -> Payment | None:
        model = await self._session.scalar(
            select(PaymentModel).where(PaymentModel.provider_payment == provider_payment)
        )
        return mappers.payment_to_entity(model) if model else None

    async def add(self, payment: Payment) -> None:
        self._session.add(mappers.payment_to_model(payment))
        await flush(self._session)

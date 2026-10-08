"""Task items 3 and 4: register a payment with its event, read payment delivery status."""

import uuid
from datetime import datetime

from app.application.dto import (
    NotificationView,
    PaymentStatusView,
    PaymentView,
    RegisterPaymentCommand,
    RegisterPaymentResult,
)
from app.application.events import PAYMENT_AGGREGATE, payment_result_event
from app.application.ports import Clock, DuplicateKeyError, UnitOfWork, UnitOfWorkFactory
from app.domain.entities import Payment
from app.domain.errors import (
    PaymentIdempotencyConflictError,
    PaymentNotFoundError,
    SubscriptionNotFoundError,
)
from app.domain.value_objects import Money


class RegisterPayment:
    """Store a payment and its result event in ONE transaction (transactional outbox).

    Either both rows are committed or neither is, so a payment can never exist
    without its event. Publishing to RabbitMQ is done later by the outbox relay.

    Idempotent by `provider_payment`: a retried request with the same data returns
    the original result, a request with different data is rejected.
    """

    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(self, command: RegisterPaymentCommand) -> RegisterPaymentResult:
        now = self._clock.now()
        # Validate input before touching the database
        candidate = Payment(
            subscription_id=command.subscription_id,
            provider_payment=command.provider_payment,
            money=Money(command.amount, command.currency),
            status=command.status,
            created_at=now,
        )
        try:
            return await self._register(candidate, now)
        except DuplicateKeyError:
            # A concurrent request with the same provider_payment won the race
            return await self._replay(candidate)

    async def _register(self, candidate: Payment, now: datetime) -> RegisterPaymentResult:
        async with self._uow_factory() as uow:
            existing = await uow.payments.get_by_provider_payment(candidate.provider_payment)
            if existing is not None:
                return await self._replay(candidate, existing)

            subscription = await uow.subscriptions.get(candidate.subscription_id)
            if subscription is None:
                raise SubscriptionNotFoundError(candidate.subscription_id)

            await uow.payments.add(candidate)
            event_id = await uow.outbox.add(payment_result_event(candidate, subscription, now))
            await uow.commit()

        return RegisterPaymentResult(
            payment=PaymentView.from_entity(candidate), event_id=event_id, created=True
        )

    async def _replay(
        self, candidate: Payment, existing: Payment | None = None
    ) -> RegisterPaymentResult:
        async with self._uow_factory() as uow:
            if existing is None:
                existing = await uow.payments.get_by_provider_payment(candidate.provider_payment)
            if existing is None:  # pragma: no cover  # duplicate key implies the row exists
                raise PaymentNotFoundError(candidate.provider_payment)
            if not existing.is_same_request(candidate):
                raise PaymentIdempotencyConflictError(candidate.provider_payment)
            event = await uow.outbox.get_latest_for_aggregate(PAYMENT_AGGREGATE, str(existing.id))
        return RegisterPaymentResult(
            payment=PaymentView.from_entity(existing),
            event_id=event.id if event else None,
            created=False,
        )


class GetPaymentStatus:
    """Payment state, whether its event reached the broker and whether the user was notified."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def by_id(self, payment_id: uuid.UUID) -> PaymentStatusView:
        async with self._uow_factory() as uow:
            payment = await uow.payments.get(payment_id)
            if payment is None:
                raise PaymentNotFoundError(str(payment_id))
            return await self._build(uow, payment)

    async def by_provider_payment(self, provider_payment: str) -> PaymentStatusView:
        async with self._uow_factory() as uow:
            payment = await uow.payments.get_by_provider_payment(provider_payment)
            if payment is None:
                raise PaymentNotFoundError(provider_payment)
            return await self._build(uow, payment)

    @staticmethod
    async def _build(uow: UnitOfWork, payment: Payment) -> PaymentStatusView:
        event = await uow.outbox.get_latest_for_aggregate(PAYMENT_AGGREGATE, str(payment.id))
        notification = await uow.notifications.get_for_payment(payment.id)
        return PaymentStatusView(
            payment=PaymentView.from_entity(payment),
            event=event,
            notification=NotificationView.from_entity(notification) if notification else None,
        )

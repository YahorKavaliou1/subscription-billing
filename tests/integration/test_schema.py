"""Database-level guarantees: constraints that protect data even if application code is wrong."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import NotificationStatus, OutboxStatus, PaymentStatus, SubscriptionStatus
from app.infrastructure.db.models import (
    InboxMessageModel,
    NotificationModel,
    OutboxEventModel,
    PaymentModel,
    SubscriptionModel,
)

pytestmark = pytest.mark.integration

EXPIRES = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


def make_subscription(**overrides: Any) -> SubscriptionModel:
    values: dict[str, Any] = {
        "id": f"sub-{uuid.uuid4().hex[:8]}",
        "user_id": "user-1",
        "day_count": 30,
        "expected_expires_on": EXPIRES,
    }
    return SubscriptionModel(**(values | overrides))


def make_payment(subscription_id: str, **overrides: Any) -> PaymentModel:
    values: dict[str, Any] = {
        "subscription_id": subscription_id,
        "provider_payment": f"pay-{uuid.uuid4().hex[:8]}",
        "amount": Decimal("9.99"),
        "currency": "EUR",
        "status": PaymentStatus.SUCCEEDED,
    }
    return PaymentModel(**(values | overrides))


async def add(session: AsyncSession, *objects: object) -> None:
    session.add_all(objects)
    await session.flush()


async def assert_violates(session: AsyncSession, constraint: str, *objects: object) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        async with session.begin_nested():
            await add(session, *objects)


async def test_rows_get_server_defaults(session: AsyncSession) -> None:
    subscription = make_subscription()
    await add(session, subscription)
    payment = make_payment(subscription.id)
    event = OutboxEventModel(
        aggregate_type="payment",
        aggregate_id=str(payment.id),
        event_type="payment.succeeded",
        routing_key="payment.succeeded",
        payload={"amount": "9.99"},
    )
    await add(session, payment, event)
    await session.refresh(subscription)
    await session.refresh(event)

    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.version == 1
    assert subscription.created_at.tzinfo is not None
    assert event.status is OutboxStatus.PENDING
    assert event.attempts == 0
    assert event.headers == {}
    assert event.available_at is not None
    assert event.published_at is None


async def test_amount_keeps_exact_decimal_value(session: AsyncSession) -> None:
    subscription = make_subscription()
    await add(session, subscription)
    payment = make_payment(subscription.id, amount=Decimal("1234.5678"))
    await add(session, payment)
    session.expunge(payment)

    stored = await session.scalar(select(PaymentModel.amount).where(PaymentModel.id == payment.id))

    assert stored == Decimal("1234.5678")


async def test_subscription_update_increments_version(session: AsyncSession) -> None:
    subscription = make_subscription()
    await add(session, subscription)

    subscription.day_count = 60
    await session.flush()

    assert subscription.version == 2


async def test_subscription_day_count_must_be_positive(session: AsyncSession) -> None:
    await assert_violates(
        session, "ck_subscriptions_day_count_positive", make_subscription(day_count=0)
    )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"amount": Decimal(0)}, "ck_payments_amount_positive"),
        ({"amount": Decimal("-1")}, "ck_payments_amount_positive"),
        ({"currency": "eur"}, "ck_payments_currency_iso4217"),
        ({"currency": "E1R"}, "ck_payments_currency_iso4217"),
    ],
)
async def test_payment_value_checks(
    session: AsyncSession, overrides: dict[str, Any], constraint: str
) -> None:
    subscription = make_subscription()
    await add(session, subscription)

    await assert_violates(session, constraint, make_payment(subscription.id, **overrides))


async def test_provider_payment_is_unique(session: AsyncSession) -> None:
    subscription = make_subscription()
    await add(session, subscription)
    await add(session, make_payment(subscription.id, provider_payment="p-1"))

    await assert_violates(
        session,
        "uq_payments_provider_payment",
        make_payment(subscription.id, provider_payment="p-1"),
    )


async def test_payment_requires_existing_subscription(session: AsyncSession) -> None:
    await assert_violates(
        session, "fk_payments_subscription_id_subscriptions", make_payment("missing")
    )


async def test_notification_dedup_key_is_unique(session: AsyncSession) -> None:
    subscription = make_subscription()
    await add(session, subscription)

    def notification() -> NotificationModel:
        return NotificationModel(
            subscription_id=subscription.id,
            event_name="subscription.expiring_soon",
            scheduled_for=EXPIRES - timedelta(days=3),
            dedup_key=f"{subscription.id}:expiring_soon:3",
        )

    first = notification()
    await add(session, first)
    assert first.status is NotificationStatus.SCHEDULED

    await assert_violates(session, "uq_notifications_dedup_key", notification())


async def test_inbox_rejects_already_processed_message(session: AsyncSession) -> None:
    await add(session, InboxMessageModel(consumer="renewal", message_id="m-1"))
    # Same message for another consumer is fine
    await add(session, InboxMessageModel(consumer="notifications", message_id="m-1"))

    await assert_violates(
        session, "pk_inbox_messages", InboxMessageModel(consumer="renewal", message_id="m-1")
    )

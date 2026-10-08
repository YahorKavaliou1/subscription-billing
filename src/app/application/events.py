"""Builders of outbox messages. Payloads are versioned JSON-friendly dicts."""

from datetime import datetime

from app.application.dto import OutboxMessage
from app.domain.entities import Payment, Subscription

SCHEMA_VERSION = 1
PAYMENT_AGGREGATE = "payment"


def payment_result_event(
    payment: Payment, subscription: Subscription, occurred_at: datetime
) -> OutboxMessage:
    """`payment.succeeded` / `payment.failed`: drives user notification and renewal."""
    event_type = payment.event.value
    return OutboxMessage(
        aggregate_type=PAYMENT_AGGREGATE,
        aggregate_id=str(payment.id),
        event_type=event_type,
        routing_key=event_type,
        payload={
            "schema_version": SCHEMA_VERSION,
            "payment_id": str(payment.id),
            "subscription_id": subscription.id,
            "user_id": subscription.user_id,
            "provider_payment": payment.provider_payment,
            # Decimal as string: JSON numbers would lose precision
            "amount": str(payment.money.amount),
            "currency": payment.money.currency,
            "status": payment.status.value,
            "occurred_at": occurred_at.isoformat(),
        },
    )

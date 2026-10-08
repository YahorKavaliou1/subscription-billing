"""Builders of outbox messages. Payloads are versioned JSON-friendly dicts."""

from datetime import datetime

from app.application.dto import OutboxMessage
from app.domain.entities import Notification, Payment, Subscription
from app.domain.value_objects import format_decimal

SCHEMA_VERSION = 1
PAYMENT_AGGREGATE = "payment"
NOTIFICATION_AGGREGATE = "notification"


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
            "amount": format_decimal(payment.money.amount),
            "currency": payment.money.currency,
            "status": payment.status.value,
            "occurred_at": occurred_at.isoformat(),
        },
    )


def reminder_due_event(notification: Notification) -> OutboxMessage:
    """`notification.<event_name>`: a scheduled reminder is due and must be sent."""
    event_type = f"notification.{notification.event_name.value}"
    return OutboxMessage(
        aggregate_type=NOTIFICATION_AGGREGATE,
        aggregate_id=str(notification.id),
        event_type=event_type,
        # Matches the `notification.#` binding of the notifications.send queue
        routing_key=event_type,
        payload={
            "schema_version": SCHEMA_VERSION,
            "notification_id": str(notification.id),
            "subscription_id": notification.subscription_id,
            "event_name": notification.event_name.value,
            "scheduled_for": notification.scheduled_for.isoformat(),
        },
    )

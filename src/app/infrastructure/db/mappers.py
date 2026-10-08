"""Conversion between ORM models and domain entities.

Repositories return domain entities only; ORM models never leave the infrastructure layer.
"""

from app.domain.entities import Notification, Payment, Subscription
from app.domain.enums import NotificationEvent
from app.domain.value_objects import Money
from app.infrastructure.db.models import NotificationModel, PaymentModel, SubscriptionModel

# --- Subscription --------------------------------------------------------------------


def subscription_to_entity(model: SubscriptionModel) -> Subscription:
    return Subscription(
        id=model.id,
        user_id=model.user_id,
        day_count=model.day_count,
        expected_expires_on=model.expected_expires_on,
        status=model.status,
    )


def subscription_to_model(entity: Subscription) -> SubscriptionModel:
    model = SubscriptionModel(id=entity.id)
    copy_subscription(entity, model)
    return model


def copy_subscription(entity: Subscription, model: SubscriptionModel) -> None:
    model.user_id = entity.user_id
    model.day_count = entity.day_count
    model.expected_expires_on = entity.expected_expires_on
    model.status = entity.status


# --- Notification --------------------------------------------------------------------


def notification_to_entity(model: NotificationModel) -> Notification:
    return Notification(
        id=model.id,
        subscription_id=model.subscription_id,
        event_name=NotificationEvent(model.event_name),
        scheduled_for=model.scheduled_for,
        dedup_key=model.dedup_key,
        status=model.status,
        payment_id=model.payment_id,
        sent_at=model.sent_at,
        attempts=model.attempts,
        last_error=model.last_error,
    )


def notification_to_model(entity: Notification) -> NotificationModel:
    model = NotificationModel(
        id=entity.id,
        subscription_id=entity.subscription_id,
        event_name=entity.event_name.value,
        scheduled_for=entity.scheduled_for,
        dedup_key=entity.dedup_key,
        payment_id=entity.payment_id,
    )
    copy_notification_state(entity, model)
    return model


def copy_notification_state(entity: Notification, model: NotificationModel) -> None:
    """Only the mutable part of a notification; identity fields never change."""
    model.status = entity.status
    model.sent_at = entity.sent_at
    model.attempts = entity.attempts
    model.last_error = entity.last_error


# --- Payment -------------------------------------------------------------------------


def payment_to_entity(model: PaymentModel) -> Payment:
    return Payment(
        id=model.id,
        subscription_id=model.subscription_id,
        provider_payment=model.provider_payment,
        money=Money(model.amount, model.currency),
        status=model.status,
        created_at=model.created_at,
    )


def payment_to_model(entity: Payment) -> PaymentModel:
    model = PaymentModel(
        id=entity.id,
        subscription_id=entity.subscription_id,
        provider_payment=entity.provider_payment,
        amount=entity.money.amount,
        currency=entity.money.currency,
        status=entity.status,
    )
    if entity.created_at is not None:
        model.created_at = entity.created_at
    return model

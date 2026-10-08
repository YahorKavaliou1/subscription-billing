"""Payload schemas of messages exchanged through RabbitMQ (versioned contracts).

Producers build payloads as plain dicts (application/events.py); consumers validate
them with these models before doing anything.
"""

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.domain.enums import NotificationEvent, PaymentStatus


class EventPayload(BaseModel):
    # New optional fields from a newer producer must not break an older consumer
    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1] = 1


class PaymentResultEvent(EventPayload):
    """`payment.succeeded` / `payment.failed`."""

    payment_id: uuid.UUID
    subscription_id: str
    user_id: str
    provider_payment: str
    amount: Decimal
    currency: str
    status: PaymentStatus
    occurred_at: AwareDatetime


class ReminderDueEvent(EventPayload):
    """`notification.<event_name>`: a scheduled reminder whose time has come."""

    notification_id: uuid.UUID
    subscription_id: str
    event_name: NotificationEvent
    scheduled_for: AwareDatetime

import uuid
from decimal import Decimal

from pydantic import AwareDatetime, Field, field_serializer

from app.api.schemas.common import RequestModel, ResponseModel
from app.api.schemas.subscriptions import NotificationResponse
from app.domain.enums import OutboxStatus, PaymentStatus
from app.domain.value_objects import format_decimal


class RegisterPaymentRequest(RequestModel):
    subscription_id: str = Field(min_length=1, max_length=64, examples=["sub-1"])
    provider_payment: str = Field(min_length=1, max_length=128, examples=["pi_3PqZ"])
    # Accepts a JSON number or string; parsed as an exact Decimal, never a float
    amount: Decimal = Field(gt=0, max_digits=19, decimal_places=4, examples=["9.99"])
    currency: str = Field(min_length=3, max_length=3, examples=["EUR"])
    status: PaymentStatus


class PaymentResponse(ResponseModel):
    id: uuid.UUID
    subscription_id: str
    provider_payment: str
    # Serialized as a JSON string to keep the exact value
    amount: Decimal
    currency: str
    status: PaymentStatus
    created_at: AwareDatetime | None

    @field_serializer("amount")
    def _serialize_amount(self, amount: Decimal) -> str:
        # Same text whether the value came from the request or from NUMERIC(19, 4)
        return format_decimal(amount)


class RegisterPaymentResponse(ResponseModel):
    payment: PaymentResponse
    event_id: uuid.UUID | None


class EventDeliveryResponse(ResponseModel):
    """Was the payment event published to the message broker?"""

    id: uuid.UUID
    event_type: str
    status: OutboxStatus
    attempts: int
    created_at: AwareDatetime
    published_at: AwareDatetime | None
    last_error: str | None


class PaymentStatusResponse(ResponseModel):
    payment: PaymentResponse
    event: EventDeliveryResponse | None
    notification: NotificationResponse | None

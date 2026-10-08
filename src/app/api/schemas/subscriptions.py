from pydantic import AwareDatetime, Field

from app.api.schemas.common import RequestModel, ResponseModel
from app.domain.enums import NotificationEvent, NotificationStatus, SubscriptionStatus


class UpsertSubscriptionRequest(RequestModel):
    user_id: str = Field(min_length=1, max_length=64, examples=["user-1"])
    day_count: int = Field(gt=0, examples=[30])
    # AwareDatetime rejects values without a timezone
    expected_expires_on: AwareDatetime = Field(examples=["2026-11-01T12:00:00Z"])


class NotificationResponse(ResponseModel):
    event_name: NotificationEvent
    scheduled_for: AwareDatetime
    sent_at: AwareDatetime | None
    status: NotificationStatus
    attempts: int
    last_error: str | None


class SubscriptionResponse(ResponseModel):
    subscription_id: str
    user_id: str
    day_count: int
    expected_expires_on: AwareDatetime
    status: SubscriptionStatus
    notifications: list[NotificationResponse]

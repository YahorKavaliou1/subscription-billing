from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.domain.enums import NotificationEvent


@dataclass(frozen=True, slots=True)
class OutgoingNotification:
    notification_id: str
    user_id: str
    subscription_id: str
    event_name: NotificationEvent
    scheduled_for: datetime
    data: dict[str, Any]


class NotificationSender(Protocol):
    """Delivery channel (log, email, push). Raises on failure; retries are the caller's job."""

    async def send(self, notification: OutgoingNotification) -> None: ...

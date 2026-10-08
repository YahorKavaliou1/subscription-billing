"""Notification channels. Real email/push delivery is out of scope; senders log what they send."""

from app.application.ports import NotificationSender, OutgoingNotification
from app.config import Settings
from app.infrastructure.observability.logging import get_logger

log = get_logger("app.notifications")


class LogSender:
    """Writes the notification to the log; stands in for a real channel."""

    async def send(self, notification: OutgoingNotification) -> None:
        log.info(
            "notification.sent",
            channel="log",
            notification_id=notification.notification_id,
            user_id=notification.user_id,
            subscription_id=notification.subscription_id,
            event_name=notification.event_name.value,
            data=notification.data,
        )


class EmailSender:
    """Stub of an email channel: shows where a provider call would go."""

    async def send(self, notification: OutgoingNotification) -> None:
        # A real implementation would call the provider with
        # idempotency_key=notification.notification_id to avoid duplicate emails on retries.
        log.info(
            "notification.sent",
            channel="email",
            notification_id=notification.notification_id,
            user_id=notification.user_id,
            event_name=notification.event_name.value,
        )


def create_sender(settings: Settings) -> NotificationSender:
    if settings.notification_sender == "email":
        return EmailSender()
    return LogSender()

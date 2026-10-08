"""Notification scheduling policy: which reminders a subscription needs and when."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.entities import Notification, Subscription
from app.domain.enums import NotificationEvent
from app.domain.errors import InvalidValueError
from app.domain.value_objects import require_aware_utc


@dataclass(frozen=True, slots=True)
class PlannedReminder:
    event_name: NotificationEvent
    scheduled_for: datetime

    @property
    def key(self) -> tuple[NotificationEvent, datetime]:
        return self.event_name, self.scheduled_for

    def to_notification(self, subscription_id: str) -> Notification:
        return Notification(
            subscription_id=subscription_id,
            event_name=self.event_name,
            scheduled_for=self.scheduled_for,
            # Unique among non-cancelled notifications of the subscription
            dedup_key=(
                f"reminder:{subscription_id}:{self.event_name.value}:"
                f"{self.scheduled_for.isoformat()}"
            ),
        )


@dataclass(frozen=True, slots=True)
class Reschedule:
    """Result of comparing the current schedule with the desired one."""

    to_cancel: list[Notification]
    to_create: list[Notification]

    @property
    def is_empty(self) -> bool:
        return not self.to_cancel and not self.to_create


class NotificationSchedulePolicy:
    """Reminders at fixed offsets (in days) before `expected_expires_on`.

    Offset 0 means "on the expiry moment" and produces the EXPIRED event,
    any other offset produces EXPIRING_SOON.
    """

    def __init__(self, offsets_days: Iterable[int]) -> None:
        offsets = sorted(set(offsets_days), reverse=True)
        if not offsets:
            raise InvalidValueError("at least one reminder offset is required")
        if offsets[-1] < 0:
            raise InvalidValueError("reminder offsets must be non-negative")
        self._offsets = tuple(offsets)

    @property
    def offsets_days(self) -> tuple[int, ...]:
        return self._offsets

    def plan(self, subscription: Subscription, now: datetime) -> list[PlannedReminder]:
        """Reminders still ahead of `now`, ordered by time; past moments are skipped."""
        now = require_aware_utc(now, "now")
        planned = []
        for days in self._offsets:
            moment = subscription.expected_expires_on - timedelta(days=days)
            if moment < now:
                continue
            event = NotificationEvent.EXPIRED if days == 0 else NotificationEvent.EXPIRING_SOON
            planned.append(PlannedReminder(event, moment))
        return planned

    def reschedule(
        self,
        subscription: Subscription,
        existing: Sequence[Notification],
        now: datetime,
    ) -> Reschedule:
        """Diff the desired schedule against existing notifications.

        Only notifications still waiting (status scheduled) are touched:
        - kept as is if they are still in the plan (repeated requests change nothing);
        - cancelled if the plan no longer has them;
        - planned reminders without a waiting notification are created.
        Sent, failed and cancelled notifications are history and never change.
        """
        desired = {reminder.key: reminder for reminder in self.plan(subscription, now)}
        waiting = [n for n in existing if n.is_pending]

        to_cancel = [n for n in waiting if (n.event_name, n.scheduled_for) not in desired]
        kept = {(n.event_name, n.scheduled_for) for n in waiting} - {
            (n.event_name, n.scheduled_for) for n in to_cancel
        }
        to_create = [
            reminder.to_notification(subscription.id)
            for key, reminder in desired.items()
            if key not in kept
        ]
        return Reschedule(to_cancel=to_cancel, to_create=to_create)

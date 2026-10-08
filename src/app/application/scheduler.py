"""Reminder scheduler: turns reminders whose time has come into events."""

from dataclasses import dataclass

from app.application.events import reminder_due_event
from app.application.ports import Clock, UnitOfWorkFactory
from app.domain.enums import NotificationEvent, SubscriptionStatus


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    enqueued: int = 0
    expired_subscriptions: int = 0


class ReminderScheduler:
    """One iteration = one transaction:

    1. lock a batch of due scheduled reminders (FOR UPDATE SKIP LOCKED);
    2. mark each `enqueued` and write a `notification.*` event to the outbox;
    3. on the expiry reminder, mark the subscription `expired` if it was not renewed;
    4. commit.

    Delivery then follows the usual path: relay → RabbitMQ → notification consumer.
    A reminder cancelled by a subscription update is never picked up (status check).
    """

    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock, batch_size: int = 100) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._batch_size = batch_size

    async def run_once(self) -> SchedulerResult:
        now = self._clock.now()
        expired = 0
        async with self._uow_factory() as uow:
            due = await uow.notifications.claim_due(self._batch_size, now)
            for notification in due:
                notification.enqueue()
                await uow.outbox.add(reminder_due_event(notification))

                if notification.event_name is NotificationEvent.EXPIRED:
                    subscription = await uow.subscriptions.get(
                        notification.subscription_id, for_update=True
                    )
                    if (
                        subscription is not None
                        and subscription.status is SubscriptionStatus.ACTIVE
                        and subscription.expected_expires_on <= now
                    ):
                        subscription.expire()
                        await uow.subscriptions.update(subscription)
                        expired += 1

            await uow.notifications.update_many(due)
            await uow.commit()
        return SchedulerResult(enqueued=len(due), expired_subscriptions=expired)

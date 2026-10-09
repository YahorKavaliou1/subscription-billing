"""Use cases run by message consumers: deliver notifications, renew subscriptions.

Delivery is at-least-once, so every use case is idempotent: a redelivered message
is recognised (inbox, notification status) and changes nothing.
"""

import uuid
from enum import StrEnum

from app.application.ports import (
    Clock,
    NotificationSender,
    OutgoingNotification,
    UnitOfWork,
    UnitOfWorkFactory,
)
from app.contracts.events import PaymentResultEvent, ReminderDueEvent
from app.domain.entities import Notification
from app.domain.enums import NotificationStatus, PaymentStatus
from app.domain.policies import NotificationSchedulePolicy
from app.domain.value_objects import format_decimal

NOTIFICATIONS_CONSUMER = "notifications"
RENEWAL_CONSUMER = "renewal"


class Outcome(StrEnum):
    PROCESSED = "processed"
    DUPLICATE = "duplicate"  # already handled earlier: nothing to do
    SKIPPED = "skipped"  # nothing to do for a legitimate reason (cancelled, unknown...)


class DeliveryFailedError(Exception):
    """The notification channel failed; the attempt is recorded, the message must be retried."""


class RenewSubscriptionOnPayment:
    """`payment.succeeded` → extend the subscription by `day_count` days and reschedule reminders.

    The inbox row and the renewal are committed in one transaction, so a redelivered
    message can never renew twice.
    """

    def __init__(
        self, uow_factory: UnitOfWorkFactory, policy: NotificationSchedulePolicy, clock: Clock
    ) -> None:
        self._uow_factory = uow_factory
        self._policy = policy
        self._clock = clock

    async def execute(self, message_id: str, event: PaymentResultEvent) -> Outcome:
        if event.status is not PaymentStatus.SUCCEEDED:
            return Outcome.SKIPPED

        async with self._uow_factory() as uow:
            if not await uow.inbox.add(RENEWAL_CONSUMER, message_id):
                return Outcome.DUPLICATE

            subscription = await uow.subscriptions.get(event.subscription_id, for_update=True)
            if subscription is None:
                # Cannot happen through the API (payments require a subscription);
                # recorded in the inbox so the message is not retried forever.
                await uow.commit()
                return Outcome.SKIPPED

            # From the payment time, not the processing time: retries give the same result
            subscription.renew(paid_at=event.occurred_at)
            await uow.subscriptions.update(subscription)
            existing = await uow.notifications.list_for_subscription(subscription.id)
            reschedule = self._policy.reschedule(subscription, existing, self._clock.now())
            for notification in reschedule.to_cancel:
                notification.cancel()
            await uow.notifications.update_many(reschedule.to_cancel)
            await uow.notifications.add_many(reschedule.to_create)
            await uow.commit()
        return Outcome.PROCESSED


class DeliverNotification:
    """Send a notification to the user and record the result.

    Sending to an external channel cannot be part of a database transaction, so:
    1. transaction: find or create the notification; stop if it was already sent;
    2. send (outside any transaction);
    3. transaction: mark it sent and record the message in the inbox.
    A crash between 2 and 3 means the user may get the message twice (at-least-once);
    the notification id is passed to the sender as an idempotency key.
    """

    def __init__(
        self, uow_factory: UnitOfWorkFactory, sender: NotificationSender, clock: Clock
    ) -> None:
        self._uow_factory = uow_factory
        self._sender = sender
        self._clock = clock

    async def for_payment(
        self, message_id: str, event: PaymentResultEvent, *, final_attempt: bool
    ) -> Outcome:
        async with self._uow_factory() as uow:
            if await uow.inbox.exists(NOTIFICATIONS_CONSUMER, message_id):
                return Outcome.DUPLICATE
            payment = await uow.payments.get(event.payment_id)
            if payment is None:
                return Outcome.SKIPPED
            notification = await uow.notifications.get_for_payment(payment.id)
            if notification is None:
                notification = Notification.for_payment(payment, self._clock.now())
                await uow.notifications.add_many([notification])
            ready = await self._prepare(uow, notification, message_id)
            await uow.commit()
        if ready is None:
            return Outcome.DUPLICATE

        data = {
            "payment_id": str(event.payment_id),
            "amount": format_decimal(event.amount),
            "currency": event.currency,
        }
        return await self._send(ready, event.user_id, data, message_id, final_attempt)

    async def for_reminder(
        self, message_id: str, event: ReminderDueEvent, *, final_attempt: bool
    ) -> Outcome:
        async with self._uow_factory() as uow:
            if await uow.inbox.exists(NOTIFICATIONS_CONSUMER, message_id):
                return Outcome.DUPLICATE
            notification = await uow.notifications.get(event.notification_id)
            subscription = await uow.subscriptions.get(event.subscription_id)
            if notification is None or subscription is None:
                return Outcome.SKIPPED
            if notification.status is NotificationStatus.CANCELLED:
                # The expiry date changed after the reminder was queued
                await uow.inbox.add(NOTIFICATIONS_CONSUMER, message_id)
                await uow.commit()
                return Outcome.SKIPPED
            if notification.status is NotificationStatus.SCHEDULED:
                notification.enqueue()
                await uow.notifications.update_many([notification])
            ready = await self._prepare(uow, notification, message_id)
            await uow.commit()
        if ready is None:
            return Outcome.DUPLICATE

        data = {"expected_expires_on": subscription.expected_expires_on.isoformat()}
        return await self._send(ready, subscription.user_id, data, message_id, final_attempt)

    async def _prepare(
        self, uow: UnitOfWork, notification: Notification, message_id: str
    ) -> Notification | None:
        """Return the notification to send, or None if it is already in a final state."""
        if notification.status in (NotificationStatus.SENT, NotificationStatus.FAILED):
            await uow.inbox.add(NOTIFICATIONS_CONSUMER, message_id)
            return None
        return notification

    async def _send(
        self,
        notification: Notification,
        user_id: str,
        data: dict[str, str],
        message_id: str,
        final_attempt: bool,
    ) -> Outcome:
        try:
            await self._sender.send(
                OutgoingNotification(
                    notification_id=str(notification.id),
                    user_id=user_id,
                    subscription_id=notification.subscription_id,
                    event_name=notification.event_name,
                    scheduled_for=notification.scheduled_for,
                    data=data,
                )
            )
        except Exception as exc:
            await self._record_failure(notification.id, repr(exc), message_id, final_attempt)
            raise DeliveryFailedError(repr(exc)) from exc

        async with self._uow_factory() as uow:
            current = await uow.notifications.get(notification.id)
            if current is not None and current.status is NotificationStatus.ENQUEUED:
                current.mark_sent(self._clock.now())
                await uow.notifications.update_many([current])
            await uow.inbox.add(NOTIFICATIONS_CONSUMER, message_id)
            await uow.commit()
        return Outcome.PROCESSED

    async def _record_failure(
        self, notification_id: uuid.UUID, error: str, message_id: str, final_attempt: bool
    ) -> None:
        async with self._uow_factory() as uow:
            current = await uow.notifications.get(notification_id)
            if current is not None and current.status is NotificationStatus.ENQUEUED:
                current.record_failure(error, final=final_attempt)
                await uow.notifications.update_many([current])
            if final_attempt:
                await uow.inbox.add(NOTIFICATIONS_CONSUMER, message_id)
            await uow.commit()

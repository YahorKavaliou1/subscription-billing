"""Task items 1 and 2: create/update a subscription, read it with notification history."""

from app.application.dto import (
    SubscriptionView,
    UpsertSubscriptionCommand,
    UpsertSubscriptionResult,
)
from app.application.ports import Clock, DuplicateKeyError, UnitOfWorkFactory
from app.domain.entities import Notification, Subscription
from app.domain.errors import SubscriptionNotFoundError
from app.domain.policies import NotificationSchedulePolicy


class UpsertSubscription:
    """Create or update a subscription and keep its reminder schedule in sync.

    Idempotent: repeating the same request leaves the stored state unchanged.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        policy: NotificationSchedulePolicy,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._policy = policy
        self._clock = clock

    async def execute(self, command: UpsertSubscriptionCommand) -> UpsertSubscriptionResult:
        try:
            return await self._execute(command)
        except DuplicateKeyError:
            # A concurrent request created the same subscription first:
            # retry once, now as an update of the existing row
            return await self._execute(command)

    async def _execute(self, command: UpsertSubscriptionCommand) -> UpsertSubscriptionResult:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            subscription = await uow.subscriptions.get(command.subscription_id, for_update=True)
            created = subscription is None
            if subscription is None:
                subscription = Subscription(
                    id=command.subscription_id,
                    user_id=command.user_id,
                    day_count=command.day_count,
                    expected_expires_on=command.expected_expires_on,
                )
                # An expiry date in the past means the subscription is already over
                subscription.sync_status(now)
                await uow.subscriptions.add(subscription)
                existing: list[Notification] = []
            else:
                subscription.ensure_owned_by(command.user_id)
                subscription.update(
                    day_count=command.day_count,
                    expected_expires_on=command.expected_expires_on,
                    now=now,
                )
                await uow.subscriptions.update(subscription)
                existing = await uow.notifications.list_for_subscription(subscription.id)

            reschedule = self._policy.reschedule(subscription, existing, now)
            for notification in reschedule.to_cancel:
                notification.cancel()
            await uow.notifications.update_many(reschedule.to_cancel)
            await uow.notifications.add_many(reschedule.to_create)

            await uow.commit()

        view = SubscriptionView.build(subscription, [*existing, *reschedule.to_create])
        return UpsertSubscriptionResult(subscription=view, created=created)


class GetSubscription:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, subscription_id: str) -> SubscriptionView:
        async with self._uow_factory() as uow:
            subscription = await uow.subscriptions.get(subscription_id)
            if subscription is None:
                raise SubscriptionNotFoundError(subscription_id)
            notifications = await uow.notifications.list_for_subscription(subscription_id)
        return SubscriptionView.build(subscription, notifications)

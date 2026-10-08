from datetime import UTC, datetime, timedelta

import pytest

from app.domain.entities import Notification, Subscription
from app.domain.enums import NotificationEvent, NotificationStatus
from app.domain.errors import InvalidValueError
from app.domain.policies import NotificationSchedulePolicy

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
POLICY = NotificationSchedulePolicy([3, 1, 0])


def make_subscription(expires: datetime = EXPIRES) -> Subscription:
    return Subscription(id="sub-1", user_id="user-1", day_count=30, expected_expires_on=expires)


def materialize(subscription: Subscription) -> list[Notification]:
    """Notifications as they would be stored after the first scheduling."""
    return POLICY.reschedule(subscription, [], NOW).to_create


class TestPlan:
    def test_reminders_at_offsets(self) -> None:
        planned = POLICY.plan(make_subscription(), NOW)

        assert [(p.event_name, p.scheduled_for) for p in planned] == [
            (NotificationEvent.EXPIRING_SOON, EXPIRES - timedelta(days=3)),
            (NotificationEvent.EXPIRING_SOON, EXPIRES - timedelta(days=1)),
            (NotificationEvent.EXPIRED, EXPIRES),
        ]

    def test_past_moments_are_skipped(self) -> None:
        expires = NOW + timedelta(days=2)

        planned = POLICY.plan(make_subscription(expires), NOW)

        assert [p.scheduled_for for p in planned] == [expires - timedelta(days=1), expires]

    def test_already_expired_subscription_gets_nothing(self) -> None:
        assert POLICY.plan(make_subscription(NOW - timedelta(seconds=1)), NOW) == []

    def test_moment_equal_to_now_is_kept(self) -> None:
        planned = POLICY.plan(make_subscription(NOW), NOW)

        assert [p.event_name for p in planned] == [NotificationEvent.EXPIRED]

    def test_offsets_are_normalized(self) -> None:
        assert NotificationSchedulePolicy([0, 7, 1, 7]).offsets_days == (7, 1, 0)

    @pytest.mark.parametrize("offsets", [[], [3, -1]])
    def test_invalid_offsets(self, offsets: list[int]) -> None:
        with pytest.raises(InvalidValueError):
            NotificationSchedulePolicy(offsets)

    def test_rejects_naive_now(self) -> None:
        with pytest.raises(InvalidValueError):
            POLICY.plan(make_subscription(), datetime(2026, 10, 8))  # noqa: DTZ001


class TestReschedule:
    def test_new_subscription_creates_all_reminders(self) -> None:
        result = POLICY.reschedule(make_subscription(), [], NOW)

        assert result.to_cancel == []
        assert len(result.to_create) == 3
        assert all(n.status is NotificationStatus.SCHEDULED for n in result.to_create)
        assert len({n.dedup_key for n in result.to_create}) == 3

    def test_repeated_request_changes_nothing(self) -> None:
        subscription = make_subscription()
        existing = materialize(subscription)

        assert POLICY.reschedule(subscription, existing, NOW).is_empty

    def test_moved_expiry_cancels_old_and_creates_new(self) -> None:
        existing = materialize(make_subscription())
        moved = make_subscription(EXPIRES + timedelta(days=30))

        result = POLICY.reschedule(moved, existing, NOW)

        assert {n.id for n in result.to_cancel} == {n.id for n in existing}
        assert [n.scheduled_for for n in result.to_create] == [
            moved.expected_expires_on - timedelta(days=3),
            moved.expected_expires_on - timedelta(days=1),
            moved.expected_expires_on,
        ]

    def test_history_is_never_touched(self) -> None:
        subscription = make_subscription()
        sent, cancelled, waiting = materialize(subscription)
        sent.enqueue()
        sent.mark_sent(NOW)
        cancelled.cancel()

        moved = make_subscription(EXPIRES + timedelta(days=30))
        result = POLICY.reschedule(moved, [sent, cancelled, waiting], NOW)

        # Only the waiting notification is cancelled; sent and cancelled stay as they are
        assert result.to_cancel == [waiting]

    def test_moving_back_reuses_the_same_reminder_keys(self) -> None:
        original = materialize(make_subscription())
        for notification in original:
            notification.cancel()

        result = POLICY.reschedule(make_subscription(), original, NOW)

        # Cancelled rows do not block the reminders; the DB unique index ignores them
        assert {n.dedup_key for n in result.to_create} == {n.dedup_key for n in original}

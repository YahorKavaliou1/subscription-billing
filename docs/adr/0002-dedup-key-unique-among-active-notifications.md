# 0002. Notification `dedup_key` is unique only among non-cancelled rows

- Status: Accepted
- Date: 2026-10-08
- Implementation: **Done**

## Context

Each notification has a `dedup_key` so it is never created twice
(e.g. `reminder:<subscription>:<event>:<time>`, `payment:<payment_id>:<event>`).
Cancelled reminders stay in the history. If a subscription's expiry moves A → B → A,
the new reminder for A gets the same key as the cancelled one, and a global UNIQUE
constraint would reject it.

## Options

1. Global UNIQUE on `dedup_key`; add a random suffix to every reminder key.
2. Global UNIQUE; delete cancelled reminders instead of keeping them.
3. **Partial unique index: unique among rows with `status <> 'cancelled'`.**

## Decision

Option 3: `uq_notifications_dedup_key_active ... WHERE status <> 'cancelled'`
(second migration).

## Consequences

- \+ Keys stay deterministic, so duplicates of active notifications are still impossible.
- \+ Full history is kept, as required by the "notification history" endpoint.
- − Downgrading this migration fails if active and cancelled rows share a key.

## Where in code

`NotificationModel` (`infrastructure/db/models/notification.py`), migration
`*_dedup_key_unique_among_active_.py`, `PlannedReminder.to_notification()` (`domain/policies.py`).
Tests: `test_notification_dedup_key_is_unique`, `test_reschedule_cancels_and_creates`.

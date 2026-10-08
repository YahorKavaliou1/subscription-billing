# 0005. Notification schedule is stored in the database

- Status: Accepted
- Date: 2026-10-08
- Implementation: **Data model and policy done**; scheduler process planned

## Context

Reminders are sent days or weeks after a subscription is created, and they are
cancelled or moved whenever the expiry date changes. The task also requires a history
of scheduled and sent notifications.

## Options

1. RabbitMQ delayed messages (delayed-message plugin or per-message TTL): a scheduled
   message cannot be cancelled or listed, and there is no history.
2. **Notifications table as the source of truth**; a scheduler periodically picks due rows
   and turns them into outbox events.

## Decision

Option 2. Rescheduling is a diff between the desired and existing schedule
(`NotificationSchedulePolicy.reschedule()`): unchanged reminders are kept, obsolete ones
are cancelled, missing ones are created.

## Consequences

- \+ Cancel, reschedule and history are ordinary SQL updates and queries.
- \+ Several scheduler instances can run safely with `FOR UPDATE SKIP LOCKED`.
- − Sending precision equals the polling interval (default 5 seconds).
- − Constant light polling load on the database (served by a partial index).

## Where in code

Done: `NotificationModel` with `ix_notifications_due`, `NotificationSchedulePolicy`,
`UpsertSubscription`. Planned: `workers/scheduler.py`.

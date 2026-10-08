# 0003. `day_count` is the subscription period length

- Status: Accepted
- Date: 2026-10-08
- Implementation: **Domain done**; triggered by the renewal consumer (planned)

## Context

The task passes `day_count` with a subscription but does not define it. Two readings are possible.

## Options

1. **Length of one paid period in days**: a successful payment extends `expected_expires_on`
   by `day_count` days.
2. How many days before expiry the user is notified.

## Decision

Option 1. Reminder timing is configured separately (`APP_NOTIFICATION_OFFSETS_DAYS`,
default 3 days, 1 day and on the expiry date), so reading 2 is still covered.

## Consequences

- \+ Renewal after payment has a clear rule.
- \+ Both behaviours are available; only the field meaning is fixed.
- − If the intended meaning was option 2, `day_count` would need to feed the reminder offsets.

## Where in code

`Subscription.renew()` (`domain/entities.py`), `NotificationSchedulePolicy` (`domain/policies.py`).
Planned: the renewal consumer calls `renew()` on `payment.succeeded`.

# 0003. `day_count` is the subscription period length

- Status: Accepted
- Date: 2026-10-08
- Implementation: **Done**

## Context

The task passes `day_count` with a subscription but does not define it. Two readings are possible.

## Options

1. **Length of one paid period in days**: a successful payment extends `expected_expires_on`
   by `day_count` days.
2. How many days before expiry the user is notified.

## Decision

Option 1. Reminder timing is configured separately (`APP_NOTIFICATION_OFFSETS_DAYS`,
default 3 days, 1 day and on the expiry date), so reading 2 is still covered.

### Renewal rules

- Range: 1 to 3650 days. Larger values are rejected with `422`; they are not real billing
  periods and would overflow date arithmetic during renewal.
- An **active** subscription is extended from its current `expected_expires_on`, so paying
  early loses nothing.
- An **expired** subscription starts a new period at the payment time (`occurred_at` of the
  payment event), not from a date in the past. The payment time, not the processing time, keeps
  the result the same when the event is processed late or redelivered.
- `PUT` with an `expected_expires_on` already in the past stores the subscription as `expired`
  and cancels its pending reminders.

### `PUT` and payments both change the expiry date

`PUT` sets an absolute date chosen by the client; a payment adds `day_count` days to whatever
date is stored when the renewal is processed. If a client renews a subscription by payment
**and** also sends a `PUT` with the already extended date, the period is added twice: for
example `PUT 2026-12-01`, a payment, then `PUT 2026-12-31` before the renewal is processed ends
at `2027-01-30`.

The rule for clients: a period is renewed either by a payment or by a `PUT`, not both. `PUT` is
meant for creating a subscription and for manual corrections. Making the two writers
commutative (for example, storing the paid periods and computing the expiry date from them)
is possible but needs the business to define how manual corrections and payments combine.

## Consequences

- \+ Renewal after payment has a clear rule.
- \+ Both behaviours are available; only the field meaning is fixed.
- − If the intended meaning was option 2, `day_count` would need to feed the reminder offsets.

## Where in code

`Subscription.renew()`, `Subscription.sync_status()` and `MAX_DAY_COUNT` (`domain/entities.py`),
`NotificationSchedulePolicy` (`domain/policies.py`), `RenewSubscriptionOnPayment`
(`application/use_cases/consumers.py`) run by the renewal consumer on `payment.succeeded`.
Tests: `test_early_payment_extends_from_the_expiry_date`,
`test_late_payment_starts_a_new_period_at_payment_time`,
`test_past_expiry_date_creates_an_expired_subscription`,
`test_successful_payment_notifies_user_and_renews_subscription`.

# Architecture Decision Records

Short records of decisions where there was a real choice: context, options, decision, consequences.

| ADR | Decision | Implementation |
|---|---|---|
| [0001](0001-payment-idempotency-by-provider-payment.md) | Payment idempotency by `provider_payment` | Done |
| [0002](0002-dedup-key-unique-among-active-notifications.md) | `dedup_key` unique only among non-cancelled notifications | Done |
| [0003](0003-day-count-is-subscription-period.md) | `day_count` is the subscription period length | Done |
| [0004](0004-transactional-outbox.md) | Transactional outbox for events | Done |
| [0005](0005-notification-schedule-in-database.md) | Notification schedule stored in the database | Model and policy done, scheduler planned |
| [0006](0006-at-least-once-delivery-idempotent-consumers.md) | At-least-once delivery, idempotent consumers | Done |

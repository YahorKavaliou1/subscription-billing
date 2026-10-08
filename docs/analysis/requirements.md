# Requirements

## Goal

Backend for subscription payments and notifications.
Key rule: **a payment and its event are saved together or not at all.**

## Functional requirements

1. **Create / update a subscription** (`user_id`, `subscription_id`, `day_count`, `expected_expires_on`)
   - schedule reminders before expiry (default: 3 days, 1 day, on the day);
   - on update, cancel unsent reminders and schedule new ones.

2. **Get a subscription** with its notification history
   (`event_name`, `scheduled_for`, `sent_at`, `status`).

3. **Record a payment** (`subscription_id`, `provider_payment`, `amount`, `currency`, `status`)
   - save the payment and its event in one transaction;
   - the same `provider_payment` sent twice does not create a duplicate;
   - on success, extend the subscription by `day_count` days;
   - notify the user about success or failure.

4. **Get a payment** with its status and whether its event and notification were sent.

## Non-functional requirements

- Events are delivered at least once; duplicates are ignored by consumers.
- The API keeps accepting payments when RabbitMQ is down.
- Money is stored as `Decimal`, time as UTC.
- Runs locally with one command: `docker compose up`.
- Covered by tests; CI runs lint, type checks and tests.

## Assumptions

- `day_count` is the subscription period length in days.
- A payment for an unknown subscription is rejected.
- Real email / push sending is out of scope; notifications are logged.
# 0001. Payment idempotency by `provider_payment`

- Status: Accepted
- Date: 2026-10-08
- Implementation: **Done**

## Context

Payment providers retry webhooks on timeouts and network errors, sometimes in parallel.
Each retry must not create a second payment, a second event or a second subscription renewal.

## Options

1. **Use the provider's payment id (`provider_payment`) as the idempotency key.**
2. Require a separate `Idempotency-Key` HTTP header from the caller.
3. No idempotency; deduplicate later in consumers.

## Decision

Option 1. `provider_payment` is unique in the database (`uq_payments_provider_payment`).

- Same `provider_payment` and same data → `200` with the original payment and event.
- Same `provider_payment` and different data → `409 Conflict`.
- Concurrent duplicates: the loser hits the unique constraint (`DuplicateKeyError`),
  re-reads the winner's row and returns it.

## Consequences

- \+ No extra contract for the caller; the provider already sends a stable id.
- \+ Protection is enforced by the database, not only by application code.
- − A provider that reuses ids across different payments would be rejected with `409`.

## Where in code

`RegisterPayment` (`application/use_cases/payments.py`), `Payment.is_same_request()`,
`translate_integrity_errors()` (`infrastructure/db/errors.py`).
Tests: `test_concurrent_duplicates_store_one_payment`, `test_concurrent_identical_payments_over_http`.

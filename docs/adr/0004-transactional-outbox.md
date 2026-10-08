# 0004. Transactional outbox for events

- Status: Accepted
- Date: 2026-10-08
- Implementation: **Write side done**; outbox relay planned

## Context

A payment and the event about it must be committed together or not at all (task item 3).
PostgreSQL and RabbitMQ cannot share one transaction.

## Options

1. Commit to the database, then publish to RabbitMQ: the event is lost if the process
   crashes in between.
2. Publish first, then commit: an event about a payment that may not exist.
3. Distributed transaction (2PC): not supported by RabbitMQ.
4. **Transactional outbox**: write the event to an `outbox_events` table in the same
   transaction; a separate relay publishes it and marks it `published`.

## Decision

Option 4.

## Consequences

- \+ A payment can never exist without its event (verified by a crash-injection test).
- \+ The API keeps accepting payments while RabbitMQ is down; events wait in the table.
- \+ Delivery state is queryable (`pending` / `published` / `dead`) for task item 4.
- − Delivery is at-least-once (see 0006) and has a small delay.
- − One more process to run; the outbox table needs periodic cleanup.

## Where in code

Done: `RegisterPayment` writes the payment and `OutboxMessage` in one `UnitOfWork`;
`OutboxEventModel` with a partial index on pending rows; `SqlOutboxRepository`.
Test: `test_failure_after_payment_insert_rolls_back_everything`.
Planned: `workers/relay.py` (`FOR UPDATE SKIP LOCKED`, publisher confirms, retry with backoff).

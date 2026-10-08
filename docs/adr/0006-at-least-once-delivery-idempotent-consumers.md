# 0006. At-least-once delivery with idempotent consumers

- Status: Accepted
- Date: 2026-10-08
- Implementation: **Database schema done**; consumers planned

## Context

The outbox relay may publish an event and crash before marking it `published`;
RabbitMQ may redeliver a message after a consumer crash. Duplicates are therefore possible.

## Options

1. Exactly-once delivery: not achievable between a database and a message broker.
2. At-most-once (acknowledge before processing): messages can be lost.
3. **At-least-once and idempotent consumers.**

## Decision

Option 3.

- Every message carries `message_id` = outbox event id.
- A consumer records `(consumer, message_id)` in `inbox_messages` in the same transaction
  as its side effects; a repeated message hits the primary key and is skipped.
- Failed messages go to a retry queue via dead-lettering, then to a parking queue after
  the attempt limit, and are never silently dropped.

## Consequences

- \+ No lost events and no double renewal or duplicate notification records.
- − Sending to an external channel (email, push) is outside the transaction, so a user
  may rarely get the same message twice; `notification.id` is passed as an idempotency
  key where the channel supports it.

## Where in code

Done: `InboxMessageModel` (`infrastructure/db/models/inbox.py`),
`Notification.for_payment()` with a deterministic `dedup_key`, RabbitMQ retry and parking
queues in `infra/rabbitmq/definitions.json`.
Planned: consumers in `workers/consumers/`.

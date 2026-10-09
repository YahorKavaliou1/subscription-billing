# 0007. Outbox and inbox rows are kept; retention is deferred

- Status: Accepted
- Date: 2026-10-09
- Implementation: **Deliberately not implemented**

## Context

Both tables only grow:

- `outbox_events` gets a row for every payment and every due reminder. After publishing,
  the row stays with the status `published`.
- `inbox_messages` gets a row for every message a consumer processed, one row per consumer.

The rows are not just technical leftovers:

- `GET /api/v1/payments/{id}` reads the payment's outbox row to show whether its event was
  delivered (`event.status`, `published_at`, `last_error`). This is a feature from the task.
  Once the row is deleted, the API can no longer say whether the event was delivered.
- An inbox row is what makes a redelivered message a no-op. A message can come back long after
  it was first processed: a message moved back from a parking queue by an operator days later
  (there is no tool for this yet; it would be done with RabbitMQ's shovel or management UI), or
  an event published again by a relay that crashed before marking it and stayed down for a long
  time.
  If the inbox row is already gone, the message is processed again, and a payment could renew a
  subscription twice.

So the safe retention period depends on how long the delivery status must be visible to support
and finance, and how long parked messages may be replayed. These are business and operations
decisions, not technical ones.

## Options

1. **Delete old rows** with a periodic job (for example in the scheduler): published outbox rows
   and inbox rows older than N days. Simple, but N has to be agreed first.
2. **Partition the tables by month** and drop old partitions. Cheap at high volume, but more
   complex migrations.
3. **Archive** old rows to cheaper storage before deleting them. Keeps the audit trail, adds
   infrastructure.
4. **Keep everything for now** and choose 1–3 when the requirements are known.

## Decision

Option 4. No cleanup is implemented. The volume of a test assignment does not need it, and a
wrong retention period would silently break the payment status API or deduplication.

## Consequences

- \+ Delivery status and deduplication work for any age of data.
- \+ No retention period is invented without the business.
- − The tables grow without limit. Inserts stay fast: lookups use the primary keys and the
  partial index on pending events. But storage grows, and the backlog metrics query
  (`GROUP BY status` over the whole table) gets slower.
- − Retention has to be added before production volumes. Signals to watch:
  `billing_outbox_events{status="published"}` and the table sizes in PostgreSQL.

## When this is revisited

- Agree on how long the payment delivery status must be visible (support, disputes, audit).
- Agree on the longest time after which a parked message may still be replayed. Inbox retention
  must be longer than that.
- Then implement option 1 (enough up to tens of millions of rows) or option 2, with a test that
  the payment status API still behaves well for payments whose event row was removed.

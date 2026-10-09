
Backend for subscription payments and user notifications.

## What it does

- **Subscriptions.** Creates or updates a user's subscription and schedules reminders
  before it expires (3 days, 1 day, and on the expiry date). When the expiry date
  changes, pending reminders are rescheduled; sent ones stay in the history.
- **Payments.** Records a payment from the provider and emits an event about its result.
  A successful payment extends the subscription by its period; the user is notified
  about success or failure.
- **Diagnostics.** For any payment shows whether its event reached the message broker
  and whether the user was notified.

**Key guarantee:** a payment and its event are saved in one database transaction
(transactional outbox), so a payment is never recorded without its event. Events are
delivered at least once, even if RabbitMQ is down at the moment of payment; consumers
ignore duplicates. Repeated requests from the provider are idempotent.

## How it works

```
POST /payments ─▶ api ──one transaction──▶ PostgreSQL: payments + outbox_events (pending)
                                                │
                     outbox-relay ◀─────────────┘ publishes, marks "published"
                          │
                          ▼
                 RabbitMQ: billing.events
                    ├─ notifications.send ──▶ notification-worker ─▶ user is notified
                    └─ subscriptions.renewal ─▶ renewal-worker ───▶ subscription extended
```

**Processes**

| Process | What it does |
|---|---|
| `api` | Validates requests, saves subscriptions and payments. A payment and its event are written in one transaction ([ADR 0004](docs/adr/0004-transactional-outbox.md)) |
| `outbox-relay` | Publishes pending events to RabbitMQ and waits for the broker's confirmation. While RabbitMQ is down, events wait in the database |
| `notification-worker` | On `payment.succeeded` / `payment.failed`: creates the notification, sends it and stores `sent_at` |
| `renewal-worker` | On `payment.succeeded`: extends `expected_expires_on` by `day_count` days ([ADR 0003](docs/adr/0003-day-count-is-subscription-period.md)) and reschedules reminders |
| `scheduler` | Turns due reminders into events for `notification-worker` ([ADR 0005](docs/adr/0005-notification-schedule-in-database.md)) |

**What happens on failures**

- **RabbitMQ is down:** the API still accepts payments; the relay retries and publishes once the broker is back.
- **A message is delivered twice:** consumers detect it (`inbox_messages`, notification status) and do nothing,
  so a subscription is never renewed twice ([ADR 0006](docs/adr/0006-at-least-once-delivery-idempotent-consumers.md)).
- **Sending a notification fails:** the message goes to a retry queue and comes back after a delay.
  After `APP_CONSUMER_MAX_DELIVERY_ATTEMPTS` the notification is marked `failed` and the message is moved
  to a parking queue (`*.parking`) for manual review.
- **A message is malformed:** it is parked immediately, without retries.

Every step can be traced by one id: the `X-Request-ID` of the original HTTP request appears in the logs
of the API and both workers.

The state of each step is visible through the API: `GET /api/v1/payments/{id}` shows whether the event
was published (`event.status`) and whether the user was notified (`notification.sent_at`).

## Design decisions

Key architectural decisions are recorded as short ADRs (context, options, decision, consequences)
in [`docs/adr/`](docs/adr/README.md):

1. [Payment idempotency by `provider_payment`](docs/adr/0001-payment-idempotency-by-provider-payment.md)
2. [Notification `dedup_key` unique only among non-cancelled rows](docs/adr/0002-dedup-key-unique-among-active-notifications.md)
3. [`day_count` is the subscription period length](docs/adr/0003-day-count-is-subscription-period.md)
4. [Transactional outbox for events](docs/adr/0004-transactional-outbox.md)
5. [Notification schedule stored in the database](docs/adr/0005-notification-schedule-in-database.md)
6. [At-least-once delivery with idempotent consumers](docs/adr/0006-at-least-once-delivery-idempotent-consumers.md)

Requirements analysis: [`docs/analysis/requirements.md`](docs/analysis/requirements.md).
API schema: [`docs/openapi.json`](docs/openapi.json).

## Architecture

| Layer | Contents |
|---|---|
| `domain/` | Entities (`Subscription`, `Payment`, `Notification`), `Money`, reminder scheduling policy. Pure Python, no I/O |
| `application/` | Use cases (one per feature) and ports: `UnitOfWork`, repositories, `Clock`, `NotificationSender` |
| `infrastructure/` | PostgreSQL (SQLAlchemy, Alembic), RabbitMQ, notification senders, logging |
| `api/`, `workers/` | FastAPI endpoints; outbox relay, reminder scheduler and FastStream consumers |

Processes: `api` → PostgreSQL ← `scheduler`; `outbox-relay` → RabbitMQ →
`notification-worker`, `renewal-worker`.
## Quick start

Requirements: Docker with Compose v2.

```bash
git clone git@github.com:YahorKavaliou1/subscription-billing.git
cd subscription-billing
cp .env.example .env
docker compose up -d --build
```

Services:

| Service | URL |
|---|---|
| API | http://localhost:8000 (OpenAPI docs: `/docs`) |
| RabbitMQ management | http://localhost:15672 (credentials from `.env`) |
| PostgreSQL | `localhost:5432` (credentials from `.env`) |

Stop and remove data:

```bash
docker compose down -v
```

## Local development

Requirements: [uv](https://docs.astral.sh/uv/).

```bash
uv python install 3.12
uv sync                       # creates .venv with runtime + dev dependencies

# infrastructure only, the app runs from the host
docker compose up -d postgres rabbitmq rabbitmq-init
```

When running the app from the host, replace the `postgres` / `rabbitmq` hosts in
`.env` with `localhost`.

Common commands:

```bash
uv run alembic upgrade head          # apply migrations
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                        # unit + integration (needs Docker for testcontainers)
```

Without uv:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

`requirements*.txt` are exported from `uv.lock`; after changing dependencies run:

```bash
uv lock
uv export --no-dev --no-hashes --no-emit-project -o requirements.txt
uv export --only-group dev --no-hashes --no-emit-project -o requirements-dev.txt
```

## Configuration

All settings come from environment variables (see `.env.example`).
Application settings use the `APP_` prefix.

| Variable | Purpose |
|---|---|
| `APP_DATABASE_URL` | PostgreSQL DSN (`postgresql+asyncpg://...`) |
| `APP_RABBITMQ_URL` | RabbitMQ AMQP URL |
| `APP_API_KEY` | API key expected in the `X-API-Key` header |
| `APP_RELAY_*` | Outbox relay batch size, poll interval, retry limits |
| `APP_SCHEDULER_*` | Scheduler batch size and poll interval |
| `APP_NOTIFICATION_OFFSETS_DAYS` | Reminder offsets before expiry, in days |
| `APP_CONSUMER_*` | Consumer delivery attempts and prefetch |
| `APP_NOTIFICATION_SENDER` | Notification sender implementation (`log`, `email`) |

## API

Base URL: `http://localhost:8000`. Interactive docs: `/docs`.

### Headers

| Header | Required | Purpose |
|---|---|---|
| `X-API-Key` | Yes, for `/api/v1/*` | Access key. Must match `APP_API_KEY` from `.env`; otherwise `401` |
| `X-Request-ID` | No | Your id for the request. Returned in the response and written to all logs of this request. Generated if missing |
| `Content-Type: application/json` | For `PUT` / `POST` | Request body format |

The response always contains `X-Request-ID`. `POST /payments` also returns `Location` with the payment URL.

### Endpoints

**1. Create or update a subscription.** Schedules reminders before expiry.
Returns `201` if created, `200` if updated.

```bash
curl -X PUT localhost:8000/api/v1/subscriptions/sub-1 \
  -H 'X-API-Key: change-me' -H 'Content-Type: application/json' \
  -d '{"user_id": "user-1", "day_count": 30, "expected_expires_on": "2026-12-01T12:00:00Z"}'
```

**2. Get a subscription** with the history of its notifications.

```bash
curl localhost:8000/api/v1/subscriptions/sub-1 -H 'X-API-Key: change-me'
```

**3. Record a payment.** The payment and its event are saved together.
Returns `201` for a new payment, `200` if the same payment was already recorded,
`409` if the same `provider_payment` comes with different data.

```bash
curl -X POST localhost:8000/api/v1/payments \
  -H 'X-API-Key: change-me' -H 'Content-Type: application/json' \
  -d '{"subscription_id": "sub-1", "provider_payment": "pi_123", "amount": "9.99", "currency": "EUR", "status": "succeeded"}'
```

**4. Get payment status** and whether its event and notification were delivered.

```bash
curl localhost:8000/api/v1/payments/<payment_id> -H 'X-API-Key: change-me'
curl 'localhost:8000/api/v1/payments?provider_payment=pi_123' -H 'X-API-Key: change-me'
```

In the response, `event.status` shows whether the event reached RabbitMQ
(`pending` → `published`), and `notification.sent_at` shows when the user was notified.

**Health checks** (no key needed): `GET /health/live`, `GET /health/ready`.

### Errors

Errors use one format (`application/problem+json`):

```json
{"title": "Not Found", "status": 404, "detail": "Subscription 'sub-9' not found", "instance": "/api/v1/subscriptions/sub-9"}
```

| Status | Meaning |
|---|---|
| `401` | Missing or wrong `X-API-Key` |
| `404` | Subscription or payment not found |
| `409` | Conflict: subscription belongs to another user, or payment data differs from the first request |
| `422` | Invalid input; the `errors` field lists the bad fields |

Rules: dates must include a timezone; `amount` is a positive number with at most 4 decimals
(send it as a string to keep it exact); `currency` is a 3-letter code.

## CI

GitHub Actions runs on every push to `main` and on pull requests:

- lock file check, ruff lint and format check, mypy;
- pytest with coverage (PostgreSQL and RabbitMQ via testcontainers);
- `docker compose config` validation and Docker image build.

## Tests

```bash
uv run pytest                      # all tests; integration tests start PostgreSQL via testcontainers (Docker required)
uv run pytest tests/unit           # fast, no Docker
TEST_DATABASE_URL=postgresql+asyncpg://billing:billing@localhost:5432/billing_test uv run pytest
                                   # reuse an existing database instead of a container
```

**Unit tests** (`tests/unit/`): pure Python, no database, run in about a second.

| File | What it checks |
|---|---|
| `test_config.py` | Settings load from environment, required variables, validation, secrets hidden in `repr` |
| `test_logging.py` | JSON log format, `correlation_id` in every record, stdlib loggers share the format |
| `test_domain_value_objects.py` | `Money`: positive exact `Decimal`, ISO currency, no floats; timezone-aware datetimes |
| `test_domain_entities.py` | Subscription invariants, ownership and renewal; payment idempotency check; notification state machine |
| `test_domain_policies.py` | Reminder schedule: offsets, skipping past moments, rescheduling without touching history |
| `test_use_cases_subscriptions.py` | Create/update/read a subscription: idempotency, rescheduling, ownership, concurrent create |
| `test_use_cases_payments.py` | Payment + event saved together, rollback on failure, idempotent replay, conflicts, payment status |

Use-case tests run against in-memory fakes (`tests/unit/fakes.py`) that commit state only on `commit()`, like a real transaction.

**Integration tests** (`tests/integration/`): real PostgreSQL, schema created by Alembic migrations.

| File | What it checks |
|---|---|
| `test_migrations.py` | Migrations match the ORM models; downgrade and upgrade are repeatable |
| `test_schema.py` | Database constraints: CHECKs, unique keys, foreign keys, exact decimals, optimistic locking |
| `test_use_cases_db.py` | Use cases end to end: a crash between payment and event leaves neither; concurrent duplicate payments and subscription updates produce exactly one consistent result; delivery status of a payment |

## Observability

### Logs

Every process writes one JSON object per line to stdout. Application logs, uvicorn and FastStream all use the same format (`APP_LOG_JSON=false` gives colored console output instead).

Each record carries a `correlation_id`, so one request can be traced across all processes:

1. The API takes it from the `X-Request-ID` header or generates one, and returns it in the response.
2. It is stored with the outbox event and sent as the AMQP `correlation_id`.
3. The relay and the consumers log under the same id. Events not caused by a request (scheduler reminders) get their own id.

```bash
docker compose logs | grep <request-id>
```

| Event | Process | Meaning |
|---|---|---|
| `http.request` | api | access log: method, route, status, duration |
| `event.published` | outbox-relay | broker confirmed an outbox event |
| `event.publish_rejected` | outbox-relay | broker rejected it; the relay retries with backoff |
| `relay.broker_unavailable` | outbox-relay | RabbitMQ unreachable; events wait in the outbox |
| `scheduler.batch` | scheduler | due reminders moved to the outbox |
| `notification.sent` | notification-worker | notification delivered |
| `message.processed` | consumers | handled: `processed` / `duplicate` / `skipped` |
| `message.retry_scheduled` | consumers | failed; redelivered after the retry delay |
| `message.parked` | consumers | poison message or out of attempts; moved to `*.parking` |

FastStream's per-message `Received` / `Processed` lines are shown only with `APP_LOG_LEVEL=DEBUG`.

### Metrics

Prometheus format. The API serves `GET /metrics` on port 8000 (no API key, not in OpenAPI). Workers serve it on `APP_METRICS_PORT` (default `9100`, `0` disables it) inside the compose network.

| Metric | Source | What to watch |
|---|---|---|
| `billing_http_requests_total`, `billing_http_request_duration_seconds` | api | error rate, latency per route |
| `billing_outbox_events{status}` | api (from DB) | `dead` > 0 needs attention |
| `billing_outbox_oldest_pending_age_seconds` | api (from DB) | grows while the relay or RabbitMQ is down |
| `billing_notifications{status}` | api (from DB) | `failed` notifications |
| `billing_relay_events_total{result}`, `billing_relay_broker_unavailable_total` | outbox-relay | publish failures, broker outages |
| `billing_scheduler_reminders_enqueued_total`, `billing_scheduler_subscriptions_expired_total` | scheduler | reminder throughput |
| `billing_consumer_messages_total{queue,result}`, `billing_consumer_processing_seconds` | consumers | retries and parked messages |
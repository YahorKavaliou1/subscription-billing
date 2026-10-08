
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
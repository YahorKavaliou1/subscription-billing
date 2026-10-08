
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
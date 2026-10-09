# Subscription billing

Backend for subscriptions, payments and user notifications.
Python 3.12 · FastAPI · FastStream · asyncio · PostgreSQL · RabbitMQ.

## What it does

- **Subscriptions.** Creates or updates a subscription and schedules reminders before it
  expires: 3 days before, 1 day before, and on the expiry date. If the expiry date changes,
  pending reminders are rescheduled, and sent ones stay in the history.
- **Payments.** Records a payment from the provider together with an event about its result.
  A successful payment extends the subscription by `day_count` days. The user is notified
  about success or failure.
- **Status.** For any payment, shows whether its event reached RabbitMQ and whether the user
  was notified.

**Key guarantee:** a payment and its event are saved in one database transaction
(transactional outbox), so a payment can never be recorded without its event. Events are
delivered at least once, even if RabbitMQ was down when the payment arrived. Consumers ignore
duplicates, and repeated requests from the provider are idempotent.

> Notification delivery is a stub: `notification.sent` is written to the log
> (`APP_NOTIFICATION_SENDER=log`). A real channel only needs a new `NotificationSender`
> implementation; retries and failure tracking already work.

## How it works

```
POST /payments ─▶ api ──one transaction──▶ PostgreSQL: payments + outbox_events
                                                 ▲              │
             scheduler: due reminders ──▶ outbox ┘              ▼
                                                     outbox-relay (publish + confirm)
                                                                │
                                                     RabbitMQ: billing.events
                                     ┌──────────────────────────┴───────────────────┐
                             notifications.send                          subscriptions.renewal
                                     ▼                                              ▼
                             notification-worker                            renewal-worker
                             notifies the user                              extends the subscription
```

| Process | Role |
|---|---|
| `api` | REST API. Saves subscriptions and payments; a payment and its event go in one transaction |
| `outbox-relay` | Publishes pending outbox events to RabbitMQ and waits for the broker's confirmation |
| `scheduler` | Moves due reminders to the outbox; marks a subscription `expired` on its expiry date |
| `notification-worker` | Sends payment results and reminders, stores `sent_at` |
| `renewal-worker` | On `payment.succeeded` extends the subscription and reschedules its reminders |

**On failures:**

- **RabbitMQ is down:** the API still accepts payments. Events wait in the outbox and are
  published once the broker is back.
- **Duplicate delivery:** consumers detect it (`inbox_messages`) and do nothing, so a payment
  never renews a subscription twice.
- **Sending fails:** the message is retried after 60 s. After
  `APP_CONSUMER_MAX_DELIVERY_ATTEMPTS` the notification is marked `failed`, and the message is
  moved to a `*.parking` queue. Malformed messages are parked right away.
- **PostgreSQL is down:** the API answers `503` with `Retry-After`. Workers keep retrying
  and recover without a restart.

Design decisions are recorded as ADRs in [`docs/adr/`](docs/adr/README.md): outbox, idempotency, 
delivery guarantees, reminder storage, and why old outbox/inbox rows are not deleted yet.

## Quick start

Requirements: Docker with Compose v2.

```bash
git clone git@github.com:YahorKavaliou1/subscription-billing.git
cd subscription-billing
cp .env.example .env
docker compose up -d --build
curl localhost:8000/health/ready        # {"status":"ok",...}
```

| Service | Address |
|---|---|
| API | http://localhost:8000, interactive docs at `/docs` |
| RabbitMQ management | http://localhost:15672 (credentials from `.env`) |
| PostgreSQL | `localhost:5432` (credentials from `.env`) |

Logs: `docker compose logs -f`. Stop: `docker compose down`. Stop and delete data: `docker compose down -v`.

If a port is already taken by another project, change `POSTGRES_HOST_PORT`, `RABBITMQ_HOST_PORT`, `RABBITMQ_MANAGEMENT_HOST_PORT` or `API_HOST_PORT` in `.env`.

## API

Every `/api/v1/*` request must include the header `X-API-Key: <key>`, where the key is `APP_API_KEY` from `.env` 
(`change-me` by default). Without it the API returns `401`.
You can also send `X-Request-ID: <any id>`, for example `demo-1`. 
The API returns this id in the response and writes it to every log line about the request, 
in the API and in the workers. Then `docker compose logs | grep demo-1` shows the whole path of one payment. 
If you don't send it, the API generates an id.

| Method and path | Purpose | Responses |
|---|---|---|
| `PUT /api/v1/subscriptions/{id}` | Create or update a subscription, schedule reminders | `201` created, `200` updated |
| `GET /api/v1/subscriptions/{id}` | Subscription with its notification history | `200`, `404` |
| `POST /api/v1/payments` | Record a payment and its event | `201` new, `200` repeat of the same payment, `409` same `provider_payment` with other data |
| `GET /api/v1/payments/{id}` | Payment, event delivery and notification status | `200`, `404` |
| `GET /api/v1/payments?provider_payment=…` | The same, by the provider's payment id | `200`, `404` |
| `GET /health/live`, `GET /health/ready` | Liveness; readiness (database reachable) | `200`, `503` |
| `GET /metrics` | Prometheus metrics | `200` |

```bash
KEY=change-me
curl -X PUT localhost:8000/api/v1/subscriptions/sub-1 \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"user_id": "user-1", "day_count": 30, "expected_expires_on": "2026-12-01T12:00:00Z"}'

curl -X POST localhost:8000/api/v1/payments \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -H 'X-Request-ID: demo-1' \
  -d '{"subscription_id": "sub-1", "provider_payment": "pi_123", "amount": "9.99", "currency": "EUR", "status": "succeeded"}'

curl 'localhost:8000/api/v1/payments?provider_payment=pi_123' -H "X-API-Key: $KEY"
```

In the payment status, `event.status` goes `pending` → `published` when RabbitMQ confirms the event,
and `notification.sent_at` shows when the user was notified.

Errors use `application/problem+json`, for example
`{"title": "Not Found", "status": 404, "detail": "Subscription 'sub-9' not found", ...}`.
The codes are `401` (wrong key), `404`, `409` (conflict), `422` (invalid input, with the bad fields
in `errors`) and `503` (database unavailable, retry later). Dates must include a timezone.
Send `amount` as a string to keep it exact (positive, at most 4 decimals). The full schema is in
[`docs/openapi.json`](docs/openapi.json).

## Observability

### Logs

All services write their logs as JSON, one record per line. View them with `docker compose logs -f`.

Every record about a request contains its `correlation_id`. This is the `X-Request-ID` you sent, or the id the API generated. The id follows the payment from service to service, so one command shows the whole path:

```bash
docker compose logs | grep demo-1
```

You will see:

1. `http.request` from the API: the payment was saved;
2. `event.published` from the relay: RabbitMQ received the event;
3. `notification.sent` from the notification worker: the user was notified;
4. `message.processed` from both workers: the event was handled.

### Metrics

Metrics are in the Prometheus format. The main ones come from the API:

```bash
curl localhost:8000/metrics
```

They show the number of requests per endpoint, the number of events and notifications in each status, and `billing_outbox_oldest_pending_age_seconds`, which says how long the oldest unsent event has been waiting. If this number keeps growing, the relay or RabbitMQ is down.

Each worker also has its own metrics (published and failed events, retried and parked messages, sent reminders). They are available on port `9100` inside the container, for example:

```bash
docker compose exec outbox-relay python -c "import urllib.request as u; print(u.urlopen('http://localhost:9100/metrics').read().decode())"
```

## Development

Requirements: [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                                               # .venv with runtime + dev dependencies
docker compose up -d postgres rabbitmq rabbitmq-init  # infrastructure only
uv run alembic upgrade head
uv run python -m app.api.main                         # or app.workers.relay / app.workers.scheduler /
                                                      # app.workers.consumers.notifications / .renewal
```

When the app runs on the host, replace the `postgres` / `rabbitmq` hosts in `.env` with `localhost`.

Checks (the same as in CI):

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run python -m app.api.export_openapi --check     # docs/openapi.json matches the code
```

Without uv: `pip install -r requirements.txt -r requirements-dev.txt` (both files are exported from `uv.lock`).

Shortcuts for all common commands are in the `Makefile`: run `make` to see them (`make up`, `make check`, `make test`, `make e2e`, ...).

## Configuration

Settings come from environment variables with the `APP_` prefix. See `.env.example` for the full list.

| Variable | Purpose |
|---|---|
| `APP_DATABASE_URL`, `APP_RABBITMQ_URL` | Connections |
| `APP_API_KEY` | Expected `X-API-Key` value |
| `APP_NOTIFICATION_OFFSETS_DAYS` | Reminder offsets before expiry, in days (default `[3,1,0]`) |
| `APP_RELAY_*`, `APP_SCHEDULER_*` | Batch sizes, poll intervals, relay retry limits |
| `APP_CONSUMER_*` | Delivery attempts before parking, prefetch |
| `APP_NOTIFICATION_SENDER` | `log` (default) or `email` (stub) |
| `APP_LOG_LEVEL`, `APP_LOG_JSON` | Log level; JSON or console format |
| `APP_METRICS_PORT` | Workers' metrics port (`0` disables it) |

## Tests

```bash
uv run pytest tests/unit     # fast, no Docker
uv run pytest                # + integration tests: real PostgreSQL and RabbitMQ via testcontainers
scripts/e2e.sh               # + end-to-end and failure tests against the docker compose stack
```

| Level | What it covers |
|---|---|
| Unit | Domain rules, use cases on in-memory fakes, HTTP layer, relay, scheduler, consumers, logging, metrics |
| Integration | Migrations and constraints; a crash between a payment and its event leaves neither; concurrent duplicates; the full chain payment → outbox → RabbitMQ → consumers, including retries, parking and duplicate deliveries |
| End-to-end | The running stack as a black box: happy paths, then RabbitMQ / relay / consumer / PostgreSQL outages and `SIGKILL` of the relay and consumers mid-stream. Nothing is lost, and 30 payments renew the subscription exactly 30 times |

To reuse existing services instead of testcontainers, set `TEST_DATABASE_URL` / `TEST_RABBITMQ_URL`.
End-to-end tests stop and kill containers, so run them only against a local or CI stack.

CI (GitHub Actions) runs on every push to `main` and on pull requests: lint, mypy and the OpenAPI
check; unit and integration tests with coverage; the Docker build; then the end-to-end tests.

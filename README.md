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
# Common commands. Run `make` or `make help` to see the list.

.DEFAULT_GOAL := help
.PHONY: help env install up down reset restart ps logs migrate \
        lint format typecheck openapi check test-unit test cov e2e requirements

COMPOSE ?= docker compose
UV      ?= uv

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- Setup -------------------------------------------------------------------

env: ## Create .env from .env.example (keeps an existing .env)
	@test -f .env || (cp .env.example .env && echo "Created .env")

install: ## Create .venv with runtime + dev dependencies
	$(UV) sync

# --- Stack (docker compose) --------------------------------------------------

up: env ## Build and start the whole stack in the background
	$(COMPOSE) up -d --build

down: ## Stop and remove containers (data is kept)
	$(COMPOSE) down --remove-orphans

reset: ## Stop and remove containers AND data volumes
	$(COMPOSE) down -v --remove-orphans

restart: down up ## Recreate the stack

ps: ## Show service status
	$(COMPOSE) ps

logs: ## Follow logs of all services (make logs s=api for one)
	$(COMPOSE) logs -f $(s)

migrate: ## Apply database migrations inside the stack
	$(COMPOSE) run --rm migrate

# --- Code quality ------------------------------------------------------------

lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Auto-format and fix lint issues
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## Mypy (strict)
	$(UV) run mypy

openapi: ## Regenerate docs/openapi.json from the code
	$(UV) run python -m app.api.export_openapi

check: lint typecheck ## Everything CI checks before tests
	$(UV) run python -m app.api.export_openapi --check

# --- Tests -------------------------------------------------------------------

test-unit: ## Unit tests (fast, no Docker)
	$(UV) run pytest tests/unit

test: ## Unit + integration tests (Docker needed for testcontainers)
	$(UV) run pytest

cov: ## Tests with coverage, fails below 80% as in CI
	$(UV) run pytest --cov --cov-report=term-missing --cov-fail-under=80

e2e: env ## End-to-end and failure tests against the compose stack (stops/kills containers)
	bash scripts/e2e.sh

# --- Dependencies ------------------------------------------------------------

requirements: ## Re-export requirements*.txt from uv.lock
	$(UV) lock
	$(UV) export --no-dev --no-hashes --no-emit-project -o requirements.txt
	$(UV) export --only-group dev --no-hashes --no-emit-project -o requirements-dev.txt

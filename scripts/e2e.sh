#!/usr/bin/env bash
# Run the end-to-end tests against the full docker compose stack.
#
#   scripts/e2e.sh            # build, start the stack, run all e2e tests, leave it running
#   scripts/e2e.sh --down     # same, then remove containers and volumes
#   scripts/e2e.sh -k smoke   # extra arguments go to pytest
#
# Fault tests stop and kill containers; do not point this at a shared environment.
set -euo pipefail
cd "$(dirname "$0")/.."

down=false
pytest_args=()
for arg in "$@"; do
  if [[ "$arg" == "--down" ]]; then down=true; else pytest_args+=("$arg"); fi
done

[[ -f .env ]] || cp .env.example .env

env_value() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true; }
api_port="$(env_value API_HOST_PORT)"
base_url="${E2E_BASE_URL:-http://localhost:${api_port:-8000}}"
api_key="${E2E_API_KEY:-$(env_value APP_API_KEY)}"

cleanup() {
  if $down; then docker compose down -v --remove-orphans; fi
}
trap cleanup EXIT

docker compose up --build -d

echo "Waiting for ${base_url}/health/ready ..."
for _ in $(seq 60); do
  if curl -fsS "${base_url}/health/ready" > /dev/null 2>&1; then ready=true; break; fi
  sleep 2
done
if [[ "${ready:-false}" != true ]]; then
  echo "API did not become ready in time" >&2
  docker compose ps
  docker compose logs --tail=50
  exit 1
fi

E2E_BASE_URL="$base_url" E2E_API_KEY="$api_key" \
  uv run pytest tests/e2e -v -p no:cacheprovider "${pytest_args[@]}"

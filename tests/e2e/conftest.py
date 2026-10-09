"""End-to-end tests against the running docker compose stack.

Black-box: the tests talk to the API over HTTP and control containers with
`docker compose`; they import nothing from the application.

    docker compose up --build -d --wait
    E2E_BASE_URL=http://localhost:8000 uv run pytest tests/e2e -v

Skipped unless E2E_BASE_URL is set. Every test uses fresh ids, so the stack
does not need to be empty, and fault tests restore the services they stop.

Environment:
    E2E_BASE_URL   API address, e.g. http://localhost:8000
    E2E_API_KEY    X-API-Key value (default: change-me, as in .env.example)
    E2E_COMPOSE    command used to control the stack (default: "docker compose")
"""

import os
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("E2E_BASE_URL", "")
API_KEY = os.environ.get("E2E_API_KEY", "change-me")
COMPOSE = shlex.split(os.environ.get("E2E_COMPOSE", "docker compose"))

# Payments, events and notifications normally take well under a second end to end;
# the margin covers broker reconnects and the relay's backoff after an outage.
DELIVERY_TIMEOUT = 30.0
RECOVERY_TIMEOUT = 120.0

_METRIC_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][\w:]*)(?:\{(?P<labels>[^}]*)\})? (?P<value>\S+)$")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.e2e)
            if not BASE_URL:
                item.add_marker(pytest.mark.skip(reason="E2E_BASE_URL is not set"))


def eventually[T](
    check: Callable[[], T | None],
    *,
    timeout: float = DELIVERY_TIMEOUT,
    interval: float = 0.5,
    message: str = "condition",
) -> T:
    """Poll until `check` returns a truthy value; return it."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, AssertionError, KeyError) as exc:
            last_error = exc  # e.g. the API is still restarting
        time.sleep(interval)
    raise AssertionError(f"{message} not met within {timeout:.0f}s (last error: {last_error!r})")


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Api:
    """Thin client for the public API."""

    def __init__(self, client: httpx.Client) -> None:
        self.http = client

    def upsert_subscription(
        self,
        subscription_id: str,
        *,
        expires: datetime,
        day_count: int = 30,
        user_id: str = "e2e-user",
    ) -> dict[str, Any]:
        response = self.http.put(
            f"/api/v1/subscriptions/{subscription_id}",
            json={
                "user_id": user_id,
                "day_count": day_count,
                "expected_expires_on": expires.isoformat(),
            },
        )
        assert response.status_code in (200, 201), response.text
        return dict(response.json())

    def subscription(self, subscription_id: str) -> dict[str, Any]:
        response = self.http.get(f"/api/v1/subscriptions/{subscription_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def pay(
        self,
        subscription_id: str,
        *,
        status: str = "succeeded",
        provider_payment: str | None = None,
        amount: str = "9.99",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self.http.post(
            "/api/v1/payments",
            json={
                "subscription_id": subscription_id,
                "provider_payment": provider_payment or unique("pi"),
                "amount": amount,
                "currency": "EUR",
                "status": status,
            },
            headers=headers,
        )

    def payment(self, payment_id: str) -> dict[str, Any]:
        response = self.http.get(f"/api/v1/payments/{payment_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def delivered_payment(
        self, payment_id: str, timeout: float = DELIVERY_TIMEOUT
    ) -> dict[str, Any]:
        """Wait until the payment's event is published and its notification sent."""

        def check() -> dict[str, Any] | None:
            view = self.payment(payment_id)
            event, notification = view["event"], view["notification"]
            done = (
                event is not None
                and event["status"] == "published"
                and notification is not None
                and notification["status"] == "sent"
            )
            return view if done else None

        return eventually(check, timeout=timeout, message=f"payment {payment_id} delivered")

    def metrics(self) -> dict[str, float]:
        """`/metrics` as {"name{labels}": value}."""
        response = self.http.get("/metrics")
        assert response.status_code == 200
        samples = {}
        for line in response.text.splitlines():
            match = _METRIC_LINE.match(line)
            if match:
                labels = match["labels"]
                key = f"{match['name']}{{{labels}}}" if labels else match["name"]
                samples[key] = float(match["value"])
        return samples


class Stack:
    """Starts, stops and kills compose services; reads their logs."""

    def _run(self, *args: str) -> str:
        result = subprocess.run(  # fixed command, test-controlled arguments
            [*COMPOSE, *args], check=True, capture_output=True, text=True, timeout=120
        )
        return result.stdout

    def stop(self, service: str) -> None:
        self._run("stop", service)

    def start(self, service: str) -> None:
        self._run("start", service)

    def kill(self, service: str) -> None:
        """SIGKILL: no graceful shutdown, in-flight work is lost mid-way."""
        self._run("kill", "-s", "SIGKILL", service)

    def logs(self, service: str) -> str:
        return self._run("logs", "--no-log-prefix", service)


@pytest.fixture(scope="session")
def api() -> Iterator[Api]:
    with httpx.Client(base_url=BASE_URL, headers={"X-API-Key": API_KEY}, timeout=10) as client:
        yield Api(client)


@pytest.fixture(scope="session")
def stack() -> Stack:
    return Stack()


@pytest.fixture
def subscription(api: Api) -> dict[str, Any]:
    """A fresh active subscription expiring in 30 days."""
    expires = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)
    return api.upsert_subscription(unique("sub"), expires=expires)

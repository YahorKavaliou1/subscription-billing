"""Prometheus metrics: /metrics endpoint, HTTP metrics, relay counters, backlog gauges."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from prometheus_client import REGISTRY

from app.api.main import create_app
from app.application.relay import RelayResult
from app.bootstrap import Container
from app.domain.enums import NotificationStatus, OutboxStatus
from app.domain.policies import NotificationSchedulePolicy
from app.infrastructure.db.stats import BacklogStats, publish_backlog
from app.workers.relay import RelayWorker
from tests.unit.fakes import FakeClock, FakeDatabase

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def container() -> Container:
    db = FakeDatabase(FakeClock())
    return Container(
        api_key="test-key",
        uow_factory=db.uow,
        policy=NotificationSchedulePolicy([3, 1, 0]),
        clock=db.clock,
    )


@pytest.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_metrics_endpoint_needs_no_api_key_and_is_hidden_from_openapi(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "billing_http_requests_total" in response.text
    assert "/metrics" not in (await client.get("/openapi.json")).json()["paths"]


async def test_http_requests_are_counted_by_route_template(client: httpx.AsyncClient) -> None:
    route = "/api/v1/payments/{payment_id}"
    before = sample("billing_http_requests_total", method="GET", route=route, status="404")

    for _ in range(2):
        await client.get(
            "/api/v1/payments/00000000-0000-0000-0000-000000000001",
            headers={"X-API-Key": "test-key"},
        )

    after = sample("billing_http_requests_total", method="GET", route=route, status="404")
    assert after - before == 2
    count = sample("billing_http_request_duration_seconds_count", method="GET", route=route)
    assert count >= 2


async def test_unknown_paths_share_one_label(client: httpx.AsyncClient) -> None:
    before = sample("billing_http_requests_total", method="GET", route="unmatched", status="404")

    await client.get("/wp-admin.php")
    await client.get("/.env")

    after = sample("billing_http_requests_total", method="GET", route="unmatched", status="404")
    assert after - before == 2


async def test_refreshers_run_on_scrape_and_failures_do_not_break_it(
    container: Container, client: httpx.AsyncClient
) -> None:
    calls: list[str] = []

    async def ok() -> None:
        calls.append("ok")

    async def broken() -> None:
        raise ConnectionError("database down")

    container.metrics_refreshers.extend([broken, ok])

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert calls == ["ok"]


def test_backlog_gauges() -> None:
    stats = BacklogStats(
        outbox={OutboxStatus.PENDING: 3, OutboxStatus.PUBLISHED: 10, OutboxStatus.DEAD: 1},
        oldest_pending_created_at=NOW - timedelta(seconds=90),
        notifications=dict.fromkeys(NotificationStatus, 0) | {NotificationStatus.SENT: 4},
    )

    publish_backlog(stats, NOW)

    assert sample("billing_outbox_events", status="pending") == 3
    assert sample("billing_outbox_events", status="dead") == 1
    assert sample("billing_outbox_oldest_pending_age_seconds") == 90
    assert sample("billing_notifications", status="sent") == 4
    assert sample("billing_notifications", status="failed") == 0

    # Nothing pending any more: the age drops to zero instead of keeping the old value
    publish_backlog(BacklogStats(stats.outbox, None, stats.notifications), NOW)
    assert sample("billing_outbox_oldest_pending_age_seconds") == 0


def test_relay_results_are_counted() -> None:
    before = {
        result: sample("billing_relay_events_total", result=result)
        for result in ("published", "failed", "dead")
    }
    unavailable_before = sample("billing_relay_broker_unavailable_total")

    RelayWorker._count(RelayResult(claimed=6, published=3, failed=3, dead=1))
    RelayWorker._count(RelayResult(claimed=1, broker_unavailable=True))

    def delta(result: str) -> float:
        return sample("billing_relay_events_total", result=result) - before[result]

    assert (delta("published"), delta("failed"), delta("dead")) == (3, 2, 1)
    assert sample("billing_relay_broker_unavailable_total") - unavailable_before == 1

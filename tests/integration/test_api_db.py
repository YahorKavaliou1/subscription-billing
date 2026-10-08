"""The four task scenarios through HTTP, with the real database behind the API."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.main import create_app
from app.application.ports import UnitOfWorkFactory
from app.bootstrap import Container
from app.domain.policies import NotificationSchedulePolicy

pytestmark = pytest.mark.integration

API_KEY = "integration-key"
EXPIRES = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)


@pytest.fixture
async def client(
    uow_factory: UnitOfWorkFactory, engine: AsyncEngine
) -> AsyncIterator[httpx.AsyncClient]:
    async def check_database() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    container = Container(
        api_key=API_KEY,
        uow_factory=uow_factory,
        policy=NotificationSchedulePolicy([3, 1, 0]),
        readiness_checks={"database": check_database},
    )
    transport = httpx.ASGITransport(app=create_app(container))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": API_KEY}
    ) as client:
        yield client


async def test_task_scenarios_end_to_end(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    # 1. Create a subscription and schedule notifications
    created = await client.put(
        "/api/v1/subscriptions/sub-1",
        json={"user_id": "user-1", "day_count": 30, "expected_expires_on": EXPIRES.isoformat()},
    )
    assert created.status_code == 201

    # 2. Subscription state with notification history
    subscription = (await client.get("/api/v1/subscriptions/sub-1")).json()
    assert [n["status"] for n in subscription["notifications"]] == ["scheduled"] * 3

    # 3. Record a payment: payment + event committed together
    paid = await client.post(
        "/api/v1/payments",
        json={
            "subscription_id": "sub-1",
            "provider_payment": "pi_123",
            "amount": "19.90",
            "currency": "usd",
            "status": "succeeded",
        },
        headers={"X-Request-ID": "e2e-req-1"},
    )
    assert paid.status_code == 201, paid.text
    payment = paid.json()["payment"]
    assert payment["currency"] == "USD"

    async with engine.connect() as connection:
        event = (
            await connection.execute(text("SELECT event_type, status, headers FROM outbox_events"))
        ).one()
    assert event.event_type == "payment.succeeded"
    assert event.status == "pending"
    assert event.headers == {"correlation_id": "e2e-req-1"}

    # 4. Payment status and delivery facts
    status = (await client.get(f"/api/v1/payments/{payment['id']}")).json()
    assert status["payment"]["amount"] == "19.9"
    assert payment["amount"] == "19.9"
    assert status["event"]["id"] == paid.json()["event_id"]
    assert status["event"]["status"] == "pending"
    assert status["notification"] is None


async def test_concurrent_identical_payments_over_http(client: httpx.AsyncClient) -> None:
    await client.put(
        "/api/v1/subscriptions/sub-1",
        json={"user_id": "user-1", "day_count": 30, "expected_expires_on": EXPIRES.isoformat()},
    )
    body = {
        "subscription_id": "sub-1",
        "provider_payment": "pi_dup",
        "amount": "5",
        "currency": "EUR",
        "status": "failed",
    }

    responses = await asyncio.gather(
        *(client.post("/api/v1/payments", json=body) for _ in range(5))
    )

    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 201]
    assert len({r.json()["payment"]["id"] for r in responses}) == 1


async def test_readiness_with_real_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok"}

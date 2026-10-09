"""HTTP layer on top of in-memory fakes: routing, status codes, error format, auth, headers."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api.main import create_app
from app.bootstrap import Container
from app.domain.entities import Subscription
from app.domain.policies import NotificationSchedulePolicy
from tests.unit.fakes import FakeClock, FakeDatabase

API_KEY = "test-key"
EXPIRES = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase(FakeClock())


@pytest.fixture
def container(db: FakeDatabase) -> Container:
    return Container(
        api_key=API_KEY,
        uow_factory=db.uow,
        policy=NotificationSchedulePolicy([3, 1, 0]),
        clock=db.clock,
    )


@pytest.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(container))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH
    ) as client:
        yield client


def subscription_body(**overrides: Any) -> dict[str, Any]:
    return {
        "user_id": "user-1",
        "day_count": 30,
        "expected_expires_on": EXPIRES.isoformat(),
    } | overrides


def payment_body(**overrides: Any) -> dict[str, Any]:
    return {
        "subscription_id": "sub-1",
        "provider_payment": "pay-1",
        "amount": "9.99",
        "currency": "EUR",
        "status": "succeeded",
    } | overrides


@pytest.fixture
def existing_subscription(db: FakeDatabase) -> None:
    db.state.subscriptions["sub-1"] = Subscription(
        id="sub-1", user_id="user-1", day_count=30, expected_expires_on=EXPIRES
    )


def assert_problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    assert body["title"]
    return body


class TestSubscriptions:
    async def test_create_then_update(self, client: httpx.AsyncClient) -> None:
        created = await client.put("/api/v1/subscriptions/sub-1", json=subscription_body())
        updated = await client.put(
            "/api/v1/subscriptions/sub-1", json=subscription_body(day_count=60)
        )

        assert created.status_code == 201
        assert updated.status_code == 200
        body = created.json()
        assert body["subscription_id"] == "sub-1"
        assert body["status"] == "active"
        assert [n["event_name"] for n in body["notifications"]] == [
            "subscription.expiring_soon",
            "subscription.expiring_soon",
            "subscription.expired",
        ]
        assert body["notifications"][0] == {
            "event_name": "subscription.expiring_soon",
            "scheduled_for": "2026-10-29T12:00:00Z",
            "sent_at": None,
            "status": "scheduled",
            "attempts": 0,
            "last_error": None,
        }
        assert updated.json()["day_count"] == 60

    async def test_get(self, client: httpx.AsyncClient) -> None:
        await client.put("/api/v1/subscriptions/sub-1", json=subscription_body())

        response = await client.get("/api/v1/subscriptions/sub-1")

        assert response.status_code == 200
        assert response.json()["expected_expires_on"] == "2026-11-01T12:00:00Z"
        assert len(response.json()["notifications"]) == 3

    async def test_get_unknown(self, client: httpx.AsyncClient) -> None:
        body = assert_problem(await client.get("/api/v1/subscriptions/nope"), 404)

        assert body["instance"] == "/api/v1/subscriptions/nope"
        assert "nope" in body["detail"]

    async def test_other_user_is_conflict(self, client: httpx.AsyncClient) -> None:
        await client.put("/api/v1/subscriptions/sub-1", json=subscription_body())

        response = await client.put(
            "/api/v1/subscriptions/sub-1", json=subscription_body(user_id="user-2")
        )

        assert_problem(response, 409)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"expected_expires_on": "2026-11-01T12:00:00"},  # no timezone
            {"day_count": 0},
            {"day_count": "thirty"},
            {"user_id": ""},
            {"unexpected": "field"},
        ],
    )
    async def test_validation(self, client: httpx.AsyncClient, overrides: dict[str, Any]) -> None:
        response = await client.put(
            "/api/v1/subscriptions/sub-1", json=subscription_body(**overrides)
        )

        body = assert_problem(response, 422)
        assert body["errors"]

    async def test_id_too_long(self, client: httpx.AsyncClient) -> None:
        response = await client.put(f"/api/v1/subscriptions/{'x' * 65}", json=subscription_body())

        assert_problem(response, 422)


@pytest.mark.usefixtures("existing_subscription")
class TestPayments:
    async def test_register(self, client: httpx.AsyncClient, db: FakeDatabase) -> None:
        response = await client.post("/api/v1/payments", json=payment_body())

        assert response.status_code == 201
        body = response.json()
        payment_id = body["payment"]["id"]
        assert response.headers["location"] == f"/api/v1/payments/{payment_id}"
        assert body["payment"]["amount"] == "9.99"
        assert body["payment"]["status"] == "succeeded"
        assert body["event_id"] == str(db.state.outbox[0].id)

    async def test_json_number_amount_is_exact(
        self, client: httpx.AsyncClient, db: FakeDatabase
    ) -> None:
        response = await client.post("/api/v1/payments", json=payment_body(amount=0.1))

        assert response.status_code == 201
        [payment] = db.state.payments.values()
        assert payment.money.amount == Decimal("0.1")

    async def test_replay_and_conflict(self, client: httpx.AsyncClient) -> None:
        first = await client.post("/api/v1/payments", json=payment_body())
        replay = await client.post("/api/v1/payments", json=payment_body())
        conflict = await client.post("/api/v1/payments", json=payment_body(amount="1.00"))

        assert replay.status_code == 200
        assert replay.json()["payment"]["id"] == first.json()["payment"]["id"]
        assert_problem(conflict, 409)

    async def test_unknown_subscription(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/v1/payments", json=payment_body(subscription_id="nope"))

        assert_problem(response, 404)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"amount": "0"},
            {"amount": "-5"},
            {"amount": "1.00001"},
            {"currency": "EU"},
            {"currency": "E1R"},  # passes the schema, rejected by the domain
            {"status": "pending"},
        ],
    )
    async def test_validation(self, client: httpx.AsyncClient, overrides: dict[str, Any]) -> None:
        assert_problem(await client.post("/api/v1/payments", json=payment_body(**overrides)), 422)

    async def test_status_by_id_and_provider_payment(self, client: httpx.AsyncClient) -> None:
        created = (await client.post("/api/v1/payments", json=payment_body())).json()

        by_id = await client.get(f"/api/v1/payments/{created['payment']['id']}")
        by_provider = await client.get("/api/v1/payments", params={"provider_payment": "pay-1"})

        assert by_id.status_code == by_provider.status_code == 200
        assert by_id.json() == by_provider.json()
        body = by_id.json()
        assert body["event"]["status"] == "pending"
        assert body["event"]["published_at"] is None
        assert body["event"]["event_type"] == "payment.succeeded"
        assert body["notification"] is None

    async def test_status_unknown(self, client: httpx.AsyncClient) -> None:
        assert_problem(await client.get(f"/api/v1/payments/{uuid.uuid4()}"), 404)
        assert_problem(await client.get("/api/v1/payments", params={"provider_payment": "x"}), 404)
        assert_problem(await client.get("/api/v1/payments/not-a-uuid"), 422)


class TestCrossCutting:
    @pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
    async def test_api_key_required(self, container: Container, headers: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anonymous:
            response = await anonymous.get("/api/v1/subscriptions/sub-1", headers=headers)

        assert_problem(response, 401)
        assert response.headers["www-authenticate"] == "ApiKey"

    async def test_unknown_route(self, client: httpx.AsyncClient) -> None:
        assert_problem(await client.get("/api/v1/nothing-here"), 404)

    async def test_request_id_is_generated_and_echoed(self, client: httpx.AsyncClient) -> None:
        generated = await client.get("/health/live")
        echoed = await client.get("/health/live", headers={"X-Request-ID": "abc-123"})
        unsafe = await client.get("/health/live", headers={"X-Request-ID": "bad id\n"})

        assert len(generated.headers["x-request-id"]) == 32
        assert echoed.headers["x-request-id"] == "abc-123"
        assert unsafe.headers["x-request-id"] != "bad id\n"

    async def test_health_is_public(self, container: Container) -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anonymous:
            live = await anonymous.get("/health/live")
            ready = await anonymous.get("/health/ready")

        assert live.json() == {"status": "ok"}
        assert ready.status_code == 200

    async def test_ready_reports_failed_dependency(
        self, container: Container, client: httpx.AsyncClient
    ) -> None:
        async def broken() -> None:
            raise ConnectionError("db down")

        async def healthy() -> None:
            return None

        container.readiness_checks = {"database": broken, "broker": healthy}

        response = await client.get("/health/ready")

        assert response.status_code == 503
        assert response.json() == {
            "status": "unavailable",
            "checks": {"database": "unavailable", "broker": "ok"},
        }

    async def test_openapi_documents_api_key(self, client: httpx.AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()

        assert "APIKeyHeader" in schema["components"]["securitySchemes"]
        assert "/api/v1/payments" in schema["paths"]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(111, "Connect call failed"),
        OperationalError("SELECT 1", {}, Exception("server closed the connection")),
    ],
)
async def test_database_outage_is_503_with_retry_after(error: Exception) -> None:
    def broken_uow() -> Any:
        raise error

    container = Container(
        api_key=API_KEY, uow_factory=broken_uow, policy=NotificationSchedulePolicy([3, 1, 0])
    )
    transport = httpx.ASGITransport(app=create_app(container), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/payments", json=payment_body(), headers=AUTH)

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "5"
    assert response.json()["status"] == 503


@pytest.mark.parametrize("day_count", [0, 3651, 99999999999])
async def test_day_count_out_of_range_is_422(client: httpx.AsyncClient, day_count: int) -> None:
    response = await client.put(
        "/api/v1/subscriptions/sub-1", json=subscription_body(day_count=day_count)
    )

    assert response.status_code == 422


class _DeadlockError(Exception):
    sqlstate = "40P01"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (DBAPIError("UPDATE ...", {}, _DeadlockError("deadlock detected")), 503),
        (PoolTimeoutError("QueuePool limit of size 10 overflow 10 reached"), 503),
        (DBAPIError("SELECT ...", {}, Exception("something else")), 500),
    ],
)
async def test_database_errors_are_classified(error: Exception, status: int) -> None:
    def broken_uow() -> Any:
        raise error

    container = Container(
        api_key=API_KEY, uow_factory=broken_uow, policy=NotificationSchedulePolicy([3, 1, 0])
    )
    transport = httpx.ASGITransport(app=create_app(container), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/payments", json=payment_body(), headers=AUTH)

    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert ("retry-after" in response.headers) is (status == 503)

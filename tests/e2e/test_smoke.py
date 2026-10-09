"""Happy paths through the whole stack: API → Postgres → relay → RabbitMQ → workers."""

from datetime import UTC, datetime, timedelta
from typing import Any

from tests.e2e.conftest import Api, Stack, eventually, parse_time, unique


def test_stack_is_ready(api: Api) -> None:
    response = api.http.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok"}


def test_api_key_is_required(api: Api) -> None:
    response = api.http.get("/api/v1/subscriptions/any", headers={"X-API-Key": "wrong"})

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


def test_successful_payment_notifies_user_and_renews_subscription(
    api: Api, stack: Stack, subscription: dict[str, Any]
) -> None:
    subscription_id = subscription["subscription_id"]
    request_id = unique("e2e-req")

    response = api.pay(subscription_id, headers={"X-Request-ID": request_id})

    assert response.status_code == 201, response.text
    assert response.headers["x-request-id"] == request_id
    payment_id = response.json()["payment"]["id"]

    view = api.delivered_payment(payment_id)
    assert view["event"]["event_type"] == "payment.succeeded"
    assert view["notification"]["event_name"] == "payment.succeeded"

    expected = parse_time(subscription["expected_expires_on"]) + timedelta(days=30)
    renewed = eventually(
        lambda: parse_time(api.subscription(subscription_id)["expected_expires_on"]) == expected,
        message="subscription renewed",
    )
    assert renewed

    # Reminders were moved to the new expiry date
    scheduled = [
        parse_time(n["scheduled_for"])
        for n in api.subscription(subscription_id)["notifications"]
        if n["status"] == "scheduled"
    ]
    assert max(scheduled) == expected

    # One request id across the API, the relay and the consumer
    assert request_id in stack.logs("outbox-relay")
    assert request_id in stack.logs("notification-worker")


def test_failed_payment_notifies_without_renewal(api: Api, subscription: dict[str, Any]) -> None:
    subscription_id = subscription["subscription_id"]

    response = api.pay(subscription_id, status="failed")

    assert response.status_code == 201, response.text
    view = api.delivered_payment(response.json()["payment"]["id"])
    assert view["notification"]["event_name"] == "payment.failed"
    current = api.subscription(subscription_id)
    assert current["expected_expires_on"] == subscription["expected_expires_on"]


def test_payment_retry_is_idempotent(api: Api, subscription: dict[str, Any]) -> None:
    subscription_id = subscription["subscription_id"]
    provider_payment = unique("pi")

    first = api.pay(subscription_id, provider_payment=provider_payment)
    replay = api.pay(subscription_id, provider_payment=provider_payment)
    conflict = api.pay(subscription_id, provider_payment=provider_payment, amount="1.00")

    assert (first.status_code, replay.status_code, conflict.status_code) == (201, 200, 409)
    assert replay.json()["payment"]["id"] == first.json()["payment"]["id"]
    # Still exactly one renewal for the two identical requests
    api.delivered_payment(first.json()["payment"]["id"])
    expected = parse_time(subscription["expected_expires_on"]) + timedelta(days=30)
    eventually(
        lambda: parse_time(api.subscription(subscription_id)["expected_expires_on"]) == expected,
        message="subscription renewed once",
    )


def test_expiry_reminder_is_sent_and_subscription_expires(api: Api) -> None:
    subscription_id = unique("sub")
    # Only the T0 reminder is still ahead; earlier offsets are already in the past
    expires = (datetime.now(UTC) + timedelta(seconds=3)).replace(microsecond=0)
    api.upsert_subscription(subscription_id, expires=expires)

    def expired() -> dict[str, Any] | None:
        view = api.subscription(subscription_id)
        return view if view["status"] == "expired" else None

    # Scheduler polls every 5 s by default
    view = eventually(expired, timeout=60, message="subscription expired")
    reminder = eventually(
        lambda: next(
            (
                n
                for n in api.subscription(subscription_id)["notifications"]
                if n["event_name"] == "subscription.expired" and n["status"] == "sent"
            ),
            None,
        ),
        message="expiry reminder sent",
    )
    assert parse_time(reminder["scheduled_for"]) == expires
    assert view["notifications"]


def test_metrics_are_exposed(api: Api, subscription: dict[str, Any]) -> None:
    api.delivered_payment(api.pay(subscription["subscription_id"]).json()["payment"]["id"])

    samples = api.metrics()

    assert samples['billing_outbox_events{status="published"}'] >= 1
    assert samples['billing_notifications{status="sent"}'] >= 1
    assert any(key.startswith("billing_http_requests_total{") for key in samples)

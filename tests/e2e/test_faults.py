"""Failure scenarios: components go down or crash, nothing is lost or applied twice.

These tests stop and kill containers of the running stack. Each one restores
what it broke, but run them against a local or CI stack, not a shared one.
"""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest

from tests.e2e.conftest import RECOVERY_TIMEOUT, Api, Stack, eventually, parse_time


@contextmanager
def stopped(stack: Stack, service: str) -> Iterator[None]:
    stack.stop(service)
    try:
        yield
    finally:
        stack.start(service)


def payment_ids(api: Api, subscription_id: str, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        response = api.pay(subscription_id)
        assert response.status_code == 201, response.text
        ids.append(response.json()["payment"]["id"])
    return ids


def wait_renewed(api: Api, subscription: dict[str, Any], times: int) -> None:
    expected = parse_time(subscription["expected_expires_on"]) + timedelta(days=30 * times)

    def current() -> bool:
        actual = parse_time(
            api.subscription(subscription["subscription_id"])["expected_expires_on"]
        )
        # Overshooting means a payment was applied twice: fail at once, do not wait
        assert actual <= expected, f"renewed more than {times} times: {actual} > {expected}"
        return actual == expected

    eventually(current, timeout=RECOVERY_TIMEOUT, message=f"renewed exactly {times} times")


def test_rabbitmq_outage_loses_no_events(
    api: Api, stack: Stack, subscription: dict[str, Any]
) -> None:
    with stopped(stack, "rabbitmq"):
        # The API does not need the broker: the event waits in the outbox
        [payment_id] = payment_ids(api, subscription["subscription_id"], 1)
        time.sleep(3)
        view = api.payment(payment_id)
        assert view["event"]["status"] == "pending"
        assert view["notification"] is None

    api.delivered_payment(payment_id, timeout=RECOVERY_TIMEOUT)
    wait_renewed(api, subscription, times=1)


def test_relay_outage_delays_but_does_not_lose_events(
    api: Api, stack: Stack, subscription: dict[str, Any]
) -> None:
    with stopped(stack, "outbox-relay"):
        ids = payment_ids(api, subscription["subscription_id"], 3)
        backlog = eventually(
            lambda: api.metrics()["billing_outbox_oldest_pending_age_seconds"] > 0,
            message="backlog visible in metrics",
        )
        assert backlog

    for payment_id in ids:
        api.delivered_payment(payment_id, timeout=RECOVERY_TIMEOUT)
    wait_renewed(api, subscription, times=3)


def test_consumer_outage_keeps_messages_in_the_queue(
    api: Api, stack: Stack, subscription: dict[str, Any]
) -> None:
    with stopped(stack, "notification-worker"):
        [payment_id] = payment_ids(api, subscription["subscription_id"], 1)
        # Published to RabbitMQ, but nobody has delivered the notification yet
        eventually(
            lambda: api.payment(payment_id)["event"]["status"] == "published",
            message="event published",
        )
        time.sleep(2)
        assert api.payment(payment_id)["notification"] is None

    api.delivered_payment(payment_id, timeout=RECOVERY_TIMEOUT)


def test_postgres_outage_is_reported_and_survived(
    api: Api, stack: Stack, subscription: dict[str, Any]
) -> None:
    with stopped(stack, "postgres"):
        ready = api.http.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["checks"] == {"database": "unavailable"}

        response = api.pay(subscription["subscription_id"])
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/problem+json"
        assert "retry-after" in response.headers

    eventually(
        lambda: api.http.get("/health/ready").status_code == 200,
        timeout=RECOVERY_TIMEOUT,
        message="API ready again",
    )
    # Every process reconnects by itself: no container was restarted
    [payment_id] = payment_ids(api, subscription["subscription_id"], 1)
    api.delivered_payment(payment_id, timeout=RECOVERY_TIMEOUT)


@pytest.mark.parametrize("victim", ["outbox-relay", "renewal-worker", "notification-worker"])
def test_crash_mid_stream_applies_every_payment_exactly_once(
    api: Api, stack: Stack, subscription: dict[str, Any], victim: str
) -> None:
    """SIGKILL while payments flow; redelivered events must not renew twice.

    The kill lands while events are being published and consumed, so some of them
    are in flight: published but not marked, or processed but not acknowledged.
    They are delivered again after the restart and must be deduplicated.
    """
    subscription_id = subscription["subscription_id"]
    count = 30

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(payment_ids, api, subscription_id, 1) for _ in range(count)]
        time.sleep(0.3)  # let the stream start
        stack.kill(victim)
        ids = [payment_id for future in futures for payment_id in future.result()]
    time.sleep(1)  # some events pile up while the victim is down
    stack.start(victim)

    for payment_id in ids:
        view = api.delivered_payment(payment_id, timeout=RECOVERY_TIMEOUT)
        assert view["notification"]["event_name"] == "payment.succeeded"
    wait_renewed(api, subscription, times=count)

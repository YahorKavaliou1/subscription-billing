"""Prometheus metrics.

Each process exposes its own metrics:

- API: ``GET /metrics`` on the HTTP port; also reports the outbox / notification
  backlog read from the database at scrape time (the database is shared, so one
  process reporting it is enough);
- workers: a small HTTP server on ``APP_METRICS_PORT`` (default 9100, 0 = disabled).

Label values are always taken from small fixed sets (statuses, outcomes, route
templates, queue names) to keep the number of time series bounded.
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
    start_http_server,
)

# No `*_created` timestamp series next to every counter: noise for this service
disable_created_metrics()  # type: ignore[no-untyped-call]

# --- HTTP API
HTTP_REQUESTS = Counter(
    "billing_http_requests_total",
    "HTTP requests by route template and status code.",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "billing_http_request_duration_seconds",
    "HTTP request duration.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

# --- Backlog in the database (refreshed by the API on every scrape)
OUTBOX_EVENTS = Gauge(
    "billing_outbox_events",
    "Outbox events by status.",
    ["status"],
)
OUTBOX_OLDEST_PENDING_AGE = Gauge(
    "billing_outbox_oldest_pending_age_seconds",
    "Age of the oldest pending outbox event; grows while the relay or the broker is down.",
)
NOTIFICATIONS = Gauge(
    "billing_notifications",
    "Notifications by status.",
    ["status"],
)

# --- Outbox relay
RELAY_EVENTS = Counter(
    "billing_relay_events_total",
    "Outbox events handled by the relay: published, failed (will be retried), dead.",
    ["result"],
)
RELAY_BROKER_UNAVAILABLE = Counter(
    "billing_relay_broker_unavailable_total",
    "Relay iterations interrupted because RabbitMQ was unreachable.",
)

# --- Reminder scheduler
SCHEDULER_REMINDERS_ENQUEUED = Counter(
    "billing_scheduler_reminders_enqueued_total",
    "Due reminders handed to the outbox.",
)
SCHEDULER_SUBSCRIPTIONS_EXPIRED = Counter(
    "billing_scheduler_subscriptions_expired_total",
    "Subscriptions marked expired by the scheduler.",
)

# --- Consumers
CONSUMER_MESSAGES = Counter(
    "billing_consumer_messages_total",
    "Consumed messages by queue and result: processed / duplicate / skipped / retry / parked.",
    ["queue", "result"],
)
CONSUMER_PROCESSING_DURATION = Histogram(
    "billing_consumer_processing_seconds",
    "Time spent handling one message.",
    ["queue"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


def render_latest() -> tuple[bytes, str]:
    """Current metrics in the Prometheus text format, with its content type."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def start_metrics_server(port: int) -> bool:
    """Serve /metrics for a worker process from a background thread; 0 disables it."""
    if port <= 0:
        return False
    start_http_server(port)
    return True

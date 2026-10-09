import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from app.infrastructure.observability.correlation import (
    bind_correlation_id,
    clear_correlation_id,
    get_correlation_id,
)
from app.infrastructure.observability.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


def read_json_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_structlog_record_is_json_with_correlation_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)
    bind_correlation_id("abc123")

    get_logger("test").info("payment.registered", payment_id="p-1")

    [record] = read_json_lines(capsys.readouterr().out)
    assert record["event"] == "payment.registered"
    assert record["payment_id"] == "p-1"
    assert record["correlation_id"] == "abc123"
    assert record["level"] == "info"
    assert record["logger"] == "test"
    assert "timestamp" in record


def test_stdlib_logger_goes_through_the_same_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", json=True)
    bind_correlation_id("xyz")

    logging.getLogger("third.party").warning("hello from stdlib")

    [record] = read_json_lines(capsys.readouterr().out)
    assert record["event"] == "hello from stdlib"
    assert record["correlation_id"] == "xyz"


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", json=True)

    log = get_logger("test")
    log.info("dropped")
    log.warning("kept")

    records = read_json_lines(capsys.readouterr().out)
    assert [r["event"] for r in records] == ["kept"]


def test_correlation_id_helpers() -> None:
    generated = bind_correlation_id()
    assert get_correlation_id() == generated
    assert len(generated) == 32

    clear_correlation_id()
    assert get_correlation_id() is None


def test_faststream_logs_are_json_with_message_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # FastStream attaches its own colored handler and disables propagation
    library_logger = logging.getLogger("faststream.access.rabbit")
    library_logger.addHandler(logging.StreamHandler())
    library_logger.propagate = False

    configure_logging("INFO", json=True)
    library_logger.info(
        "Received",
        extra={"queue": "notifications.send", "message_id": "m-1", "color_message": "x"},
    )

    assert library_logger.handlers == []
    [record] = read_json_lines(capsys.readouterr().out)
    assert record["event"] == "Received"
    assert record["queue"] == "notifications.send"
    assert record["message_id"] == "m-1"
    assert "color_message" not in record  # only known extras are kept


def test_faststream_debug_lines_follow_the_root_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # FastStream pins INFO on its parent logger at import time
    logging.getLogger("faststream").setLevel(logging.INFO)
    library_logger = logging.getLogger("faststream.access.rabbit")

    configure_logging("INFO", json=True)
    library_logger.debug("Received")  # per-message lines are hidden by default
    assert read_json_lines(capsys.readouterr().out) == []

    configure_logging("DEBUG", json=True)
    library_logger.debug("Received")
    [record] = read_json_lines(capsys.readouterr().out)
    assert record["event"] == "Received"

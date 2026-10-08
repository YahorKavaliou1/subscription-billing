"""Logging setup: structlog for application logs, stdlib loggers routed through it.

Third-party libraries (uvicorn, sqlalchemy, faststream, aio-pika) log via the
standard ``logging`` module; their records go through the same processors, so
the whole process emits one format: JSON in containers, colored console locally.
"""

import logging
import sys

import structlog
from structlog.typing import Processor

# Noisy third-party loggers and the minimum level kept for them
_THIRD_PARTY_LEVELS = {
    "uvicorn.access": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "aio_pika": logging.WARNING,
    "aiormq": logging.WARNING,
}


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once."""
    shared = _shared_processors()

    renderer: Processor
    if json:
        renderer = structlog.processors.JSONRenderer()
        shared = [*shared, structlog.processors.format_exc_info]
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name, min_level in _THIRD_PARTY_LEVELS.items():
        logging.getLogger(name).setLevel(max(min_level, root.level))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger

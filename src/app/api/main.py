"""FastAPI application factory and process entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI

from app.api.deps import require_api_key
from app.api.errors import register_error_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routers import health, metrics, payments, subscriptions
from app.bootstrap import Container, build_container
from app.config import get_settings
from app.infrastructure.observability.logging import configure_logging, get_logger

log = get_logger(__name__)


def create_app(container: Container | None = None) -> FastAPI:
    """Build the app. Without a container, it is created from settings on startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = app.state.container is None
        if owned:
            settings = get_settings()
            configure_logging(settings.log_level, json=settings.log_json)
            app.state.container = build_container(settings)
        log.info("api.started")
        try:
            yield
        finally:
            if owned:
                await app.state.container.shutdown()
            log.info("api.stopped")

    app = FastAPI(
        title="Subscription billing",
        version="0.1.0",
        description="Subscriptions, payments with a transactional outbox, notifications.",
        lifespan=lifespan,
    )
    app.state.container = container

    api_v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
    api_v1.include_router(subscriptions.router)
    api_v1.include_router(payments.router)
    app.include_router(api_v1)
    app.include_router(health.router)
    app.include_router(metrics.router)

    register_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    return app


def run() -> None:
    """Entry point: `python -m app.api.main` (compose) or `billing-api`."""
    import uvicorn

    settings = get_settings()
    # Before uvicorn starts, so its own startup messages are JSON too
    configure_logging(settings.log_level, json=settings.log_json)
    uvicorn.run(
        "app.api.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104  # inside a container
        port=8000,
        log_config=None,  # keep the logging configured above
        access_log=False,  # RequestContextMiddleware writes the access log
    )


if __name__ == "__main__":
    run()

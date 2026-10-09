"""Prometheus endpoint. Not part of the public API: no API key, hidden from OpenAPI."""

import asyncio

from fastapi import APIRouter, Response

from app.api.deps import ContainerDep
from app.infrastructure.observability.logging import get_logger
from app.infrastructure.observability.metrics import render_latest

router = APIRouter(tags=["observability"])
log = get_logger(__name__)

REFRESH_TIMEOUT_SECONDS = 2.0


@router.get("/metrics", include_in_schema=False)
async def metrics(container: ContainerDep) -> Response:
    for refresh in container.metrics_refreshers:
        try:
            await asyncio.wait_for(refresh(), timeout=REFRESH_TIMEOUT_SECONDS)
        except Exception as exc:  # a scrape must not fail: serve the last known values
            log.warning("metrics.refresh_failed", error=repr(exc))
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)

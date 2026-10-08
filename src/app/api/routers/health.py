import asyncio

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.api.deps import ContainerDep
from app.infrastructure.observability.logging import get_logger

router = APIRouter(prefix="/health", tags=["health"])
log = get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/live", summary="The process is up")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", summary="Dependencies are reachable; safe to route traffic here")
async def ready(container: ContainerDep) -> JSONResponse:
    checks: dict[str, str] = {}
    for name, check in container.readiness_checks.items():
        try:
            await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_SECONDS)
            checks[name] = "ok"
        except Exception as exc:  # any failure means "not ready"
            log.warning("health.check_failed", check=name, error=repr(exc))
            checks[name] = "unavailable"
    healthy = all(result == "ok" for result in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "unavailable", "checks": checks},
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )

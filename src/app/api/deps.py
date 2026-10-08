import secrets
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from app.api.errors import UnauthorizedError
from app.bootstrap import Container

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def require_api_key(
    container: ContainerDep,
    api_key: Annotated[str | None, Security(_api_key_header)],
) -> None:
    # Constant-time comparison: response timing does not leak the key
    if api_key is None or not secrets.compare_digest(api_key, container.api_key):
        raise UnauthorizedError

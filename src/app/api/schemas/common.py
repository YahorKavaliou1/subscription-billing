from typing import Any

from pydantic import BaseModel, ConfigDict


class RequestModel(BaseModel):
    # Unknown fields are a client bug: reject instead of silently ignoring
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Problem(BaseModel):
    """Error body, RFC 9457 (application/problem+json)."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[dict[str, Any]] | None = None

"""Login state snapshot — env/framework agnostic."""

from typing import Any

from pydantic import BaseModel, Field


class LoginSnapshot(BaseModel):
    """Serialized login state — just cookies + localStorage, env/framework agnostic."""

    profile_name: str
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    local_storage: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    timestamp: float = 0.0

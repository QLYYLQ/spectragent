"""Provider session — connection info returned by any provider."""

from typing import Any

from pydantic import BaseModel, Field


class ProviderSession(BaseModel):
    """Connection info returned by any provider."""

    session_id: str
    provider_id: str
    cdp_url: str | None = None
    vnc_address: str | None = None
    adb_device_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

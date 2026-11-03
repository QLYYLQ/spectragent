"""The browser task's setup: what a browser task asks of its environment (§4.10).

Subclassing ``SetupBase`` registers it in ``TaskSetupRegistrar`` under
``browser``, and this package's ``declarations.py`` names it ``TASK_SETUP``.
The browser's ``declarations.py`` imports this module, so it imports nothing
of the browser implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from spectragent.schemas.browser_storage import BrowserStorageState
from spectragent.schemas.task import SetupBase


def _prune(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` and empty-container entries from a context dict."""
    return {
        k: v
        for k, v in data.items()
        if v is not None and not (isinstance(v, (list, dict, str)) and len(v) == 0)
    }


class BrowserSetup(SetupBase):
    """Browser session requirements for a task."""

    __registry_key__ = "browser"
    __env_types__: ClassVar[tuple[str, ...]] = ("browser",)
    __raw_keys__: ClassVar[tuple[str, ...]] = ("start_url", "storage_state")

    start_url: str | None = None
    login_required: bool = False
    login_type: str | None = None  # "account_password", "sso", "oauth"
    storage_state: BrowserStorageState | str | None = None
    risk_control: bool = False
    risk_control_types: list[str] = Field(default_factory=list)

    @field_validator("storage_state", mode="before")
    @classmethod
    def storage_source(cls, value: Any) -> Any:
        return str(value) if isinstance(value, Path) else value

    @model_validator(mode="before")
    @classmethod
    def reject_credentials_ref(cls, value: Any) -> Any:
        if isinstance(value, dict) and "credentials_ref" in value:
            raise ValueError(
                "credentials_ref is unsupported; include login instructions and credentials in task_text"
            )
        return value

    @classmethod
    def start_point(cls, context: Mapping[str, Any]) -> str | None:
        """The start URL's site: its host, lowercased; ``None`` without a start URL with a host."""
        start_url = context.get("start_url")
        host = urlsplit(start_url).hostname if isinstance(start_url, str) else None
        return host.lower() if host else None

    def to_context(self) -> dict[str, Any]:
        return _prune(
            {
                "start_url": self.start_url,
                "storage_state": self.storage_state,
                "risk_control": self.risk_control or None,
                "risk_control_types": self.risk_control_types,
            }
        )

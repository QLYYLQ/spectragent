"""Portable browser session evidence, limited to cookies and Web Storage.

Storage keys are Chrome's opaque partition identities. Ancestor origins describe
how to recreate that partition, without parsing or manufacturing a storage key.
This is not a disk-profile backup: IndexedDB, service workers and caches are outside
the declared coverage.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StorageArea(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_key: str = Field(min_length=1)
    origin: str = Field(pattern=r"^https?://")
    # Nearest parent first, as location.ancestorOrigins reports them.
    ancestor_origins: list[str] = Field(default_factory=list)
    items: dict[str, str] = Field(default_factory=dict)


class SessionStorageArea(StorageArea):
    tab: str = Field(min_length=1)


class StorageTab(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tab: str = Field(min_length=1)
    url: str = "about:blank"


class StorageFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    frame: str | None = None
    operation: str
    error: str


class BrowserStorageState(BaseModel):
    """A snapshot with explicit coverage and incomplete-read evidence.

    Callers may edit a model while constructing it. Execution freezes its canonical
    bytes once at setup; no mutable caller object or changing source file is retained.
    """

    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1] = 1
    coverage: Literal["cookies_local_session"] = "cookies_local_session"
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    local: list[StorageArea] = Field(default_factory=list)
    session: list[SessionStorageArea] = Field(default_factory=list)
    tabs: list[StorageTab] = Field(default_factory=list)
    unavailable: list[StorageFailure] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_areas(self) -> "BrowserStorageState":
        tab_ids = [tab.tab for tab in self.tabs]
        if len(set(tab_ids)) != len(tab_ids):
            raise ValueError("storage state has duplicate tab identities")
        if any(area.tab not in tab_ids for area in self.session):
            raise ValueError("session storage refers to a tab absent from tabs")
        local = [area.storage_key for area in self.local]
        session = [(area.tab, area.storage_key) for area in self.session]
        if len(set(local)) != len(local) or len(set(session)) != len(session):
            raise ValueError("storage state has duplicate storage areas")
        for cookie in self.cookies:
            if not all(isinstance(cookie.get(k), str) for k in ("name", "value", "domain", "path")):
                raise ValueError("cookies require string name, value, domain and path")
        return self

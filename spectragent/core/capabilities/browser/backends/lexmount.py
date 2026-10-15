"""Lexmount browser provider.

SDK: ``lexmount`` (sync — calls wrapped via ``run_in_executor``).
Env vars: ``LEXMOUNT_API_KEY``, ``LEXMOUNT_PROJECT_ID``, ``LEXMOUNT_BASE_URL``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.capabilities.browser.backends._registrar import BrowserProviderBase
from spectragent.core.capabilities.browser.backends.capabilities import (
    ANTI_DETECT,
    CDP,
    CONTEXT_PERSISTENCE,
    FINGERPRINT,
    PROXY,
)
from spectragent.schemas.provider_session import ProviderSession


@dataclass
class LexmountProvider(BrowserProviderBase):
    """Lexmount cloud browser — fingerprint, proxy, context persistence."""

    __registry_key__ = "lexmount"
    __capabilities__: ClassVar[set[str]] = {
        CDP,
        PROXY,
        FINGERPRINT,
        ANTI_DETECT,
        CONTEXT_PERSISTENCE,
    }

    # ── connection config ─────────────────────────────────────────────
    api_key: Annotated[
        str | None,
        Field(description="Lexmount API key. None (the default): read from LEXMOUNT_API_KEY."),
    ] = None
    project_id: Annotated[
        str | None,
        Field(
            description="Lexmount project ID. None (the default): read from LEXMOUNT_PROJECT_ID."
        ),
    ] = None
    base_url: Annotated[
        str | None,
        Field(description="API base URL override. None (the default): the service's own endpoint."),
    ] = None

    # ── capability config (visible at init, overrideable in open()) ───
    browser_mode: Annotated[str, Field(description="Browser mode: 'normal' or 'light'")] = "normal"
    context_id: Annotated[
        str | None,
        Field(
            description="Context ID for persistence across sessions. None (the default): no context is attached."
        ),
    ] = None
    context_mode: Annotated[
        str, Field(description="Context access mode: 'read_write' or 'read_only'")
    ] = "read_write"

    # ── internal state (excluded from config) ─────────────────────────
    _client: Any = field(default=None, init=False, repr=False)
    _sessions: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _init_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def _ensure_client(self) -> Any:
        """Thread-safe lazy client initialization."""
        async with self._init_lock:
            if self._client is None:
                from lexmount import Lexmount

                init_kwargs: dict[str, Any] = {}
                if self.api_key is not None:
                    init_kwargs["api_key"] = self.api_key
                if self.project_id is not None:
                    init_kwargs["project_id"] = self.project_id
                if self.base_url is not None:
                    init_kwargs["base_url"] = self.base_url
                self._client = Lexmount(**init_kwargs)
        return self._client

    async def open(
        self,
        *,
        browser_mode: str | None = None,
        context_id: str | None = None,
        context_mode: str | None = None,
    ) -> ProviderSession:
        client = await self._ensure_client()

        mode = browser_mode if browser_mode is not None else self.browser_mode
        ctx_id = context_id if context_id is not None else self.context_id
        ctx_mode = context_mode if context_mode is not None else self.context_mode

        create_kwargs: dict[str, Any] = {"browser_mode": mode}
        if ctx_id is not None:
            create_kwargs["context"] = {"id": ctx_id, "mode": ctx_mode}

        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(None, partial(client.sessions.create, **create_kwargs))

        try:
            self._sessions[session.id] = session
            return ProviderSession(
                session_id=session.id,
                provider_id=self.provider_id,
                cdp_url=session.connect_url,
                metadata={
                    "inspect_url": getattr(session, "inspect_url", None),
                    "browser_mode": mode,
                },
            )
        except Exception:
            await loop.run_in_executor(None, session.close)
            raise

    async def close(self, session: ProviderSession | None = None) -> None:
        """Release owned sessions, retaining failed cleanup for a later retry."""
        if session is not None and session.provider_id != self.provider_id:
            return
        session_ids = [session.session_id] if session is not None else list(self._sessions)
        failures: list[Exception] = []
        for session_id in session_ids:
            owned_session = self._sessions.get(session_id)
            if owned_session is None:
                continue
            try:
                await asyncio.get_running_loop().run_in_executor(None, owned_session.close)
            except asyncio.CancelledError as cancellation:
                for failure in failures:
                    cancellation.add_note(f"Session cleanup also failed: {failure}")
                raise
            except Exception as exc:
                failures.append(exc)
            else:
                self._sessions.pop(session_id, None)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("Browser session cleanup failed", failures)

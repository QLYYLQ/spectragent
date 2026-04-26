"""Skyvern cloud browser provider.

SDK: ``skyvern_lite`` (async, polls for CDP URL up to 30 s).
Env var: ``SKYVERN_API_KEY``.

**CDP auth**: Skyvern requires an ``x-api-key`` header on the CDP WebSocket.
The header is stored in ``ProviderSession.metadata["cdp_headers"]`` for
downstream ``CDPClient(url, additional_headers=...)`` usage.

WARNING: ``metadata["cdp_headers"]`` contains authentication tokens.
Do NOT log or serialize ProviderSession.metadata without redaction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.capabilities.browser.backends._registrar import BrowserProviderBase
from spectragent.core.capabilities.browser.backends.capabilities import (
    ADBLOCK,
    CAPTCHA_SOLVE,
    CDP,
    PROXY,
)
from spectragent.schemas.provider_session import ProviderSession


@dataclass
class SkyvernProvider(BrowserProviderBase):
    """Skyvern cloud browser — proxy, ad blocker, captcha solver."""

    __registry_key__ = "skyvern"
    __capabilities__: ClassVar[set[str]] = {CDP, PROXY, ADBLOCK, CAPTCHA_SOLVE}

    # ── connection config ─────────────────────────────────────────────
    api_key: Annotated[str | None, Field(description="Skyvern API key (env: SKYVERN_API_KEY)")] = None
    base_url: Annotated[str | None, Field(description="API base URL override")] = None

    # ── capability config (visible at init, overrideable in open()) ───
    timeout_minutes: Annotated[int, Field(description="Session timeout in minutes (5-1440)")] = 60
    solve_captcha: Annotated[bool, Field(description="Enable automatic CAPTCHA solving")] = False
    adblock: Annotated[bool, Field(description="Enable ad / tracker blocking")] = False
    proxy_country: Annotated[str | None, Field(description="Managed proxy country code (e.g. 'US')")] = None

    # ── internal state ────────────────────────────────────────────────
    _client: Any = field(default=None, init=False, repr=False)
    _sessions: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            from skyvern_lite import AsyncSkyvernCloud

            kwargs: dict[str, Any] = {}
            if self.api_key is not None:
                kwargs["api_key"] = self.api_key
            if self.base_url is not None:
                kwargs["base_url"] = self.base_url
            self._client = AsyncSkyvernCloud(**kwargs)
        return self._client

    async def open(
        self,
        *,
        timeout_minutes: int | None = None,
        solve_captcha: bool | None = None,
        adblock: bool | None = None,
        proxy_country: str | None = None,
    ) -> ProviderSession:
        client = self._get_client()

        timeout = timeout_minutes if timeout_minutes is not None else self.timeout_minutes
        captcha = solve_captcha if solve_captcha is not None else self.solve_captcha
        ad = adblock if adblock is not None else self.adblock
        proxy_cc = proxy_country if proxy_country is not None else self.proxy_country

        from skyvern_lite import ManagedProxyConfig

        create_kwargs: dict[str, Any] = {"timeout": timeout}

        # Skyvern uses extensions list for captcha/adblock
        extensions: list[str] = []
        if captcha:
            extensions.append("captcha-solver")
        if ad:
            extensions.append("ad-blocker")
        if extensions:
            create_kwargs["extensions"] = extensions

        if proxy_cc is not None:
            create_kwargs["proxy"] = ManagedProxyConfig(country=proxy_cc)

        session = await client.sessions.create(**create_kwargs)

        try:
            self._sessions[session.session_id] = session.session_id

            # Skyvern requires x-api-key header on the CDP WebSocket connection.
            cdp_headers: dict[str, str] = {}
            resolved_key = self.api_key or os.environ.get("SKYVERN_API_KEY", "")
            if resolved_key:
                cdp_headers["x-api-key"] = resolved_key

            return ProviderSession(
                session_id=session.session_id,
                provider_id=self.provider_id,
                cdp_url=session.cdp_url,
                metadata={
                    "cdp_headers": cdp_headers,
                    "inspect_url": getattr(session, "inspect_url", None),
                },
            )
        except Exception:
            await client.sessions.delete(session.session_id)
            raise

    async def close(self, session: ProviderSession | None = None) -> None:
        client = self._get_client()
        if session is not None:
            sid = self._sessions.pop(session.session_id, None)
            if sid is not None:
                await client.sessions.delete(sid)
            return
        for sid in list(self._sessions.values()):
            await client.sessions.delete(sid)
        self._sessions.clear()

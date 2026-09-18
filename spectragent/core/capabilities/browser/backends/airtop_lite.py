"""Airtop cloud browser provider.

SDK: ``airtop_lite`` (async, polls for CDP URL up to 30 s).
Env var: ``AIRTOP_API_KEY``.

**CDP auth**: Airtop requires a Bearer token header on the CDP WebSocket.
The header is stored in ``ProviderSession.metadata["cdp_headers"]`` for
downstream ``CDPClient(url, additional_headers=...)`` usage.

WARNING: ``metadata["cdp_headers"]`` contains authentication tokens.
Do NOT log or serialize ProviderSession.metadata without redaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.capabilities.browser.backends._registrar import BrowserProviderBase
from spectragent.core.capabilities.browser.backends.capabilities import (
    CAPTCHA_SOLVE,
    CDP,
    EXTENSIONS,
    PROFILE,
    PROXY,
)
from spectragent.schemas.provider_session import ProviderSession


@dataclass
class AirtopProvider(BrowserProviderBase):
    """Airtop cloud browser — profiles, captcha solving, extensions."""

    __registry_key__ = "airtop"
    __capabilities__: ClassVar[set[str]] = {
        CDP,
        PROXY,
        PROFILE,
        CAPTCHA_SOLVE,
        EXTENSIONS,
    }

    # ── connection config ─────────────────────────────────────────────
    api_key: Annotated[str | None, Field(description="Airtop API key (env: AIRTOP_API_KEY)")] = None
    base_url: Annotated[str | None, Field(description="API base URL override")] = None

    # ── capability config (visible at init, overrideable in open()) ───
    profile_name: Annotated[str | None, Field(description="Browser profile name to restore")] = None
    solve_captcha: Annotated[bool, Field(description="Enable automatic CAPTCHA solving")] = False
    proxy_country: Annotated[
        str | None, Field(description="Managed proxy country code (e.g. 'US')")
    ] = None

    # ── internal state ────────────────────────────────────────────────
    _client: Any = field(default=None, init=False, repr=False)
    _sessions: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            from airtop_lite import AsyncAirtopCloud

            kwargs: dict[str, Any] = {}
            if self.api_key is not None:
                kwargs["api_key"] = self.api_key
            if self.base_url is not None:
                kwargs["base_url"] = self.base_url
            self._client = AsyncAirtopCloud(**kwargs)
        return self._client

    async def open(
        self,
        *,
        profile_name: str | None = None,
        solve_captcha: bool | None = None,
        proxy_country: str | None = None,
    ) -> ProviderSession:
        client = self._get_client()

        p_name = profile_name if profile_name is not None else self.profile_name
        captcha = solve_captcha if solve_captcha is not None else self.solve_captcha
        proxy_cc = proxy_country if proxy_country is not None else self.proxy_country

        from airtop_lite import ManagedProxyConfig

        create_kwargs: dict[str, Any] = {}

        if p_name is not None:
            create_kwargs["profile_name"] = p_name
        if captcha:
            create_kwargs["solve_captcha"] = True
        if proxy_cc is not None:
            create_kwargs["proxy"] = ManagedProxyConfig(country=proxy_cc)

        session = await client.sessions.create(**create_kwargs)

        try:
            self._sessions[session.session_id] = session.session_id

            # Airtop requires Bearer auth on the CDP WebSocket connection.
            cdp_headers: dict[str, str] = {}
            if client.api_key:
                cdp_headers["Authorization"] = f"Bearer {client.api_key}"

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

"""Hyperbrowser cloud browser provider.

SDK: ``hyperbrowser_lite`` (async, polls for CDP URL up to 30 s).
Env var: ``HYPERBROWSER_API_KEY``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.providers.browser._registrar import BrowserProviderBase
from spectragent.core.providers.browser.capabilities import (
    ADBLOCK,
    CAPTCHA_SOLVE,
    CDP,
    CUSTOM_PROXY,
    EXTENSIONS,
    FINGERPRINT,
    PROFILE,
    PROXY,
    RECORDING,
    STEALTH,
)
from spectragent.core.providers.session import ProviderSession


@dataclass
class HyperbrowserProvider(BrowserProviderBase):
    """Hyperbrowser cloud browser — stealth, fingerprint, recording, captcha, extensions."""

    __registry_key__ = "hyperbrowser"
    __capabilities__: ClassVar[set[str]] = {
        CDP,
        PROXY,
        CUSTOM_PROXY,
        FINGERPRINT,
        STEALTH,
        RECORDING,
        CAPTCHA_SOLVE,
        ADBLOCK,
        EXTENSIONS,
        PROFILE,
    }

    # ── connection config ─────────────────────────────────────────────
    api_key: Annotated[str | None, Field(description="Hyperbrowser API key (env: HYPERBROWSER_API_KEY)")] = None
    base_url: Annotated[str | None, Field(description="API base URL override")] = None

    # ── capability config (visible at init, overrideable in open()) ───
    stealth: Annotated[bool, Field(description="Enable stealth browsing mode")] = True
    solve_captchas: Annotated[bool, Field(description="Enable automatic CAPTCHA solving")] = False
    adblock: Annotated[bool, Field(description="Enable ad / tracker blocking")] = False
    enable_recording: Annotated[bool, Field(description="Enable web session recording")] = False
    proxy_country: Annotated[str | None, Field(description="Managed proxy country code (e.g. 'US')")] = None
    proxy_server: Annotated[str | None, Field(description="Custom proxy server URL (e.g. 'http://proxy:8080')")] = None

    # ── internal state ────────────────────────────────────────────────
    _client: Any = field(default=None, init=False, repr=False)
    _sessions: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            from hyperbrowser_lite import AsyncHyperbrowserCloud

            kwargs: dict[str, Any] = {}
            if self.api_key is not None:
                kwargs["api_key"] = self.api_key
            if self.base_url is not None:
                kwargs["base_url"] = self.base_url
            self._client = AsyncHyperbrowserCloud(**kwargs)
        return self._client

    async def open(
        self,
        *,
        stealth: bool | None = None,
        solve_captchas: bool | None = None,
        adblock: bool | None = None,
        enable_recording: bool | None = None,
        proxy_country: str | None = None,
        proxy_server: str | None = None,
    ) -> ProviderSession:
        client = self._get_client()

        use_stealth = stealth if stealth is not None else self.stealth
        captcha = solve_captchas if solve_captchas is not None else self.solve_captchas
        ad = adblock if adblock is not None else self.adblock
        rec = enable_recording if enable_recording is not None else self.enable_recording
        proxy_cc = proxy_country if proxy_country is not None else self.proxy_country
        p_server = proxy_server if proxy_server is not None else self.proxy_server

        from hyperbrowser_lite import ManagedProxyConfig, ProxyConfig, RecordingConfig

        browser_mode = "stealth" if use_stealth else "normal"
        create_kwargs: dict[str, Any] = {"browser_mode": browser_mode}

        if rec:
            create_kwargs["recording"] = RecordingConfig(enabled=True)

        # Custom proxy takes priority over managed proxy
        if p_server is not None:
            create_kwargs["proxy"] = ProxyConfig(server=p_server)
        elif proxy_cc is not None:
            create_kwargs["proxy"] = ManagedProxyConfig(country=proxy_cc)

        if captcha:
            create_kwargs["solve_captchas"] = True
        if ad:
            create_kwargs["adblock"] = True

        session = await client.sessions.create(**create_kwargs)

        try:
            self._sessions[session.session_id] = session.session_id
            return ProviderSession(
                session_id=session.session_id,
                provider_id=self.provider_id,
                cdp_url=session.cdp_url,
                metadata={
                    "inspect_url": getattr(session, "inspect_url", None),
                    "stealth": use_stealth,
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

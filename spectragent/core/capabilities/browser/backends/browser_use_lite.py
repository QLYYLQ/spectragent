"""Browser-Use cloud browser provider.

SDK: ``browser_use_lite`` (async).
Env var: ``BROWSER_USE_API_KEY``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.capabilities.browser.backends._registrar import BrowserProviderBase
from spectragent.core.capabilities.browser.backends.capabilities import (
    CDP,
    CUSTOM_PROXY,
    PROXY,
    SCREEN_SIZE,
)
from spectragent.schemas.provider_session import ProviderSession


@dataclass
class BrowserUseProvider(BrowserProviderBase):
    """Browser-Use cloud browser — proxy, custom proxy, screen size."""

    __registry_key__ = "browser_use"
    __capabilities__: ClassVar[set[str]] = {CDP, PROXY, CUSTOM_PROXY, SCREEN_SIZE}

    # ── connection config ─────────────────────────────────────────────
    api_key: Annotated[
        str | None,
        Field(
            description="Browser-Use API key. None (the default): read from BROWSER_USE_API_KEY."
        ),
    ] = None
    base_url: Annotated[
        str | None,
        Field(description="API base URL override. None (the default): the service's own endpoint."),
    ] = None

    # ── capability config (visible at init, overrideable in open()) ───
    proxy_country: Annotated[
        str | None,
        Field(
            description="Managed proxy country code (e.g. 'US'). None (the default): no managed proxy is requested."
        ),
    ] = None
    proxy_server: Annotated[
        str | None,
        Field(
            description="Custom proxy server URL (e.g. 'http://proxy:8080'). None (the default): no custom proxy."
        ),
    ] = None
    screen_width: Annotated[
        int | None,
        Field(
            description="Browser screen width in pixels. None (the default): the service's default."
        ),
    ] = None
    screen_height: Annotated[
        int | None,
        Field(
            description="Browser screen height in pixels. None (the default): the service's default."
        ),
    ] = None

    # ── internal state ────────────────────────────────────────────────
    _client: Any = field(default=None, init=False, repr=False)
    _sessions: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            from browser_use_lite import AsyncBrowserUseCloud

            kwargs: dict[str, Any] = {}
            if self.api_key is not None:
                kwargs["api_key"] = self.api_key
            if self.base_url is not None:
                kwargs["base_url"] = self.base_url
            self._client = AsyncBrowserUseCloud(**kwargs)
        return self._client

    async def open(
        self,
        *,
        proxy_country: str | None = None,
        proxy_server: str | None = None,
        screen_width: int | None = None,
        screen_height: int | None = None,
    ) -> ProviderSession:
        client = self._get_client()

        p_country = proxy_country if proxy_country is not None else self.proxy_country
        p_server = proxy_server if proxy_server is not None else self.proxy_server
        s_width = screen_width if screen_width is not None else self.screen_width
        s_height = screen_height if screen_height is not None else self.screen_height

        from browser_use_lite import ManagedProxyConfig, ProxyConfig

        create_kwargs: dict[str, Any] = {}

        # Custom proxy takes priority over managed proxy
        if p_server is not None:
            create_kwargs["proxy"] = ProxyConfig(server=p_server)
        elif p_country is not None:
            create_kwargs["proxy"] = ManagedProxyConfig(country=p_country)
        if s_width is not None:
            create_kwargs["browser_screen_width"] = s_width
        if s_height is not None:
            create_kwargs["browser_screen_height"] = s_height

        session = await client.sessions.create(**create_kwargs)

        try:
            self._sessions[session.session_id] = session.session_id
            return ProviderSession(
                session_id=session.session_id,
                provider_id=self.provider_id,
                cdp_url=session.cdp_url,
                metadata={
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

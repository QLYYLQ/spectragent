"""AgentBay (Alibaba Cloud) browser provider.

SDK: ``agentbay_lite`` (async, RPC-based — CreateMcpSession → InitBrowser → GetCdpLink).
Env vars: ``AGENTBAY_API_KEY``, ``AGENTBAY_ENDPOINT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.providers.browser._registrar import BrowserProviderBase
from spectragent.core.providers.browser.capabilities import (
    CDP,
    CONTEXT_PERSISTENCE,
    FINGERPRINT,
    PROXY,
    RECORDING,
    STEALTH,
)
from spectragent.core.providers.session import ProviderSession


@dataclass
class AgentBayProvider(BrowserProviderBase):
    """AgentBay (Alibaba Wuying) cloud browser — context persistence, fingerprint, recording."""

    __registry_key__ = "agentbay"
    __capabilities__: ClassVar[set[str]] = {
        CDP,
        PROXY,
        FINGERPRINT,
        STEALTH,
        RECORDING,
        CONTEXT_PERSISTENCE,
    }

    # ── connection config ─────────────────────────────────────────────
    api_key: Annotated[str | None, Field(description="AgentBay API key (env: AGENTBAY_API_KEY)")] = None
    endpoint: Annotated[str | None, Field(description="AgentBay RPC endpoint (env: AGENTBAY_ENDPOINT)")] = None

    # ── capability config (visible at init, overrideable in open()) ───
    stealth: Annotated[bool, Field(description="Enable stealth browsing mode")] = False
    enable_recording: Annotated[bool, Field(description="Enable browser session recording")] = True
    proxy_country: Annotated[str | None, Field(description="Managed proxy country code (e.g. 'US')")] = None
    context_id: Annotated[str | None, Field(description="Context ID for persistence across sessions")] = None

    # ── internal state ────────────────────────────────────────────────
    _client: Any = field(default=None, init=False, repr=False)
    _sessions: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            from agentbay_lite import AsyncAgentBayCloud

            kwargs: dict[str, Any] = {}
            if self.api_key is not None:
                kwargs["api_key"] = self.api_key
            if self.endpoint is not None:
                kwargs["endpoint"] = self.endpoint
            self._client = AsyncAgentBayCloud(**kwargs)
        return self._client

    async def open(
        self,
        *,
        stealth: bool | None = None,
        enable_recording: bool | None = None,
        proxy_country: str | None = None,
        context_id: str | None = None,
    ) -> ProviderSession:
        client = self._get_client()

        use_stealth = stealth if stealth is not None else self.stealth
        recording = enable_recording if enable_recording is not None else self.enable_recording
        proxy_cc = proxy_country if proxy_country is not None else self.proxy_country
        ctx_id = context_id if context_id is not None else self.context_id

        # Build typed SDK params
        from agentbay_lite import ContextAttach, ManagedProxyConfig, RecordingConfig

        browser_mode = "stealth" if use_stealth else "normal"
        create_kwargs: dict[str, Any] = {"browser_mode": browser_mode}

        if not recording:
            create_kwargs["recording"] = RecordingConfig(enabled=False)

        if proxy_cc is not None:
            create_kwargs["proxy"] = ManagedProxyConfig(country=proxy_cc)

        if ctx_id is not None:
            create_kwargs["context"] = ContextAttach(context_id=ctx_id)

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

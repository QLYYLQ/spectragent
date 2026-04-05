"""Mobile provider protocol (reserved for L2+)."""

from typing import Any, Protocol, runtime_checkable

from spectragent.core.providers.session import ProviderSession


@runtime_checkable
class BaseMobileProvider(Protocol):
    """Mobile infrastructure provider."""

    @property
    def provider_id(self) -> str: ...

    async def open(self, config: dict[str, Any] | None = None) -> ProviderSession: ...

    async def close(self, session: ProviderSession | None = None) -> None: ...

"""Browser provider protocol."""

from typing import Any, Protocol, runtime_checkable

from spectragent.schemas.provider_session import ProviderSession


@runtime_checkable
class BaseBrowserProvider(Protocol):
    """Browser infrastructure provider.

    Each provider exposes capability-specific config at ``__init__`` time (so
    users can discover configurable options).  ``open()`` accepts the same
    fields as explicit keyword arguments for per-session overrides.
    """

    @property
    def provider_id(self) -> str: ...

    async def open(self, **kwargs: Any) -> ProviderSession: ...

    async def close(self, session: ProviderSession | None = None) -> None: ...

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.any_schema()

"""Browser provider protocol."""

from collections.abc import Sequence
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


@runtime_checkable
class NetworkCaptureProvider(Protocol):
    """What a provider declaring ``NETWORK_CAPTURE`` promises beyond its launch (§4.9.x).

    It launches the browser the capability owns, so it knows the profile that
    browser starts from — and whether that profile is fresh is what decides
    §5.6's ``logged_in``: archivers need ``allow_session_state`` and the
    environment fingerprint reports ``fresh_profile`` from it.
    :meth:`persistent_profile` names the knob or environment variable that
    makes the browser start from a profile that is not fresh for this run,
    or returns ``None`` when every run gets a new one. A provider that
    declares ``NETWORK_CAPTURE`` without answering it is refused by the
    cooperation check (fail closed), never taken as fresh.
    """

    def persistent_profile(self) -> str | None: ...


@runtime_checkable
class LaunchArgsProvider(Protocol):
    """What a provider declaring ``LAUNCH_ARGS`` promises (§4.9.x).

    ``launch_args`` are Chrome switches some other party owns for this
    session — today only ``replayability: pinned``. The provider appends
    them after its own switches and merges the list switches
    (``chrome_switches.merge_chrome_switches``).

    :meth:`launch_arg_conflicts` lets the capability's cooperation check name
    the provider's side of a contradiction without reading the provider's
    fields or branching on ``provider_id``: it lists the provider's own knobs
    that set something ``launch_args`` also sets, differently — one entry per
    knob, each naming it (``"deterministic_rendering=true …"``).
    """

    async def open(self, *, launch_args: Sequence[str] = (), **kwargs: Any) -> ProviderSession: ...

    def launch_arg_conflicts(self, launch_args: Sequence[str]) -> list[str]: ...

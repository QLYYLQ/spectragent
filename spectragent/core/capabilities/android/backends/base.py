"""The Android provider contract: ``open()`` hands the capability a device's ADB endpoint.

``docs/architecture_design.md`` §11.2 ("Provider"). A provider brings a device
up and tells the capability where any ADB client reaches it: a loopback
:class:`AdbEndpoint` and the device's serial there. The ADB protocol and its
keys stay with Spectragent, as the CDP protocol does for the browser: a provider
hands over an address, never a client.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class AdbEndpoint(BaseModel):
    """A loopback address where an ADB client reaches a session's device.

    Attributes:
        host: The loopback host.
        port: The port.
        kind: What answers there. ``server``: an adb server speaking the host
            protocol, on which the session's ``serial`` selects the device
            (``host:transport:<serial>``). A provider that hands over a
            device's own ADB port, as a cloud phone's tunnel will, adds a kind.
        encrypted: Whether the connection to the device is encrypted.
        needs_adb_key: Whether the device must be given the run's ADB key
            before it accepts the connection. A server holds its key itself.
        expires_at: When the endpoint stops working (Unix time); ``None``: it
            lasts until the provider closes the session.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    port: int
    kind: Literal["server"]
    encrypted: bool
    needs_adb_key: bool
    expires_at: float | None = None


class AndroidProviderSession(BaseModel):
    """One device a provider brought up: where to reach it, and its serial there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    provider_id: str
    endpoint: AdbEndpoint
    serial: str


@runtime_checkable
class BaseAndroidProvider(Protocol):
    """Brings a device up for one run and releases it."""

    @property
    def provider_id(self) -> str: ...

    async def open(self) -> AndroidProviderSession: ...

    async def close(self, session: AndroidProviderSession | None = None) -> None:
        """Release *session*, or every session this provider opened when it is ``None``."""
        ...

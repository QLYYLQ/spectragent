"""The Android provider contract: ``open()`` hands the capability a device's ADB endpoint.

``docs/architecture_design.md`` §11.2 ("Provider"). A provider brings a device
up and tells the capability where any ADB client reaches it: a loopback
:class:`AdbEndpoint` and the device's serial there. The ADB protocol and its
keys stay with Spectragent, as the CDP protocol does for the browser: a provider
hands over an address, never a client.

What a run needs of its device beyond ADB (:class:`DeviceNeeds`) is never a
provider setting: the capability derives it from the strategies the run
selected (the task oracle's ``device_needs``) and hands it to ``open()``, so no
provider boolean can disagree with the strategy that needs it (§11.2 "任务裁判与
``AndroidSetup``"; the browser's isolation, derived from its network source, is
the precedent, §4.14).
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


class DeviceNeeds(BaseModel):
    """What a run needs of its device beyond ADB; ``open()`` returns a device that meets it.

    Derived from the run's strategies, never configured (module docstring). A
    provider that cannot meet a need says so in ``check_needs()``, which the
    capability's ``check_setup`` asks before anything starts.

    Attributes:
        emulator_grpc: The emulator's own gRPC endpoint, behind a token, on a
            loopback port chosen for the run; the session reports where it is
            (:attr:`EmulatorAccess.grpc`).
        root: adbd runs as root before anything attaches to the device. It
            restarts to become root, which drops the device for every client
            for about 1.5 s (measured), so it happens before the session is
            handed over.
        guest_wifi: The guest's Wi-Fi has its IPv4 default route. It came
            about 7 s after the boot completed, and once in eight boots after
            a wipe it never came (measured).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    emulator_grpc: bool = False
    root: bool = False
    guest_wifi: bool = False


class EmulatorGrpc(BaseModel):
    """Where the emulator's gRPC endpoint is, as the emulator itself reported it.

    Attributes:
        host: The address in its "Started GRPC server at" line: ``127.0.0.1``,
            or ``[::1]`` when that port was taken on IPv4.
        port: The port in that line.
        token_file: The discovery file the emulator wrote (``pid_<PID>.ini``);
            its ``grpc.token`` must ride on every call as ``authorization:
            Bearer <token>``. The token itself is never copied elsewhere.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    port: int
    token_file: str


class EmulatorAccess(BaseModel):
    """What a local emulator offers a second client of the run's adb server.

    Attributes:
        console_port: The console port the emulator bound; its serial is
            ``emulator-<console_port>``.
        home: The ``HOME`` the emulator and the run's adb server share. The
            emulator keeps its console auth token there, and a console
            command sent through adb (``adb emu ...``) reads it from the adb
            client's own ``HOME``, so a client must run with this one.
        adb: The adb binary the run's server runs. A client of another build
            would restart the server.
        grpc: The emulator's gRPC endpoint, when the run needed it
            (:attr:`DeviceNeeds.emulator_grpc`); ``None`` otherwise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    console_port: int
    home: str
    adb: str
    grpc: EmulatorGrpc | None = None


class AndroidProviderSession(BaseModel):
    """One device a provider brought up: where to reach it, and its serial there.

    ``emulator`` is set by a provider whose device is a local emulator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    provider_id: str
    endpoint: AdbEndpoint
    serial: str
    emulator: EmulatorAccess | None = None


@runtime_checkable
class BaseAndroidProvider(Protocol):
    """Brings a device up for one run and releases it."""

    @property
    def provider_id(self) -> str: ...

    def check_needs(self, needs: DeviceNeeds) -> None:
        """Raise, saying why, if this provider cannot meet *needs*; reads only its configuration."""
        ...

    async def open(self, needs: DeviceNeeds | None = None) -> AndroidProviderSession:
        """Bring a device up meeting *needs* (none when ``None``) and return where to reach it.

        Raises what :meth:`check_needs` raises before anything starts.
        """
        ...

    async def close(self, session: AndroidProviderSession | None = None) -> None:
        """Release *session*, or every session this provider opened when it is ``None``."""
        ...

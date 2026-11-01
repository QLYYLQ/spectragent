"""The device side for one run: install, start the helper, enable and confirm the service, stop.

``docs/architecture_design.md`` §11.2 ("设备端"). :meth:`DeviceSide.start` brings
both device-side programs up on one device and :meth:`DeviceSide.stop` takes them
down, within the section's four hard constraints:

1. no UiAutomation in its default mode, and 2. no ``uiautomator dump`` while
   the service is enabled: nothing here, in the helper or in the service opens
   a UiAutomation or runs ``uiautomator``
   (``tests/test_core/test_capabilities/test_android_device_side.py`` reads the
   sources);
3. never force-stop our own package on reset: :meth:`DeviceSide.stop` takes the
   service out of ``enabled_accessibility_services``, other services kept, which
   unbinds it, and leaves the package alone; nothing here sends ``am force-stop``;
4. binding is confirmed by a ping on the service's socket, never by reading the
   setting again: Android 14 and later drop a service they refuse from the
   setting without an error.

The steps of :meth:`DeviceSide.start`, all within one ``timeout_s``:

- **Install.** Skipped when the device's installed APK has the build's SHA-256,
  hashed on the device; otherwise ``cmd package install -r`` takes the APK on
  its stdin. A read-only boot throws the install away with the run, so each run
  on one installs again and the AVD stays as it was.
- **The helper.** ``app_process`` runs ``ShellHelper`` from the installed APK,
  with its socket's name and its token on its stdin, and it prints ``READY``
  once it listens. It lives as long as this shell session: closing the session
  ends its stdin and it exits.
- **Bootstrap.** ``content write`` to the bootstrap provider hands the app's
  process the service's token on stdin, and ``content call`` reports the
  service's socket. The service and the helper have a token each, and neither
  token is ever on a command line, where ``ps`` shows it to the shell and root
  users.
- **Enable.** The helper appends the service to ``enabled_accessibility_services``.
- **Confirm.** Connect to the service's socket and ping until it answers. A
  token refusal means the app's process restarted and lost the token, which is
  then handed over again.
- **Gestures.** Whether the device declares ``android.hardware.faketouch``, then
  one test tap: without the feature the system cancels every accessibility
  gesture while node actions still work. Both outcomes are measured facts
  (:attr:`DeviceSide.faketouch`, :attr:`DeviceSide.gestures`), never a reason
  to refuse here.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
from collections import deque

from pydantic import BaseModel, ConfigDict

from spectragent.core.capabilities.android.adb import AdbClient, AdbError, ShellSession
from spectragent.core.capabilities.android.device.apk import HELPER_CLASS, PACKAGE, ServiceApk
from spectragent.core.capabilities.android.device.channel import (
    Channel,
    ChannelError,
    HandshakeRefused,
    RemoteError,
)
from spectragent.core.capabilities.android.device.clients import HelperClient, Point, ServiceClient

#: The bootstrap provider (``BootstrapProvider.java``).
PROVIDER_URI = f"content://{PACKAGE}.bootstrap"
#: How often the confirmation pings while the service is not up yet.
_POLL_S = 0.1
#: How long stop() waits for each step on the device: taking the service out of
#: the setting, and the helper's last output once its stdin closed.
_STOP_STEP_S = 10.0
#: ``Result: Bundle[{json={...}}]``, as ``content call`` prints the provider's answer.
_BUNDLE = re.compile(r"Bundle\[\{json=(\{.*\})\}\]", re.DOTALL)


class DeviceSideError(RuntimeError):
    """A device-side step failed; the message says which and how."""


class ProviderHealth(BaseModel):
    """The bootstrap provider's report: the build, the service's socket and its state.

    ``service_connected`` and ``listening`` say what the app's process knows;
    only a ping confirms the binding (constraint 4, module docstring).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: int
    package: str
    version_code: int
    version_name: str
    accessibility_tool: bool
    sdk_int: int
    socket: str
    service_connected: bool
    listening: bool
    token_set: bool


class DeviceSide:
    """Both device-side programs on one device, for one run. Made by :meth:`start`."""

    def __init__(self, adb: AdbClient, serial: str, apk: ServiceApk) -> None:
        self.adb = adb
        self.serial = serial
        self.apk = apk
        #: The installed APK's path on the device.
        self.installed_path: str | None = None
        #: Whether :meth:`start` installed it (else the device had this build).
        self.installed_now = False
        #: Whether the device declares ``android.hardware.faketouch``; ``None``: it did not say.
        self.faketouch: bool | None = None
        #: Whether the test tap completed: accessibility gestures work.
        self.gestures: bool | None = None
        #: Why the test tap did not complete, as the service said.
        self.gesture_detail: str | None = None
        self._service_token = secrets.token_urlsafe(24)
        self._helper_token = secrets.token_urlsafe(24)
        self._helper_socket = f"spectragent_helper_{secrets.token_hex(8)}"
        self._service_socket: str | None = None
        self._helper_shell: ShellSession | None = None
        self._helper_output: deque[str] = deque(maxlen=40)
        self._helper_reading: asyncio.Future[None] | None = None
        self._helper: HelperClient | None = None
        self._service: ServiceClient | None = None
        #: Whether this run put the service into the setting, so stop() takes it out.
        self._enabled_service = False
        self._step = "not started"

    @property
    def service(self) -> ServiceClient:
        if self._service is None:
            raise RuntimeError("the accessibility service is not connected")
        return self._service

    @property
    def helper(self) -> HelperClient:
        if self._helper is None:
            raise RuntimeError("the shell helper is not running")
        return self._helper

    @classmethod
    async def start(
        cls,
        adb: AdbClient,
        serial: str,
        apk: ServiceApk,
        *,
        timeout_s: float = 60.0,
        test_tap: Point = (0, 0),
    ) -> DeviceSide:
        """Install, start the helper, enable the service and confirm it with a ping, within *timeout_s*.

        A failure stops whatever had started.

        Raises:
            DeviceSideError: A step failed; the message names it.
            TimeoutError: The steps took longer than *timeout_s*; the message
                names the step and what the device reported.
        """
        side = cls(adb, serial, apk)
        try:
            try:
                async with asyncio.timeout(timeout_s):
                    await side._start(test_tap)
            except TimeoutError:
                raise TimeoutError(
                    f"the device side of {serial} was not up within {timeout_s:g} s: {side._step}"
                ) from None
        except BaseException as failure:
            try:
                await side.stop()
            except Exception as exc:
                failure.add_note(f"stopping the device side also failed: {exc}")
            raise
        return side

    async def ensure_bound(self, *, timeout_s: float) -> bool:
        """Ping the service; when it does not answer, enable it again and wait for a ping.

        For a service the system unbound since: a force-stop of our package (a
        task may do one) takes it out of the setting. Returns whether it had to;
        then :attr:`service` is a new channel, and an event stream of the old
        one has ended (``ChannelClosed``), so whoever read events subscribes
        again.

        Raises:
            DeviceSideError, TimeoutError: As :meth:`start`.
        """
        try:
            async with asyncio.timeout(timeout_s):
                await self.service.ping()
            return False
        except (ChannelError, TimeoutError):
            pass
        await self.service.close()
        self._service = None
        try:
            async with asyncio.timeout(timeout_s):
                await self._enable_and_confirm()
        except TimeoutError:
            raise TimeoutError(
                f"the service on {self.serial} did not answer within {timeout_s:g} s after it was "
                f"enabled again: {self._step}"
            ) from None
        return True

    async def stop(self) -> None:
        """Close both channels, take the service out of the setting if this run put it there, end the helper.

        Never force-stops our package. Safe to repeat.

        Raises:
            DeviceSideError: The service could not be taken out of the
                setting; everything else was stopped anyway.
        """
        failure: Exception | None = None
        if self._service is not None:
            await self._service.close()
            self._service = None
        if self._enabled_service and self._helper is not None:
            try:
                async with asyncio.timeout(_STOP_STEP_S):
                    await self._helper.disable_service()
                self._enabled_service = False
            except (ChannelError, OSError, TimeoutError) as exc:
                failure = exc
        if self._helper is not None:
            await self._helper.close()
            self._helper = None
        if self._helper_shell is not None:
            await self._helper_shell.close()  # its stdin ends and the helper exits
            self._helper_shell = None
        if self._helper_reading is not None:
            await asyncio.wait([self._helper_reading], timeout=_STOP_STEP_S)
            self._helper_reading.cancel()
        if failure is not None:
            raise DeviceSideError(
                f"could not take {PACKAGE}'s service out of enabled_accessibility_services on "
                f"{self.serial}: {failure}"
            ) from failure

    async def health(self) -> ProviderHealth:
        """The bootstrap provider's report, which it gives while the service's socket is down."""
        result = await self.adb.shell(
            self.serial, f"content call --uri {PROVIDER_URI} --method health"
        )
        found = _BUNDLE.search(result.stdout.decode(errors="replace"))
        if result.exit_code != 0 or found is None:
            said = (result.stdout + result.stderr).decode(errors="replace").strip()
            raise DeviceSideError(f"the bootstrap provider did not report its health: {said}")
        return ProviderHealth.model_validate_json(found[1])

    # ── the steps ─────────────────────────────────────────────────────

    async def _start(self, test_tap: Point) -> None:
        self._step = "installing the APK"
        self.installed_path, self.installed_now = await self._install()
        self._step = "starting the shell helper"
        self._helper = await self._start_helper(self.installed_path)
        await self._enable_and_confirm()
        self._step = "checking gestures"
        self.faketouch = await self._has_feature("android.hardware.faketouch")
        try:
            await self.service.tap(*test_tap)
            self.gestures = True
        except RemoteError as exc:
            self.gestures, self.gesture_detail = False, str(exc)

    async def _install(self) -> tuple[str, bool]:
        """The installed APK's path, and whether it had to be installed."""
        path = await self._installed_path()
        if path is not None and await self._device_sha256(path) == self.apk.info.sha256:
            return path, False
        data = self.apk.path.read_bytes()
        result = await self.adb.shell(
            self.serial, f"cmd package install -r -S {len(data)}", stdin=data
        )
        if result.exit_code != 0 or b"Success" not in result.stdout:
            said = (result.stdout + result.stderr).decode(errors="replace").strip()
            raise DeviceSideError(f"installing {self.apk.path} on {self.serial} failed: {said}")
        path = await self._installed_path()
        if path is None:
            raise DeviceSideError(f"{PACKAGE} was installed on {self.serial} and is not there")
        return path, True

    async def _installed_path(self) -> str | None:
        result = await self.adb.shell(self.serial, f"pm path {PACKAGE}")
        for line in result.stdout.decode(errors="replace").splitlines():
            if line.startswith("package:"):
                return line.removeprefix("package:").strip()
        return None

    async def _device_sha256(self, path: str) -> str:
        result = await self.adb.shell(self.serial, f"sha256sum {shlex.quote(path)}")
        return result.stdout.decode(errors="replace").split(" ", 1)[0].strip()

    async def _start_helper(self, apk_path: str) -> HelperClient:
        shell = await self.adb.open_shell(
            self.serial, f"CLASSPATH={shlex.quote(apk_path)} app_process /system/bin {HELPER_CLASS}"
        )
        self._helper_shell = shell
        ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._helper_reading = asyncio.ensure_future(self._read_helper(shell, ready))
        await shell.write(f"{self._helper_socket} {self._helper_token}\n".encode())
        await ready
        reader, writer = await self.adb.connect_abstract(self.serial, self._helper_socket)
        return HelperClient(
            await Channel.open(reader, writer, token=self._helper_token, component="helper")
        )

    async def _read_helper(self, shell: ShellSession, ready: asyncio.Future[str]) -> None:
        """Keep the helper's last lines, and settle *ready* with its READY line or its exit."""
        partial = ""
        try:
            while (output := await shell.read()) is not None:
                partial += output.data.decode(errors="replace")
                *lines, partial = partial.split("\n")
                for line in lines:
                    self._helper_output.append(line)
                    if line.startswith("READY ") and not ready.done():
                        ready.set_result(line)
            ended = f"the helper exited with code {shell.exit_code}"
        except (AdbError, OSError) as exc:
            ended = f"the helper's shell session ended: {exc}"
        if not ready.done():
            lines = "\n".join(self._helper_output)
            ready.set_exception(DeviceSideError(f"{ended} before it was ready:\n{lines}"))

    async def _enable_and_confirm(self) -> None:
        self._step = "handing the service's token to the bootstrap provider"
        await self._hand_over_token()
        self._service_socket = (await self.health()).socket
        self._step = "enabling the service"
        if (await self.helper.enable_service()).changed:
            self._enabled_service = True
        self._step = "waiting for the service to answer a ping"
        while True:
            try:
                self._service = await self._connect_service()
                return
            except HandshakeRefused as refused:
                if refused.reason != "token":
                    raise DeviceSideError(
                        f"the service on {self.serial} refused: {refused}"
                    ) from None
                # The app's process restarted after the bootstrap and lost the token,
                # or had not finished reading it yet.
                await self._hand_over_token()
                last = str(refused)
            except (AdbError, OSError, ChannelError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            self._step = f"waiting for the service to answer a ping (last: {last})"
            await asyncio.sleep(_POLL_S)

    async def _connect_service(self) -> ServiceClient:
        assert self._service_socket is not None  # set by the bootstrap
        reader, writer = await self.adb.connect_abstract(self.serial, self._service_socket)
        service = ServiceClient(
            await Channel.open(reader, writer, token=self._service_token, component="service")
        )
        try:
            await service.ping()
        except BaseException:
            await service.close()
            raise
        return service

    async def _hand_over_token(self) -> None:
        """Write the service's token to the provider through stdin: no command line carries it."""
        result = await self.adb.shell(
            self.serial,
            f"content write --uri {PROVIDER_URI}/session",
            stdin=self._service_token.encode(),
        )
        if result.exit_code != 0:
            said = (result.stdout + result.stderr).decode(errors="replace").strip()
            raise DeviceSideError(f"the bootstrap provider did not take the token: {said}")

    async def _has_feature(self, feature: str) -> bool | None:
        answer = (await self.adb.shell(self.serial, f"pm has-feature {feature}")).stdout
        return {b"true": True, b"false": False}.get(answer.strip())

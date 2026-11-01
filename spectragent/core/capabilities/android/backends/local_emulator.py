"""The local emulator provider: the official Android emulator, booted headless from an AVD.

``docs/architecture_design.md`` §11.2 ("Provider", "两把钥匙"). For each session
it makes a run directory, starts the run's private adb server there
(``adb_server.py``), starts the emulator pointed at that server and waits until
the device has booted. The emulator's environment carries what the stage-0
study measured (2026-11-01, emulator 37.2.13, adb 1.0.41):

- ``ANDROID_ADB_SERVER_PORT`` names the private server. The emulator's own adb
  calls read only this variable; without it they start a global server on
  5037.
- ``ANDROID_EMULATOR_HOME`` is the run's ``.android`` directory, where the
  private server wrote the run's key. The emulator hands the guest that key
  (its log's "Sending adb public key"), never the user's
  ``~/.android/adbkey.pub``, and writes its own files there instead of in
  ``~/.android``. ``ANDROID_AVD_HOME`` keeps pointing at the AVDs.
- ``NO_PROXY`` / ``no_proxy`` start with the loopback addresses: the
  emulator's internal gRPC over localhost would otherwise follow
  ``http_proxy`` / ``https_proxy``.

The emulator runs headless (``-no-window -no-audio -no-boot-anim``, software
rendering ``-gpu swiftshader_indirect``), never saves a snapshot
(``-no-snapshot-save``: one is about 2.1 GB) and by default ``-read-only``: its
disk images and ``config.ini`` stay as they were (measured), so every session
boots the same device and the AVD can serve several sessions at once. It
still rewrites its own bookkeeping files in the AVD directory
(``emu-launch-params.txt``, ``emulator-user.ini``).

The console port is an even port from 5554 to 5584 whose neighbour, the
emulator's ADB port, is free too (the emulator takes no other); the serial is
``emulator-<console port>``. Teardown stops the emulator (SIGTERM; it exited
within 1.5 s and its ``netsimd`` and crash handler with it, measured), then the
adb server, then removes the run directory. Every wait is bounded, and a run
whose process did not exit stays owned, so ``close()`` can be retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import socket
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from pydantic import Field

from spectragent.core.capabilities.android.adb import AdbClient, AdbError
from spectragent.core.capabilities.android.adb_server import HOST, PrivateAdbServer
from spectragent.core.capabilities.android.backends._registrar import AndroidProviderBase
from spectragent.core.capabilities.android.backends.base import (
    AdbEndpoint,
    AndroidProviderSession,
)
from spectragent.core.capabilities.android.processes import finish, log_tail, spawn, stop

#: The console ports the emulator accepts; each takes the next port for ADB.
CONSOLE_PORTS = range(5554, 5586, 2)
#: What the emulator's NO_PROXY starts with.
LOOPBACK = ("localhost", "127.0.0.1", "::1")
#: Variables of this process the emulator must not inherit: each would point
#: it at another home, another set of AVDs or another adb server.
_NOT_INHERITED = (
    "ANDROID_USER_HOME",
    "ANDROID_SDK_HOME",
    "ANDROID_EMULATOR_HOME",
    "ANDROID_AVD_HOME",
    "ANDROID_ADB_SERVER_PORT",
    "ADB_SERVER_SOCKET",
)
_BOOT_POLL_S = 0.5
_BOOT_QUERY_TIMEOUT_S = 5.0


def find_sdk(configured: str | None, env: Mapping[str, str]) -> Path:
    """The Android SDK whose ``platform-tools/adb`` and ``emulator/emulator`` a run uses.

    *configured* when set; otherwise the first of ``ANDROID_HOME``,
    ``ANDROID_SDK_ROOT`` and the platform's default location that holds both.

    Raises:
        FileNotFoundError: No candidate holds both, naming each one tried.
    """
    if configured:
        candidates = [Path(configured).expanduser()]
    else:
        named = (env.get("ANDROID_HOME"), env.get("ANDROID_SDK_ROOT"))
        candidates = [Path(value).expanduser() for value in named if value]
        candidates.append(
            Path.home() / ("Library/Android/sdk" if sys.platform == "darwin" else "Android/Sdk")
        )
    for sdk in candidates:
        if (sdk / "platform-tools" / "adb").is_file() and (sdk / "emulator" / "emulator").is_file():
            return sdk
    raise FileNotFoundError(
        f"no Android SDK with platform-tools/adb and emulator/emulator at "
        f"{', '.join(str(sdk) for sdk in candidates)}; set the provider's sdk_root, or ANDROID_HOME"
    )


def find_avd_home(configured: str | None, env: Mapping[str, str]) -> Path:
    """Where the AVDs are: *configured*, else where the emulator itself would look.

    ``ANDROID_AVD_HOME``, then ``avd`` under ``ANDROID_EMULATOR_HOME``,
    ``ANDROID_USER_HOME`` or ``ANDROID_SDK_HOME/.android``, then
    ``~/.android/avd``.
    """
    if configured:
        return Path(configured).expanduser()
    if env.get("ANDROID_AVD_HOME"):
        return Path(env["ANDROID_AVD_HOME"]).expanduser()
    for name, below in (
        ("ANDROID_EMULATOR_HOME", "avd"),
        ("ANDROID_USER_HOME", "avd"),
        ("ANDROID_SDK_HOME", ".android/avd"),
    ):
        if env.get(name):
            return Path(env[name]).expanduser() / below
    return Path.home() / ".android" / "avd"


def emulator_command(emulator: Path, avd: str, console_port: int, *, read_only: bool) -> list[str]:
    """The emulator's command line for one headless session of *avd*."""
    command = [
        str(emulator),
        "-avd",
        avd,
        "-port",
        str(console_port),
        "-no-window",
        "-no-audio",
        "-no-boot-anim",
        "-no-snapshot-save",
        "-gpu",
        "swiftshader_indirect",
    ]
    if read_only:
        command.append("-read-only")
    return command


def emulator_environment(
    base: Mapping[str, str], *, adb_port: int, emulator_home: Path, avd_home: Path
) -> dict[str, str]:
    """The emulator's environment, built on *base*: the run's adb server, key, home and AVDs."""
    env = {name: value for name, value in base.items() if name not in _NOT_INHERITED}
    existing = base.get("NO_PROXY") or base.get("no_proxy")
    no_proxy = ",".join([*LOOPBACK, existing] if existing else LOOPBACK)
    env.update(
        ANDROID_ADB_SERVER_PORT=str(adb_port),
        ANDROID_EMULATOR_HOME=str(emulator_home),
        ANDROID_AVD_HOME=str(avd_home),
        NO_PROXY=no_proxy,
        no_proxy=no_proxy,
    )
    return env


@dataclass
class _Run:
    """What one session owns, released in reverse order of acquiring it."""

    directory: Path
    server: PrivateAdbServer | None = None
    emulator: asyncio.subprocess.Process | None = None
    releasing: asyncio.Future[None] | None = None


@dataclass
class LocalEmulatorProvider(AndroidProviderBase):
    """Boots the official Android emulator from an AVD on this machine, one device per session."""

    __registry_key__ = "local_emulator"

    avd: Annotated[
        str,
        Field(description="The AVD to boot, by name, as `emulator -list-avds` prints it."),
    ]
    sdk_root: Annotated[
        str | None,
        Field(
            description=(
                "The Android SDK holding platform-tools/adb and emulator/emulator. None (the "
                "default): ANDROID_HOME, then ANDROID_SDK_ROOT, then the platform's default "
                "location (~/Library/Android/sdk on macOS, ~/Android/Sdk elsewhere)."
            )
        ),
    ] = None
    avd_home: Annotated[
        str | None,
        Field(
            description=(
                "The directory holding the AVDs. None (the default): where the emulator itself "
                "looks (ANDROID_AVD_HOME, then the avd directory of ANDROID_EMULATOR_HOME, "
                "ANDROID_USER_HOME or ANDROID_SDK_HOME/.android, then ~/.android/avd)."
            )
        ),
    ] = None
    read_only: Annotated[
        bool,
        Field(
            description=(
                "Boot with -read-only: the AVD's disk images are not written, so every session "
                "starts from the same device, changes are discarded at shutdown, and several "
                "sessions may run the same AVD at once. False writes the session's changes "
                "into the AVD, where the next session finds them, and allows one session at "
                "a time."
            )
        ),
    ] = True
    boot_timeout_s: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Seconds the device may take to boot (sys.boot_completed); a cold boot took "
                "about 18 s on an Apple M1 Pro."
            ),
        ),
    ] = 180.0
    shutdown_timeout_s: Annotated[
        float,
        Field(
            gt=0,
            description="Seconds the emulator gets to shut down after SIGTERM before SIGKILL.",
        ),
    ] = 20.0

    _runs: dict[str, _Run] = field(default_factory=dict, init=False, repr=False)

    async def open(self) -> AndroidProviderSession:
        """Boot one device and return where to reach it.

        Raises:
            FileNotFoundError: The SDK or the AVD cannot be found.
            RuntimeError: The adb server or the emulator exited before the
                device booted; the message carries the end of its log.
            TimeoutError: The device did not boot within ``boot_timeout_s``.
        """
        sdk = find_sdk(self.sdk_root, os.environ)
        avd_home = find_avd_home(self.avd_home, os.environ)
        if not (avd_home / f"{self.avd}.ini").is_file():
            found = sorted(path.stem for path in avd_home.glob("*.ini"))
            raise FileNotFoundError(
                f"no AVD named {self.avd!r} in {avd_home} (found: {', '.join(found) or 'none'})"
            )
        session_id = uuid.uuid4().hex
        run = _Run(Path(tempfile.mkdtemp(prefix="spectragent-android-")))
        self._runs[session_id] = run
        try:
            run.server = await PrivateAdbServer.start(
                str(sdk / "platform-tools" / "adb"), run.directory
            )
            console_port = _console_port()
            run.emulator = await spawn(
                emulator_command(
                    sdk / "emulator" / "emulator",
                    self.avd,
                    console_port,
                    read_only=self.read_only,
                ),
                env=emulator_environment(
                    os.environ,
                    adb_port=run.server.port,
                    emulator_home=run.directory / ".android",
                    avd_home=avd_home,
                ),
                log=_emulator_log(run),
            )
            serial = f"emulator-{console_port}"
            await self._wait_for_boot(run, run.server, run.emulator, serial)
        except BaseException as failure:
            try:
                await self._close_run(session_id)
            except Exception as exc:
                failure.add_note(f"releasing the emulator also failed: {exc}")
            raise
        return AndroidProviderSession(
            session_id=session_id,
            provider_id=self.provider_id,
            endpoint=AdbEndpoint(
                host=HOST,
                port=run.server.port,
                kind="server",
                encrypted=False,
                needs_adb_key=False,
            ),
            serial=serial,
        )

    async def close(self, session: AndroidProviderSession | None = None) -> None:
        """Release *session*, or every session this provider opened when it is ``None``."""
        session_ids = [session.session_id] if session is not None else list(self._runs)
        errors: list[Exception] = []
        for session_id in session_ids:
            try:
                await self._close_run(session_id)
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("releasing Android emulator sessions failed", errors)

    async def _wait_for_boot(
        self,
        run: _Run,
        server: PrivateAdbServer,
        emulator: asyncio.subprocess.Process,
        serial: str,
    ) -> None:
        """Poll ``sys.boot_completed`` through the run's server until it reads 1."""
        client = AdbClient(HOST, server.port)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.boot_timeout_s
        while True:
            if emulator.returncode is not None:
                raise RuntimeError(
                    f"the emulator exited with code {emulator.returncode} before {serial} "
                    f"booted:\n{log_tail(_emulator_log(run))}"
                )
            if server.process.returncode is not None:
                raise RuntimeError(
                    f"the run's adb server exited with code {server.process.returncode} "
                    f"while {serial} was booting:\n{log_tail(server.log)}"
                )
            try:
                async with asyncio.timeout(_BOOT_QUERY_TIMEOUT_S):
                    answer = await client.shell(serial, "getprop sys.boot_completed")
                if answer.stdout.strip() == b"1":
                    return
                last = f"sys.boot_completed was {answer.stdout.strip().decode(errors='replace')!r}"
            except (AdbError, OSError, TimeoutError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"{serial} had not booted after {self.boot_timeout_s:g} s ({last}):\n"
                    f"{log_tail(_emulator_log(run))}"
                )
            await asyncio.sleep(_BOOT_POLL_S)

    async def _close_run(self, session_id: str) -> None:
        run = self._runs.get(session_id)
        if run is None:
            return
        if run.releasing is None or run.releasing.done():
            run.releasing = asyncio.ensure_future(self._release(run))
        await finish(run.releasing)
        self._runs.pop(session_id, None)

    async def _release(self, run: _Run) -> None:
        """Stop the emulator, then the adb server it talks to, then remove the run directory."""
        if run.emulator is not None:
            await stop(run.emulator, grace_s=self.shutdown_timeout_s)
        if run.server is not None:
            await run.server.stop()
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(run.directory)


def _emulator_log(run: _Run) -> Path:
    return run.directory / "emulator.log"


def _console_port() -> int:
    """The first console port that is free, with the ADB port after it."""
    for port in CONSOLE_PORTS:
        if _free(port) and _free(port + 1):
            return port
    raise RuntimeError(
        f"no free emulator console port: the emulator takes an even port from "
        f"{CONSOLE_PORTS.start} to {CONSOLE_PORTS.stop - 2} and the one after it, and every pair "
        f"is in use"
    )


def _free(port: int) -> bool:
    """Whether nothing listens on *port* on the loopback addresses the emulator binds."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            # Any other error is a host without that address family: nothing holds the port there.
    return True

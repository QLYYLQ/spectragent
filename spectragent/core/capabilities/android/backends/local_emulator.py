"""The local emulator provider: the official Android emulator, booted headless from an AVD.

``docs/architecture_design.md`` §11.2 ("Provider", "两把钥匙"). For each session
it makes a run directory, starts the run's private adb server there
(``adb_server.py``), starts the emulator pointed at that server and waits until
the device has booted. Both programs run in process groups of the run's own,
tied to this Python process (``processes.py``): a killed run leaves no emulator
behind. The emulator's environment carries what was measured (emulator 37.2.13,
adb 1.0.41, 2026-11-01):

- ``ANDROID_ADB_SERVER_PORT`` names the private server. The emulator's own adb
  calls read only this variable; without it they start a global server on
  5037.
- ``ANDROID_EMULATOR_HOME`` is the run's ``.android`` directory, where the
  private server wrote the run's key. The emulator hands the guest that key
  (its log's "Sending adb public key"), never the user's
  ``~/.android/adbkey.pub``, and writes its own files there instead of in
  ``~/.android``. ``ANDROID_AVD_HOME`` keeps pointing at the AVDs.
- ``TMPDIR`` is the run's ``tmp`` directory. ``netsimd``, which carries the
  guest's network, is shared by every emulator started with one ``TMPDIR``
  (``$TMPDIR/netsim.ini``) and lives in the process group of the emulator that
  started it, so a shared one would die with the first run's group and cut the
  others' network mid-task.
- ``ANDROID_HOME`` is the configured SDK, and ``ANDROID_SDK_ROOT`` is not passed
  on: the emulator reads them before its own location.
- ``NO_PROXY`` / ``no_proxy`` start with the loopback addresses: the
  emulator's internal gRPC over localhost would otherwise follow
  ``http_proxy`` / ``https_proxy``.

The emulator runs headless (``-no-window -no-audio -no-boot-anim``, software
rendering ``-gpu swiftshader_indirect``) and never saves a snapshot
(``-no-snapshot-save``: one is about 2.1 GB). By default it boots
``-read-only``: its disk images, ``config.ini`` and ``hardware-qemu.ini`` stay
as they were, so every session starts from the same device. It still writes
into the AVD directory on every launch: ``initrd`` (whose boot configuration
carries that run's adb public key), ``bootcompleted.ini``, ``tmpAdbCmds/``,
``emu-launch-params.txt``, ``emulator-user.ini``,
``snapshots/default_boot/snapshot.pb`` and a few files under
``modem_simulator/``. Two sessions of one AVD at once were not measured.

The emulator binds the first free console port pair itself (5554 and 5555,
then 5556 and 5557, ...) rather than being handed one, which another run could
take between the provider's check and the emulator's start. It reports the
port it bound (``-report-console``) to a loopback listener of the provider; the
serial is ``emulator-<that port>``. Other adb servers that scan the default
ports still find it.

Teardown stops the emulator (SIGTERM; it exited within 1.5 s, measured) and the
rest of its group, its ``netsimd`` included, then the adb server, then removes
the run directory. Every wait is bounded, and a run whose process did not exit
stays owned, so ``close()`` can be retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
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
from spectragent.core.capabilities.android.processes import ProcessGroup, finish, log_tail

#: What the emulator's NO_PROXY starts with.
LOOPBACK = ("localhost", "127.0.0.1", "::1")
#: Variables of this process the emulator must not inherit: each would point
#: it at another home, another SDK, another set of AVDs or another adb server.
_NOT_INHERITED = (
    "ANDROID_USER_HOME",
    "ANDROID_SDK_HOME",
    "ANDROID_SDK_ROOT",
    "ANDROID_EMULATOR_HOME",
    "ANDROID_AVD_HOME",
    "ANDROID_ADB_SERVER_ADDRESS",
    "ANDROID_ADB_SERVER_PORT",
    "ADB_SERVER_SOCKET",
)
_BOOT_POLL_S = 0.5
_BOOT_QUERY_TIMEOUT_S = 5.0
#: How long the emulator's report of its console port may take to arrive once it connected.
_REPORT_READ_S = 5.0


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


def emulator_command(emulator: Path, avd: str, report_port: int, *, read_only: bool) -> list[str]:
    """The emulator's command line for one headless session of *avd*.

    The emulator picks its console port and reports it to *report_port* on the
    loopback address.
    """
    command = [
        str(emulator),
        "-avd",
        avd,
        "-report-console",
        f"tcp:{report_port}",
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
    base: Mapping[str, str],
    *,
    sdk: Path,
    adb_port: int,
    emulator_home: Path,
    avd_home: Path,
    tmpdir: Path,
) -> dict[str, str]:
    """The emulator's environment, built on *base*: the run's SDK, adb server, key, home, AVDs and tmp."""
    env = {name: value for name, value in base.items() if name not in _NOT_INHERITED}
    existing = base.get("NO_PROXY") or base.get("no_proxy")
    no_proxy = ",".join([*LOOPBACK, existing] if existing else LOOPBACK)
    env.update(
        ANDROID_HOME=str(sdk),
        ANDROID_ADB_SERVER_PORT=str(adb_port),
        ANDROID_EMULATOR_HOME=str(emulator_home),
        ANDROID_AVD_HOME=str(avd_home),
        TMPDIR=str(tmpdir),
        NO_PROXY=no_proxy,
        no_proxy=no_proxy,
    )
    return env


@dataclass
class _ConsoleReport:
    """A loopback listener the emulator reports the console port it bound to (``-report-console``)."""

    server: asyncio.Server
    console_port: asyncio.Future[int]

    @property
    def port(self) -> int:
        return self.server.sockets[0].getsockname()[1]

    @classmethod
    async def listen(cls) -> _ConsoleReport:
        console_port: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                text = (await asyncio.wait_for(reader.read(), _REPORT_READ_S)).decode().strip()
            except (TimeoutError, UnicodeDecodeError, ConnectionError) as exc:
                text = f"<{type(exc).__name__}>"
            finally:
                writer.close()
            if not console_port.done():
                if text.isdigit():
                    console_port.set_result(int(text))
                else:
                    console_port.set_exception(
                        RuntimeError(f"the emulator reported {text!r} as its console port")
                    )

        return cls(await asyncio.start_server(receive, HOST, 0), console_port)

    def close(self) -> None:
        self.server.close()


@dataclass
class _Run:
    """What one session owns, released in reverse order of acquiring it."""

    directory: Path
    server: PrivateAdbServer | None = None
    emulator_group: ProcessGroup | None = None
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
                "starts from the same device and its changes are discarded at shutdown. False "
                "writes the session's changes into the AVD, where the next session finds them."
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
        report: _ConsoleReport | None = None
        try:
            (run.directory / "tmp").mkdir()
            run.server = await PrivateAdbServer.start(
                str(sdk / "platform-tools" / "adb"), run.directory
            )
            report = await _ConsoleReport.listen()
            run.emulator_group = await ProcessGroup.open()
            run.emulator = await run.emulator_group.spawn(
                emulator_command(
                    sdk / "emulator" / "emulator",
                    self.avd,
                    report.port,
                    read_only=self.read_only,
                ),
                env=emulator_environment(
                    os.environ,
                    sdk=sdk,
                    adb_port=run.server.port,
                    emulator_home=run.directory / ".android",
                    avd_home=avd_home,
                    tmpdir=run.directory / "tmp",
                ),
                log=_emulator_log(run),
            )
            serial = await self._wait_for_boot(run, run.server, run.emulator, report)
        except BaseException as failure:
            try:
                await self._close_run(session_id)
            except Exception as exc:
                failure.add_note(f"releasing the emulator also failed: {exc}")
            raise
        finally:
            if report is not None:
                report.close()
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
        report: _ConsoleReport,
    ) -> str:
        """Learn the emulator's serial from its report, then poll ``sys.boot_completed`` until 1."""
        client = AdbClient(HOST, server.port)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.boot_timeout_s
        serial: str | None = None
        last = "the emulator had not reported its console port"
        while True:
            if emulator.returncode is not None:
                raise RuntimeError(
                    f"the emulator exited with code {emulator.returncode} before it "
                    f"booted:\n{log_tail(_emulator_log(run))}"
                )
            if server.process.returncode is not None:
                raise RuntimeError(
                    f"the run's adb server exited with code {server.process.returncode} "
                    f"while the emulator was booting:\n{log_tail(server.log)}"
                )
            if serial is None and report.console_port.done():
                serial = f"emulator-{report.console_port.result()}"
            if serial is not None:
                try:
                    async with asyncio.timeout(_BOOT_QUERY_TIMEOUT_S):
                        answer = await client.shell(serial, "getprop sys.boot_completed")
                    if answer.stdout.strip() == b"1":
                        return serial
                    booted = answer.stdout.strip().decode(errors="replace")
                    last = f"sys.boot_completed was {booted!r}"
                except (AdbError, OSError, TimeoutError) as exc:
                    last = f"{type(exc).__name__}: {exc}"
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"{serial or 'the emulator'} had not booted after {self.boot_timeout_s:g} s "
                    f"({last}):\n{log_tail(_emulator_log(run))}"
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
        """Stop the emulator and its group, then the adb server, then remove the run directory."""
        if run.emulator_group is not None:
            await run.emulator_group.close(run.emulator, grace_s=self.shutdown_timeout_s)
        if run.server is not None:
            await run.server.stop()
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(run.directory)


def _emulator_log(run: _Run) -> Path:
    return run.directory / "emulator.log"

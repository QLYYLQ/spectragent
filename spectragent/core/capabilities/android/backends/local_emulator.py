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
- ``HOME`` is the run's directory, which is the private server's ``HOME``
  too, and ``ANDROID_EMULATOR_HOME`` its ``.android``, where the server wrote
  the run's key. The emulator hands the guest that key (its log's "Sending
  adb public key"), never the user's ``~/.android/adbkey.pub``. It keeps its
  console auth token in ``$HOME/.emulator_console_auth_token``, and a console
  command sent through adb (``adb emu ...``) reads the token from the adb
  client's own ``HOME``: with two different homes such commands fail without a
  word. It also writes nothing into the user's home, its discovery file
  ``pid_<PID>.ini`` included; ``ANDROID_EMULATOR_DISCOVERY_DIR``, which would
  move that file and the gRPC token in it elsewhere, is not passed on.
  ``ANDROID_AVD_HOME`` keeps pointing at the AVDs.
- ``TMPDIR`` is the run's ``tmp`` directory. ``netsimd``, which carries the
  guest's network, is shared by every emulator started with one ``TMPDIR``
  (``$TMPDIR/netsim.ini``) and lives in the process group of the emulator that
  started it, so a shared one would die with the first run's group and cut the
  others' network mid-task. ``ANDROID_TMP``, the emulator's own temp directory
  variable, is not passed on.
- ``ANDROID_HOME`` is the configured SDK, and ``ANDROID_SDK_ROOT`` is not passed
  on: the emulator reads them before its own location.
- ``NO_PROXY`` / ``no_proxy`` start with the loopback addresses: the
  emulator's internal gRPC over localhost would otherwise follow
  ``http_proxy`` / ``https_proxy``. ``no_grpc_proxy``, which gRPC reads before
  ``no_proxy``, is not passed on.
- ``ADB_EMU``, ``ADB_USB`` and ``ADB_MDNS`` are ``0``, as for the run's
  server: should the emulator's own adb calls start a server, the run's having
  died during the boot, it scans neither USB nor the network and asks no phone
  to trust the run's key.

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

What the run needs beyond ADB (``DeviceNeeds``, derived from its benchmark setup component)
is met before ``open()`` returns, within the same ``boot_timeout_s`` (measured
on macOS, emulator 37.2.13, API 33 Google APIs, 2026-11-01):

- ``emulator_grpc``: ``-grpc <port> -grpc-use-token``, the port a free one
  picked for the run. The address is the one in the emulator's "Started GRPC
  server at <host>:<port>" line, never assumed (``127.0.0.1``; ``[::1]`` when
  that port was taken on IPv4), and the token is in the discovery file the
  emulator writes under the run's ``HOME``,
  ``Library/Caches/TemporaryItems/avd/running/pid_<emulator PID>.ini``
  (``grpc.token``). Where it writes the file elsewhere than macOS was not
  measured, so this need is refused there.
- ``root``: adbd restarts as root (``root:``): it answered "restarting adbd
  as root" and dropped the device for 1.5 s; the wait ends when ``id -u``
  says 0. Right after the boot completed, ``root:`` sometimes met adbd
  restarting on its own (``FAIL closed``, or no answer), so it is sent again
  while the shell is not root.
- ``guest_wifi``: an IPv4 default route over ``wlan0`` in any routing table
  (``default via 10.0.2.2 dev wlan0 table 1016``), which came 4-7 s after the
  boot completed. On about 3 of 24 boots it never came, and the boot fails
  after ``boot_timeout_s`` naming the ``wlan0`` routes the guest had; how to
  recover such a boot is not decided.
- ``fresh``: ``read_only``; the provider refuses this need when it is false.

Teardown stops the emulator (SIGTERM; it exited within 1.5 s, measured) and the
rest of its group, its ``netsimd`` included, then the adb server, then removes
the run directory. Every wait is bounded, and a run whose process did not exit
stays owned, so ``close()`` can be retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, TypeVar

from pydantic import Field

from spectragent.core.capabilities.android.adb import AdbClient, AdbError
from spectragent.core.capabilities.android.adb_server import (
    HOST,
    NO_SCANS,
    PrivateAdbServer,
    free_port,
)
from spectragent.core.capabilities.android.backends._registrar import AndroidProviderBase
from spectragent.core.capabilities.android.backends.base import (
    AdbEndpoint,
    AndroidProviderSession,
    DeviceNeeds,
    EmulatorAccess,
    EmulatorGrpc,
    UnmetDeviceNeeds,
)
from spectragent.core.capabilities.android.processes import ProcessGroup, finish, log_tail
from spectragent.core.trace.redaction import trace_safe

T = TypeVar("T")

#: What the emulator's NO_PROXY starts with.
LOOPBACK = ("localhost", "127.0.0.1", "::1")
#: Variables of this process the emulator must not inherit: each would point
#: it at another home, SDK, set of AVDs, adb server, temp or discovery
#: directory, or take localhost out of its proxy exceptions.
_NOT_INHERITED = (
    "ANDROID_USER_HOME",
    "ANDROID_SDK_HOME",
    "ANDROID_SDK_ROOT",
    "ANDROID_EMULATOR_HOME",
    "ANDROID_AVD_HOME",
    "ANDROID_EMULATOR_DISCOVERY_DIR",
    "ANDROID_TMP",
    "ANDROID_ADB_SERVER_ADDRESS",
    "ANDROID_ADB_SERVER_PORT",
    "ADB_SERVER_SOCKET",
    "no_grpc_proxy",
)
_BOOT_POLL_S = 0.5
_BOOT_QUERY_TIMEOUT_S = 5.0
#: How long the emulator's report of its console port may take to arrive once it connected.
_REPORT_READ_S = 5.0
#: The emulator's line once its gRPC endpoint listens: ``Started GRPC server at
#: 127.0.0.1:8554, security: Local, auth: +token``.
_GRPC_STARTED = re.compile(r"Started GRPC server at (?P<host>[^\s,]+):(?P<port>\d+)")
#: Where, under its ``HOME``, the emulator writes its discovery file on macOS.
DISCOVERY_DIR = Path("Library/Caches/TemporaryItems/avd/running")
#: An IPv4 default route over the guest's Wi-Fi, in ``ip -4 route show table all``.
_WIFI_DEFAULT_ROUTE = re.compile(r"^default via \S+ dev wlan0\b", re.MULTILINE)


@dataclass(frozen=True)
class _NotYet:
    """What a wait saw this time, when it is not over yet."""

    state: str


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


def avd_api_level(avd_home: Path, avd: str, sdk: Path) -> int | None:
    """The API level *avd* runs, from the system image its configuration names.

    The AVD's ``config.ini`` names the image (``image.sysdir.1``, relative to
    the SDK), and the image's ``source.properties`` its level
    (``AndroidVersion.ApiLevel``). The AVD's directory is the ``path`` its
    ``<avd>.ini`` names, else ``<avd>.avd`` beside it. ``None`` when a file is
    missing or says nothing.
    """
    listing = _properties(avd_home / f"{avd}.ini")
    directory = Path(listing.get("path") or avd_home / f"{avd}.avd")
    image = _properties(directory / "config.ini").get("image.sysdir.1")
    if not image:
        return None
    level = _properties(sdk / image / "source.properties").get("AndroidVersion.ApiLevel", "")
    return int(level) if level.isdigit() else None


def _properties(path: Path) -> dict[str, str]:
    """The ``key=value`` lines of *path*; empty when there is no such file."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return {}
    pairs = (line.partition("=") for line in text.splitlines() if "=" in line)
    return {key.strip(): value.strip() for key, _, value in pairs}


def emulator_command(
    emulator: Path, avd: str, report_port: int, *, read_only: bool, grpc_port: int | None = None
) -> list[str]:
    """The emulator's command line for one headless session of *avd*.

    The emulator picks its console port and reports it to *report_port* on the
    loopback address. With *grpc_port*, it also serves gRPC there, behind a
    token.
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
    if grpc_port is not None:
        command += ["-grpc", str(grpc_port), "-grpc-use-token"]
    return command


def emulator_environment(
    base: Mapping[str, str],
    *,
    sdk: Path,
    adb_port: int,
    home: Path,
    avd_home: Path,
    tmpdir: Path,
) -> dict[str, str]:
    """The emulator's environment, built on *base*: the run's SDK, adb server, home, AVDs and tmp.

    *home* is the adb server's ``HOME``, whose ``.android`` holds the run's key.
    """
    env = {name: value for name, value in base.items() if name not in _NOT_INHERITED}
    existing = base.get("NO_PROXY") or base.get("no_proxy")
    no_proxy = ",".join([*LOOPBACK, existing] if existing else LOOPBACK)
    env.update(
        ANDROID_HOME=str(sdk),
        ANDROID_ADB_SERVER_PORT=str(adb_port),
        HOME=str(home),
        ANDROID_EMULATOR_HOME=str(home / ".android"),
        ANDROID_AVD_HOME=str(avd_home),
        TMPDIR=str(tmpdir),
        NO_PROXY=no_proxy,
        no_proxy=no_proxy,
        **NO_SCANS,
    )
    return env


@dataclass
class _ConsoleReport:
    """A loopback listener the emulator reports the console port it bound to (``-report-console``).

    A connection that sends anything but a port number is not the emulator's
    and is ignored; a report that never comes is the boot deadline's to name.
    """

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
                sent = await asyncio.wait_for(reader.read(), _REPORT_READ_S)
            except (TimeoutError, ConnectionError):
                return
            finally:
                writer.close()
            text = sent.decode(errors="replace").strip()
            if text.isascii() and text.isdigit() and not console_port.done():
                console_port.set_result(int(text))

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
        trace_safe(
            "an AVD's name on this machine, not a credential",
            description="The AVD to boot, by name, as `emulator -list-avds` prints it.",
        ),
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
                "writes the session's changes into the AVD, where the next session finds them; "
                "a run that needs a fresh device (AndroidWorld's setup) is refused then."
            )
        ),
    ] = True
    boot_timeout_s: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Seconds the device may take to boot (sys.boot_completed) and to meet what "
                "the run needs of it (its gRPC endpoint, adbd as root, the guest's Wi-Fi); a "
                "cold boot took about 18 s on an Apple M1 Pro and Wi-Fi a few seconds more."
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

    def unmet_needs(self, needs: DeviceNeeds) -> list[str]:
        """The gRPC endpoint off macOS, where the discovery file was not measured; ``fresh`` unless read-only."""
        unmet = []
        if needs.emulator_grpc and sys.platform != "darwin":
            unmet.append(
                "emulator_grpc: its token is in the discovery file the emulator writes, and "
                "where it writes that file was measured on macOS only"
            )
        if needs.fresh and not self.read_only:
            unmet.append(
                "fresh: read_only is false, so the run's changes are written into the AVD and "
                "become the next run's start"
            )
        return unmet

    def api_level(self) -> int | None:
        """The API level of the AVD's system image (:func:`avd_api_level`); ``None`` when it cannot be read."""
        try:
            sdk = find_sdk(self.sdk_root, os.environ)
        except FileNotFoundError:
            return None
        return avd_api_level(find_avd_home(self.avd_home, os.environ), self.avd, sdk)

    async def open(self, needs: DeviceNeeds | None = None) -> AndroidProviderSession:
        """Boot one device meeting *needs* and return where to reach it.

        Raises:
            FileNotFoundError: The SDK or the AVD cannot be found.
            UnmetDeviceNeeds: :meth:`unmet_needs` named a need it cannot meet.
            RuntimeError: The adb server or the emulator exited before the
                device was ready, or adbd refused to run as root; the message
                carries the end of the emulator's log.
            TimeoutError: The device did not boot and meet *needs* within
                ``boot_timeout_s``.
        """
        needs = needs or DeviceNeeds()
        unmet = self.unmet_needs(needs)
        if unmet:
            raise UnmetDeviceNeeds(
                f"the {self.provider_id!r} provider cannot meet what the run needs: "
                + "; ".join(unmet)
            )
        sdk = find_sdk(self.sdk_root, os.environ)
        avd_home = find_avd_home(self.avd_home, os.environ)
        if not (avd_home / f"{self.avd}.ini").is_file():
            found = sorted(path.stem for path in avd_home.glob("*.ini"))
            raise FileNotFoundError(
                f"no AVD named {self.avd!r} in {avd_home} (found: {', '.join(found) or 'none'})"
            )
        adb = sdk / "platform-tools" / "adb"
        session_id = uuid.uuid4().hex
        run = _Run(Path(tempfile.mkdtemp(prefix="spectragent-android-")))
        self._runs[session_id] = run
        report: _ConsoleReport | None = None
        try:
            (run.directory / "tmp").mkdir()
            run.server = server = await PrivateAdbServer.start(str(adb), run.directory)
            report = await _ConsoleReport.listen()
            run.emulator_group = await ProcessGroup.open()
            run.emulator = emulator = await run.emulator_group.spawn(
                emulator_command(
                    sdk / "emulator" / "emulator",
                    self.avd,
                    report.port,
                    read_only=self.read_only,
                    grpc_port=free_port() if needs.emulator_grpc else None,
                ),
                env=emulator_environment(
                    os.environ,
                    sdk=sdk,
                    adb_port=server.port,
                    home=server.home,
                    avd_home=avd_home,
                    tmpdir=run.directory / "tmp",
                ),
                log=_emulator_log(run),
            )
            client = AdbClient(HOST, server.port)
            deadline = asyncio.get_running_loop().time() + self.boot_timeout_s
            console_port = await self._wait_for_boot(run, client, report, deadline)
            serial = f"emulator-{console_port}"
            grpc = None
            if needs.emulator_grpc:
                grpc = await self._until(
                    run,
                    deadline,
                    "the emulator had not started its gRPC endpoint",
                    lambda: _grpc_endpoint(run, server.home, emulator.pid),
                )
            if needs.root:
                await self._become_root(run, client, serial, deadline)
            if needs.guest_wifi:
                await self._until(
                    run,
                    deadline,
                    f"{serial}'s Wi-Fi had no IPv4 default route",
                    lambda: _wifi_routed(client, serial),
                )
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
                port=server.port,
                kind="server",
                encrypted=False,
                needs_adb_key=False,
            ),
            serial=serial,
            emulator=EmulatorAccess(
                console_port=console_port, home=str(server.home), adb=str(adb), grpc=grpc
            ),
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
        self, run: _Run, client: AdbClient, report: _ConsoleReport, deadline: float
    ) -> int:
        """Learn the emulator's console port from its report, then poll ``sys.boot_completed`` until 1."""

        async def booted() -> int | _NotYet:
            if not report.console_port.done():
                return _NotYet("the emulator had not reported its console port")
            port = report.console_port.result()
            answer = await client.shell(f"emulator-{port}", "getprop sys.boot_completed")
            if answer.stdout.strip() == b"1":
                return port
            return _NotYet(
                f"sys.boot_completed was {answer.stdout.strip().decode(errors='replace')!r}"
            )

        return await self._until(run, deadline, "the emulator had not booted", booted)

    async def _become_root(
        self, run: _Run, client: AdbClient, serial: str, deadline: float
    ) -> None:
        """Restart adbd on *serial* as root, again each time its shell is still not root.

        Sent right after the boot completed, ``root:`` sometimes reached adbd
        while it was restarting on its own: the server answered ``FAIL
        closed``, or adbd closed the stream without a word (three boots in a
        row of a sweep, measured). So every attempt asks the shell first and
        sends ``root:`` only while it is not root; only a build that says it
        cannot run as root is refused.
        """

        async def rooted() -> bool | _NotYet:
            uid = (await client.shell(serial, "id -u")).stdout.strip().decode(errors="replace")
            if uid == "0":
                return True
            answer = await client.root(serial)
            if "cannot run as root" in answer:
                raise RuntimeError(f"{serial} refused to run adbd as root: {answer!r}")
            return _NotYet(f"its shell was uid {uid!r} and root: answered {answer!r}")

        await self._until(run, deadline, f"{serial} was not running adbd as root", rooted)

    async def _until(
        self,
        run: _Run,
        deadline: float,
        what: str,
        attempt: Callable[[], Awaitable[T | _NotYet]],
    ) -> T:
        """Call *attempt* every half second until it returns what it waits for, at most until *deadline*.

        An attempt that fails to reach the device (an ADB error, a refused
        connection, no answer within its own limit) only waits longer.

        Raises:
            RuntimeError: The emulator or the run's adb server exited first.
            TimeoutError: *deadline* passed; the message says *what* had not
                happened, what the last attempt saw, and ends with the
                emulator's log.
        """
        loop = asyncio.get_running_loop()
        while True:
            _check_running(run)
            try:
                async with asyncio.timeout(_BOOT_QUERY_TIMEOUT_S):
                    outcome = await attempt()
            except (AdbError, OSError, TimeoutError) as exc:
                outcome = _NotYet(f"{type(exc).__name__}: {exc}")
            if not isinstance(outcome, _NotYet):
                return outcome
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"{what} after {self.boot_timeout_s:g} s ({outcome.state}):\n"
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
        """Stop the emulator and its group, then the adb server, then remove the run directory."""
        if run.emulator_group is not None:
            await run.emulator_group.close(run.emulator, grace_s=self.shutdown_timeout_s)
        if run.server is not None:
            await run.server.stop()
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(run.directory)


def _emulator_log(run: _Run) -> Path:
    return run.directory / "emulator.log"


def _check_running(run: _Run) -> None:
    """Raise, with the end of its log, if the emulator or the run's adb server has exited."""
    if run.emulator is not None and run.emulator.returncode is not None:
        raise RuntimeError(
            f"the emulator exited with code {run.emulator.returncode} before the device was "
            f"ready:\n{log_tail(_emulator_log(run))}"
        )
    if run.server is not None and run.server.process.returncode is not None:
        raise RuntimeError(
            f"the run's adb server exited with code {run.server.process.returncode} while the "
            f"emulator was starting:\n{log_tail(run.server.log)}"
        )


async def _grpc_endpoint(run: _Run, home: Path, pid: int) -> EmulatorGrpc | _NotYet:
    """Where the emulator's gRPC endpoint listens, once its log and its discovery file say so."""
    started = _GRPC_STARTED.search(_emulator_log(run).read_text(errors="replace"))
    if started is None:
        return _NotYet("its log had no 'Started GRPC server at' line")
    discovery = home / DISCOVERY_DIR / f"pid_{pid}.ini"
    try:
        keys = {line.partition("=")[0].strip() for line in discovery.read_text().splitlines()}
    except FileNotFoundError:
        return _NotYet(f"there was no discovery file at {discovery}")
    if "grpc.token" not in keys:
        return _NotYet(f"its discovery file {discovery} had no grpc.token")
    return EmulatorGrpc(host=started["host"], port=int(started["port"]), token_file=str(discovery))


async def _wifi_routed(client: AdbClient, serial: str) -> bool | _NotYet:
    """Whether the guest's Wi-Fi has an IPv4 default route, in any routing table.

    When it has none yet, the ``wlan0`` routes it has are what a timeout reports.
    """
    routes = (await client.shell(serial, "ip -4 route show table all")).stdout.decode(
        errors="replace"
    )
    if _WIFI_DEFAULT_ROUTE.search(routes):
        return True
    wlan0 = [line.strip() for line in routes.splitlines() if " dev wlan0" in line]
    return _NotYet(
        "no routing table had a default route over wlan0; its wlan0 routes were "
        + ("; ".join(wlan0[-8:]) if wlan0 else "none")
    )

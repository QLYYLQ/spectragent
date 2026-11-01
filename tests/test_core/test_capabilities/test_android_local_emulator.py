"""The local emulator provider: the run's own adb server and key, a headless boot, a clean release.

A fake SDK stands in for the real one, so these run anywhere: its ``adb`` serves
``tests/_helpers/fake_adb.py`` on the port it is given (answering
``sys.boot_completed``, ``root:``, ``id -u`` and the routing tables, each
command recorded) and writes a key pair under ``$HOME/.android`` as adb does;
its ``emulator`` leaves a ``sleep`` of its own in its process group, as the real
one leaves ``netsimd``, reports a console port as ``-report-console`` asks,
records how it was started, starts "gRPC" as ``-grpc`` asks (its log line and
discovery file, as emulator 37.2.13 wrote them) and, when stopped, records
whether the run's adb server still answered. The real emulator is booted by
``test_android_emulator_live.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import stat
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from spectragent import REPO_ROOT
from spectragent.core.capabilities.android.backends.base import (
    AdbEndpoint,
    DeviceNeeds,
    EmulatorAccess,
    EmulatorGrpc,
    UnmetDeviceNeeds,
)
from spectragent.core.capabilities.android.backends.local_emulator import (
    DISCOVERY_DIR,
    LocalEmulatorProvider,
    _ConsoleReport,
    avd_api_level,
    emulator_command,
    emulator_environment,
    find_avd_home,
    find_sdk,
)
from tests._helpers.process_checks import gone, groups, soon

AVD = "test_avd"

#: ``adb -P <port> server nodaemon``: a fake ADB server on that port. Until the
#: emulator has registered (its ``registered`` marker), ``sys.boot_completed`` is
#: empty; then it is ``$FAKE_BOOT`` (``1`` by default). ``root:`` restarts adbd as
#: root, after which ``id -u`` says 0 (``FAKE_ROOT=refused``: a production build's
#: refusal; ``FAKE_ROOT=restarting``: the first ``root:`` gets ``FAIL closed`` and
#: the second no answer, as adbd restarting on its own after the boot answered
#: them). The routing tables have the guest Wi-Fi's default route unless
#: ``FAKE_WIFI=0``. Every command after the registration is appended to
#: ``commands.txt``.
FAKE_ADB = f"""#!{sys.executable}
import asyncio, json, os, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from tests._helpers.fake_adb import FakeAdbServer, ShellResult, scripted
home, record = os.environ["HOME"], os.environ["FAKE_RECORD"]
os.makedirs(os.path.join(home, ".android"), exist_ok=True)
for name in ("adbkey", "adbkey.pub"):
    with open(os.path.join(home, ".android", name), "w") as key:
        key.write("run key")
with open(os.path.join(record, "adb.json"), "w") as out:
    json.dump({{"pid": os.getpid(), "home": home}}, out)
booted = os.environ.get("FAKE_BOOT", "1").encode()
routes = b"default dev dummy0 table 1002 proto static scope link \\n"
routes += b"10.0.2.0/24 dev wlan0 proto kernel scope link src 10.0.2.16 \\n"
if os.environ.get("FAKE_WIFI", "1") == "1":
    routes += b"default via 10.0.2.2 dev wlan0 table 1016 proto static \\n"
rooted = False
restarting = [ShellResult(stderr=b"closed", exit_code=1), ShellResult()]

async def device(command):
    global rooted
    if not os.path.exists(os.path.join(record, "registered")):
        return ShellResult()
    with open(os.path.join(record, "commands.txt"), "a") as log:
        log.write(command + "\\n")
    if command == "root:":
        if os.environ.get("FAKE_ROOT") == "refused":
            return ShellResult(stdout=b"adbd cannot run as root in production builds\\n")
        if os.environ.get("FAKE_ROOT") == "restarting" and restarting:
            return restarting.pop(0)
        if rooted:
            return ShellResult(stdout=b"adbd is already running as root\\n")
        rooted = True
        return ShellResult(stdout=b"restarting adbd as root\\n")
    return await scripted({{
        "getprop sys.boot_completed": ShellResult(stdout=booted),
        "id -u": ShellResult(stdout=b"0\\n" if rooted else b"2000\\n"),
        "ip -4 route show table all": ShellResult(stdout=routes),
    }})(command)

port = int(sys.argv[sys.argv.index("-P") + 1])

async def serve():
    devices = {{f"emulator-{{p}}": device for p in range(5554, 5586, 2)}}
    async with FakeAdbServer(devices, port=port):
        await asyncio.Event().wait()

asyncio.run(serve())
"""

#: ``emulator ...``: starts a ``sleep 60`` that stays in its process group and
#: outlives it unless the group is swept, records its argv, environment, process
#: group and that sleep, reports console port 5554 where ``-report-console``
#: says, registers with the adb server (the marker the fake reads) and waits; on
#: SIGTERM it records whether the run's adb server still answered, then exits.
#: ``FAKE_EMULATOR_MODE=crash`` prints a line and exits with code 7 once the
#: sleep is running, instead of reporting anything. Given ``-grpc <port>``, it
#: prints the line emulator 37.2.13 prints when its gRPC listens and writes its
#: discovery file under ``$HOME``, with a token (``FAKE_GRPC=silent``: neither;
#: ``FAKE_GRPC=ipv6``: it listens on ``[::1]``, as it does when the port is taken
#: on IPv4).
FAKE_EMULATOR = f"""#!{sys.executable}
import json, os, signal, socket, subprocess, sys
record = os.environ["FAKE_RECORD"]
leftover = subprocess.Popen(["sleep", "60"])
with open(os.path.join(record, "emulator.json"), "w") as out:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid(),
               "group": os.getpgid(0), "leftover": leftover.pid}}, out)
print("emulator: could not open the AVD", flush=True)
if os.environ.get("FAKE_EMULATOR_MODE") == "crash":
    sys.exit(7)
if "-grpc" in sys.argv and os.environ.get("FAKE_GRPC") != "silent":
    port = sys.argv[sys.argv.index("-grpc") + 1]
    host = "[::1]" if os.environ.get("FAKE_GRPC") == "ipv6" else "127.0.0.1"
    print(f"INFO         | Started GRPC server at {{host}}:{{port}}, security: Local, "
          "auth: +token", flush=True)
    running = os.path.join(os.environ["HOME"], "Library/Caches/TemporaryItems/avd/running")
    os.makedirs(running, exist_ok=True)
    with open(os.path.join(running, f"pid_{{os.getpid()}}.ini"), "w") as found:
        found.write(f"avd.name=test_avd\\ngrpc.token=run-token\\ngrpc.port={{port}}\\n")

def stopped(*_):
    with socket.socket() as probe:
        probe.settimeout(2)
        answered = probe.connect_ex(("127.0.0.1", int(os.environ["ANDROID_ADB_SERVER_PORT"]))) == 0
    with open(os.path.join(record, "stopped.json"), "w") as out:
        json.dump({{"server_answered": answered}}, out)
    sys.exit(0)

signal.signal(signal.SIGTERM, stopped)
report = sys.argv[sys.argv.index("-report-console") + 1]
with socket.create_connection(("127.0.0.1", int(report.removeprefix("tcp:")))) as console:
    console.sendall(b"5554")
open(os.path.join(record, "registered"), "w").close()
while True:
    signal.pause()
"""


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the fake processes record themselves; a private temp dir for the run directories."""
    directory = tmp_path / "record"
    directory.mkdir()
    monkeypatch.setenv("FAKE_RECORD", str(directory))
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(runs))
    return directory


@pytest.fixture
def sdk(tmp_path: Path) -> Path:
    """The fake SDK, with one AVD beside it in ``<tmp>/avd``."""
    sdk = tmp_path / "sdk"
    _executable(sdk / "platform-tools" / "adb", FAKE_ADB)
    _executable(sdk / "emulator" / "emulator", FAKE_EMULATOR)
    avds = tmp_path / "avd"
    avds.mkdir()
    (avds / f"{AVD}.ini").write_text("path=/nowhere\n")
    return sdk


@pytest.fixture
async def provider(sdk: Path, tmp_path: Path) -> AsyncIterator[LocalEmulatorProvider]:
    """A provider over the fake SDK; whatever a test leaves open is closed after it."""
    provider = LocalEmulatorProvider(
        avd=AVD, sdk_root=str(sdk), avd_home=str(tmp_path / "avd"), boot_timeout_s=10
    )
    yield provider
    await provider.close()


def _recorded(record: Path, name: str) -> dict[str, object]:
    return json.loads((record / name).read_text())


async def test_a_session_boots_through_the_runs_own_server_and_close_releases_everything(
    provider: LocalEmulatorProvider, record: Path, tmp_path: Path, sdk: Path
) -> None:
    session = await provider.open()
    try:
        emulator = _recorded(record, "emulator.json")
        argv, env = emulator["argv"], emulator["env"]
        assert isinstance(argv, list) and isinstance(env, dict)
        assert session.serial == "emulator-5554", "the console port the emulator reported"
        assert session.endpoint == AdbEndpoint(
            host="127.0.0.1",
            port=session.endpoint.port,
            kind="server",
            encrypted=False,
            needs_adb_key=False,
        )
        # The emulator is pointed at the run's server and SDK, shares its home and is
        # handed the key it wrote.
        assert env["ANDROID_ADB_SERVER_PORT"] == str(session.endpoint.port)
        adb_home = _recorded(record, "adb.json")["home"]
        assert env["HOME"] == adb_home
        assert env["ANDROID_EMULATOR_HOME"] == f"{adb_home}/.android"
        assert Path(env["ANDROID_EMULATOR_HOME"], "adbkey.pub").read_text() == "run key"
        assert env["ANDROID_AVD_HOME"] == str(tmp_path / "avd")
        assert env["ANDROID_HOME"] == str(sdk)
        assert env["TMPDIR"] == f"{adb_home}/tmp" and Path(env["TMPDIR"]).is_dir()
        assert {"-read-only", "-no-snapshot-save", "-no-window"} <= set(argv)
        assert "-port" not in argv, "the emulator binds a free console port itself"
    finally:
        await provider.close(session)

    assert _recorded(record, "stopped.json") == {"server_answered": True}, (
        "the emulator is stopped while the run's adb server is still up"
    )
    assert gone(emulator["pid"]) and gone(_recorded(record, "adb.json")["pid"])
    assert await soon(lambda: gone(emulator["leftover"])), "the group is swept, not only stopped"
    assert await soon(lambda: emulator["group"] not in groups()), "nothing of the group is left"
    assert list((tmp_path / "runs").iterdir()) == [], "the run directory is removed"


async def test_an_emulator_that_exits_before_booting_is_reported_and_nothing_is_left(
    provider: LocalEmulatorProvider,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_EMULATOR_MODE", "crash")

    with pytest.raises(RuntimeError, match="exited with code 7") as failed:
        await provider.open()

    assert "could not open the AVD" in str(failed.value), "the end of the emulator's log"
    assert gone(_recorded(record, "adb.json")["pid"])
    # The emulator was gone before the release; the watcher still held its group.
    emulator = _recorded(record, "emulator.json")
    assert await soon(lambda: gone(emulator["leftover"])), "what it left in its group is swept"
    assert list((tmp_path / "runs").iterdir()) == []


async def test_a_device_that_does_not_boot_in_time_is_released(
    provider: LocalEmulatorProvider,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_BOOT", "0")
    provider.boot_timeout_s = 1.0

    with pytest.raises(TimeoutError, match="had not booted after 1 s .sys.boot_completed was '0'"):
        await provider.open()

    assert gone(_recorded(record, "emulator.json")["pid"])
    assert gone(_recorded(record, "adb.json")["pid"])
    assert list((tmp_path / "runs").iterdir()) == []


async def test_a_boot_cancelled_halfway_releases_everything_before_the_cancellation_ends(
    provider: LocalEmulatorProvider,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_BOOT", "0")
    opening = asyncio.ensure_future(provider.open())
    started = record / "emulator.json"
    for _ in range(200):  # until the emulator has started, at most 10 s
        if started.is_file() or opening.done():
            break
        await asyncio.sleep(0.05)
    assert started.is_file() and not opening.done()

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    assert gone(_recorded(record, "emulator.json")["pid"])
    assert gone(_recorded(record, "adb.json")["pid"])
    assert list((tmp_path / "runs").iterdir()) == []


#: A run in a process of its own: opens a session over the fake SDK, prints the
#: process groups of its emulator and adb server, and waits to be killed.
_KILLED_RUN = """
import asyncio, json, sys
from spectragent.core.capabilities.android.backends.local_emulator import LocalEmulatorProvider

async def main():
    avd, sdk, avd_home = sys.argv[1:]
    provider = LocalEmulatorProvider(avd=avd, sdk_root=sdk, avd_home=avd_home, boot_timeout_s=10)
    session = await provider.open()
    run = provider._runs[session.session_id]
    print(json.dumps([run.emulator_group.id, run.server.group.id]), flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
"""


async def test_a_run_killed_outright_takes_its_emulator_and_adb_server_with_it(
    sdk: Path, record: Path, tmp_path: Path
) -> None:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _KILLED_RUN,
        AVD,
        str(sdk),
        str(tmp_path / "avd"),
        stdout=asyncio.subprocess.PIPE,
        env={**os.environ, "TMPDIR": str(tmp_path / "runs")},
    )
    assert child.stdout is not None
    run_groups: set[int] = set()
    try:
        run_groups = set(json.loads(await asyncio.wait_for(child.stdout.readline(), 30)))
        assert run_groups <= groups(), "both programs are running"
        child.kill()  # this test's own child: no teardown runs in it
        await child.wait()

        assert await soon(lambda: not run_groups & groups()), (
            "the emulator's and the adb server's groups are gone"
        )
        assert gone(_recorded(record, "emulator.json")["pid"])
        assert gone(_recorded(record, "adb.json")["pid"])
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        for group in run_groups & groups():  # only when the watchers failed to
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)


async def test_a_missing_avd_is_named_with_the_avds_that_are_there(
    provider: LocalEmulatorProvider, record: Path
) -> None:
    provider.avd = "no_such_avd"

    with pytest.raises(
        FileNotFoundError, match=r"no AVD named 'no_such_avd' .* \(found: test_avd\)"
    ):
        await provider.open()
    assert list(record.iterdir()) == [], "nothing was started"


def _commands(record: Path) -> list[str]:
    """The commands the fake adb server answered after the emulator registered, in order."""
    log = record / "commands.txt"
    return log.read_text().splitlines() if log.is_file() else []


#: Every need a run's strategies can ask of the device.
EVERY_NEED = DeviceNeeds(emulator_grpc=True, root=True, guest_wifi=True)
macos_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="the emulator's gRPC discovery file was measured on macOS"
)


@macos_only
async def test_what_the_run_needs_is_met_before_the_session_is_handed_over(
    provider: LocalEmulatorProvider, record: Path, sdk: Path
) -> None:
    session = await provider.open(EVERY_NEED)
    try:
        emulator = _recorded(record, "emulator.json")
        argv = emulator["argv"]
        assert isinstance(argv, list)
        grpc_port = int(argv[argv.index("-grpc") + 1])
        assert argv[argv.index("-grpc") + 2] == "-grpc-use-token"
        home = Path(str(_recorded(record, "adb.json")["home"]))
        assert session.emulator == EmulatorAccess(
            console_port=5554,
            home=str(home),
            adb=str(sdk / "platform-tools" / "adb"),
            grpc=EmulatorGrpc(
                host="127.0.0.1",
                port=grpc_port,
                token_file=str(home / DISCOVERY_DIR / f"pid_{emulator['pid']}.ini"),
            ),
        )
        assert "run-token" not in repr(session), "the token stays in the emulator's file"
        commands = _commands(record)
        assert commands[commands.index("root:") - 1 : commands.index("root:") + 2] == [
            "id -u",
            "root:",
            "id -u",
        ], "root: only while the shell is not root, then the shell again"
        assert commands[-1] == "ip -4 route show table all", "the Wi-Fi wait ends the boot"
    finally:
        await provider.close(session)


async def test_a_run_that_needs_nothing_boots_without_grpc_or_root(
    provider: LocalEmulatorProvider, record: Path
) -> None:
    session = await provider.open()
    try:
        argv = _recorded(record, "emulator.json")["argv"]
        assert isinstance(argv, list) and "-grpc" not in argv
        assert session.emulator is not None and session.emulator.grpc is None
        assert session.emulator.console_port == 5554
        assert _commands(record) == ["getprop sys.boot_completed"]
    finally:
        await provider.close(session)


async def test_adbd_restarting_on_its_own_after_the_boot_is_asked_again(
    provider: LocalEmulatorProvider, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ROOT", "restarting")

    session = await provider.open(DeviceNeeds(root=True))
    await provider.close(session)

    assert _commands(record).count("root:") == 3, "FAIL closed, no answer, then restarting"


async def test_adbd_that_refuses_root_fails_the_boot_and_nothing_is_left(
    provider: LocalEmulatorProvider,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_ROOT", "refused")

    with pytest.raises(RuntimeError, match="refused to run adbd as root: 'adbd cannot run"):
        await provider.open(DeviceNeeds(root=True))

    assert gone(_recorded(record, "emulator.json")["pid"])
    assert gone(_recorded(record, "adb.json")["pid"])
    assert list((tmp_path / "runs").iterdir()) == []


async def test_wifi_that_never_routes_fails_the_boot_in_time_and_nothing_is_left(
    provider: LocalEmulatorProvider,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_WIFI", "0")
    provider.boot_timeout_s = 1.5

    with pytest.raises(
        TimeoutError,
        match=r"emulator-5554's Wi-Fi had no IPv4 default route after 1.5 s \(no routing table "
        r"had a default route over wlan0; its wlan0 routes were 10\.0\.2\.0/24 dev wlan0 proto "
        r"kernel scope link src 10\.0\.2\.16\)",
    ):
        await provider.open(DeviceNeeds(guest_wifi=True))

    assert gone(_recorded(record, "emulator.json")["pid"])
    assert list((tmp_path / "runs").iterdir()) == []


@macos_only
async def test_grpc_that_never_starts_fails_the_boot_in_time(
    provider: LocalEmulatorProvider, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GRPC", "silent")
    provider.boot_timeout_s = 1.5

    with pytest.raises(TimeoutError, match="had no 'Started GRPC server at' line"):
        await provider.open(DeviceNeeds(emulator_grpc=True))

    assert gone(_recorded(record, "emulator.json")["pid"])


@macos_only
async def test_grpc_on_ipv6_alone_is_reached_where_the_emulator_said(
    provider: LocalEmulatorProvider, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the port taken on IPv4 the emulator listens on [::1]; the session reports that."""
    monkeypatch.setenv("FAKE_GRPC", "ipv6")

    session = await provider.open(DeviceNeeds(emulator_grpc=True))
    await provider.close(session)

    assert session.emulator is not None and session.emulator.grpc is not None
    assert session.emulator.grpc.host == "[::1]"


async def test_the_grpc_endpoint_is_refused_where_its_discovery_file_was_not_measured(
    provider: LocalEmulatorProvider, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert provider.unmet_needs(DeviceNeeds(root=True, guest_wifi=True)) == []
    with pytest.raises(UnmetDeviceNeeds, match="emulator_grpc: .*measured on macOS only"):
        await provider.open(DeviceNeeds(emulator_grpc=True))
    assert list(record.iterdir()) == [], "nothing was started"


async def test_a_fresh_device_is_refused_unless_the_avd_boots_read_only(
    provider: LocalEmulatorProvider, record: Path
) -> None:
    assert provider.unmet_needs(DeviceNeeds(fresh=True)) == [], "read-only by default"
    provider.read_only = False

    with pytest.raises(
        UnmetDeviceNeeds,
        match="'local_emulator' provider cannot meet what the run needs: fresh: read_only is "
        "false, so the run's changes are written into the AVD and become the next run's start",
    ):
        await provider.open(DeviceNeeds(fresh=True))
    assert list(record.iterdir()) == [], "nothing was started"


def test_the_emulator_serves_grpc_behind_a_token_when_asked() -> None:
    command = emulator_command(Path("/e"), "pixel", 40999, read_only=True, grpc_port=41000)
    assert command[-4:] == ["-read-only", "-grpc", "41000", "-grpc-use-token"]


def test_the_emulator_runs_headless_reports_its_console_port_and_saves_no_snapshot() -> None:
    assert emulator_command(Path("/sdk/emulator/emulator"), "pixel", 40999, read_only=False) == [
        "/sdk/emulator/emulator",
        "-avd",
        "pixel",
        "-report-console",
        "tcp:40999",
        "-no-window",
        "-no-audio",
        "-no-boot-anim",
        "-no-snapshot-save",
        "-gpu",
        "swiftshader_indirect",
    ]


def test_the_emulator_environment_names_the_runs_sdk_server_home_avds_and_tmp() -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/Users/someone",
        "ANDROID_HOME": "/Users/someone/other-sdk",
        "ANDROID_SDK_ROOT": "/Users/someone/other-sdk",
        "ANDROID_USER_HOME": "/Users/someone/.android",
        "ANDROID_SDK_HOME": "/Users/someone",
        "ANDROID_ADB_SERVER_ADDRESS": "elsewhere",
        "ANDROID_EMULATOR_DISCOVERY_DIR": "/Users/someone/shared",
        "ANDROID_TMP": "/tmp/shared",
        "ADB_SERVER_SOCKET": "tcp:localhost:5037",
        "ADB_USB": "1",
        "TMPDIR": "/var/folders/xy/T/",
        "NO_PROXY": "intranet.example",
        "no_grpc_proxy": "intranet.example",
    }

    env = emulator_environment(
        base,
        sdk=Path("/sdk"),
        adb_port=40123,
        home=Path("/runs/r1"),
        avd_home=Path("/avds"),
        tmpdir=Path("/runs/r1/tmp"),
    )

    assert env == {
        "PATH": "/usr/bin",
        "ANDROID_HOME": "/sdk",
        "ANDROID_ADB_SERVER_PORT": "40123",
        "HOME": "/runs/r1",
        "ANDROID_EMULATOR_HOME": "/runs/r1/.android",
        "ANDROID_AVD_HOME": "/avds",
        "TMPDIR": "/runs/r1/tmp",
        "NO_PROXY": "localhost,127.0.0.1,::1,intranet.example",
        "no_proxy": "localhost,127.0.0.1,::1,intranet.example",
        "ADB_EMU": "0",
        "ADB_USB": "0",
        "ADB_MDNS": "0",
    }


async def test_the_console_port_is_taken_from_the_first_report_that_is_a_port_number() -> None:
    """A stray local connection, or digits that are not ASCII, neither fail nor stall the boot."""
    report = await _ConsoleReport.listen()
    try:
        for sent in (
            b"",
            "\u0665\u0665\u0665\u0664".encode(),
            b"\xff\xfe",
            b"not a port",
            b"5556\n",
        ):
            _, writer = await asyncio.open_connection("127.0.0.1", report.port)
            writer.write(sent)
            writer.close()
            await writer.wait_closed()
        assert await asyncio.wait_for(report.console_port, 5) == 5556
    finally:
        report.close()


def test_the_sdk_is_the_configured_one_else_the_first_named_by_the_environment(
    sdk: Path, tmp_path: Path
) -> None:
    assert find_sdk(None, {"ANDROID_HOME": str(sdk)}) == sdk
    with pytest.raises(FileNotFoundError, match=f"at {tmp_path / 'elsewhere'}; set"):
        find_sdk(str(tmp_path / "elsewhere"), {"ANDROID_HOME": str(sdk)})


def test_the_avds_are_where_the_emulator_itself_would_look() -> None:
    assert find_avd_home("/configured", {"ANDROID_AVD_HOME": "/avd"}) == Path("/configured")
    assert find_avd_home(None, {"ANDROID_AVD_HOME": "/avd", "ANDROID_USER_HOME": "/u"}) == Path(
        "/avd"
    )
    assert find_avd_home(None, {"ANDROID_USER_HOME": "/u"}) == Path("/u/avd")
    assert find_avd_home(None, {"ANDROID_SDK_HOME": "/s"}) == Path("/s/.android/avd")
    assert find_avd_home(None, {}) == Path.home() / ".android" / "avd"


def _avd_with_image(tmp_path: Path, sdk: Path, level: str | None) -> None:
    """The fixture's AVD, its configuration naming a system image whose properties say *level*."""
    avd = tmp_path / "avd" / f"{AVD}.avd"
    avd.mkdir()
    (tmp_path / "avd" / f"{AVD}.ini").write_text(
        f"avd.ini.encoding=UTF-8\npath={avd}\ntarget=android-30\n"
    )
    image = "system-images/android-30/google_apis/arm64-v8a/"
    (avd / "config.ini").write_text(f"hw.lcd.width=1080\nimage.sysdir.1={image}\n")
    properties = sdk / image / "source.properties"
    properties.parent.mkdir(parents=True)
    lines = ["Pkg.Desc=System Image arm64-v8a with Google APIs.", "SystemImage.Abi=arm64-v8a"]
    if level is not None:
        lines.append(f"AndroidVersion.ApiLevel={level}")
    properties.write_text("\n".join(lines) + "\n")


def test_the_api_level_is_the_one_the_avds_system_image_declares(
    sdk: Path, tmp_path: Path, provider: LocalEmulatorProvider
) -> None:
    """As ``spectragent_service_api30``'s files say it (2026-11-02), read before anything boots."""
    _avd_with_image(tmp_path, sdk, "30")

    assert provider.api_level() == 30
    assert avd_api_level(tmp_path / "avd", AVD, sdk) == 30


def test_an_api_level_nothing_declares_is_unknown(
    sdk: Path, tmp_path: Path, provider: LocalEmulatorProvider
) -> None:
    assert provider.api_level() is None, "the fixture's AVD names no directory there is"
    _avd_with_image(tmp_path, sdk, None)
    assert provider.api_level() is None, "an image whose properties say no level"
    assert avd_api_level(tmp_path / "avd", "another_avd", sdk) is None

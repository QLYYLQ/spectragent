"""The local emulator provider: the run's own adb server and key, a headless boot, a clean release.

A fake SDK stands in for the real one, so these run anywhere: its ``adb`` serves
``tests/_helpers/fake_adb.py`` on the port it is given (answering
``sys.boot_completed``) and writes a key pair under ``$HOME/.android`` as adb
does; its ``emulator`` leaves a ``sleep`` of its own in its process group, as
the real one leaves ``netsimd``, reports a console port as ``-report-console``
asks, records how it was started and, when stopped, whether the run's adb
server still answered. The real emulator is booted by
``test_android_emulator_live.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from spectragent import REPO_ROOT
from spectragent.core.capabilities.android.backends.base import AdbEndpoint
from spectragent.core.capabilities.android.backends.local_emulator import (
    LocalEmulatorProvider,
    _ConsoleReport,
    emulator_command,
    emulator_environment,
    find_avd_home,
    find_sdk,
)

AVD = "test_avd"

#: ``adb -P <port> server nodaemon``: a fake ADB server on that port. Until the
#: emulator has registered (its ``registered`` marker), ``sys.boot_completed`` is
#: empty; then it is ``$FAKE_BOOT`` (``1`` by default).
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

async def device(command):
    if os.path.exists(os.path.join(record, "registered")):
        return await scripted({{"getprop sys.boot_completed": ShellResult(stdout=booted)}})(command)
    return ShellResult()

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
#: sleep is running, instead of reporting anything.
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


def _gone(pid: object) -> bool:
    assert isinstance(pid, int)
    try:
        os.kill(pid, 0)  # signal 0 delivers nothing; it only asks whether the PID exists
    except ProcessLookupError:
        return True
    return False


def _groups() -> set[int]:
    """The process group of every process on the machine."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pgid="], capture_output=True, text=True, check=True
    )
    return {int(group) for group in listing.stdout.split()}


async def _soon(condition: Callable[[], bool]) -> bool:
    """Whether *condition* holds within 5 s: a killed orphan is reaped by init, not at once."""
    for _ in range(100):
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


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
    assert _gone(emulator["pid"]) and _gone(_recorded(record, "adb.json")["pid"])
    assert await _soon(lambda: _gone(emulator["leftover"])), "the group is swept, not only stopped"
    assert await _soon(lambda: emulator["group"] not in _groups()), "nothing of the group is left"
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
    assert _gone(_recorded(record, "adb.json")["pid"])
    # The emulator was gone before the release; the watcher still held its group.
    emulator = _recorded(record, "emulator.json")
    assert await _soon(lambda: _gone(emulator["leftover"])), "what it left in its group is swept"
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

    assert _gone(_recorded(record, "emulator.json")["pid"])
    assert _gone(_recorded(record, "adb.json")["pid"])
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

    assert _gone(_recorded(record, "emulator.json")["pid"])
    assert _gone(_recorded(record, "adb.json")["pid"])
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
    groups: set[int] = set()
    try:
        groups = set(json.loads(await asyncio.wait_for(child.stdout.readline(), 30)))
        assert groups <= _groups(), "both programs are running"
        child.kill()  # this test's own child: no teardown runs in it
        await child.wait()

        assert await _soon(lambda: not groups & _groups()), (
            "the emulator's and the adb server's groups are gone"
        )
        assert _gone(_recorded(record, "emulator.json")["pid"])
        assert _gone(_recorded(record, "adb.json")["pid"])
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        for group in groups & _groups():  # only when the watchers failed to
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

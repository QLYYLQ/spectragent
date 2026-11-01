"""The local emulator provider: the run's own adb server and key, a headless boot, a clean release.

A fake SDK stands in for the real one, so these run anywhere: its ``adb`` serves
``tests/_helpers/fake_adb.py`` on the port it is given (answering
``sys.boot_completed``) and writes a key pair under ``$HOME/.android`` as adb
does; its ``emulator`` records how it was started and waits to be stopped. The
real emulator is booted by ``test_android_emulator_live.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from spectragent import REPO_ROOT
from spectragent.core.capabilities.android.backends.base import AdbEndpoint
from spectragent.core.capabilities.android.backends.local_emulator import (
    LocalEmulatorProvider,
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

#: ``emulator ...``: records its argv and environment, registers with the adb
#: server (the marker the fake reads) and waits to be stopped;
#: ``FAKE_EMULATOR_MODE=crash`` prints a line and exits with code 7 instead.
FAKE_EMULATOR = f"""#!{sys.executable}
import json, os, signal, sys
record = os.environ["FAKE_RECORD"]
with open(os.path.join(record, "emulator.json"), "w") as out:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid()}}, out)
print("emulator: could not open the AVD", flush=True)
if os.environ.get("FAKE_EMULATOR_MODE") == "crash":
    sys.exit(7)
open(os.path.join(record, "registered"), "w").close()
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
async def provider(tmp_path: Path) -> AsyncIterator[LocalEmulatorProvider]:
    """A provider over the fake SDK; whatever a test leaves open is closed after it."""
    sdk = tmp_path / "sdk"
    _executable(sdk / "platform-tools" / "adb", FAKE_ADB)
    _executable(sdk / "emulator" / "emulator", FAKE_EMULATOR)
    avds = tmp_path / "avd"
    avds.mkdir()
    (avds / f"{AVD}.ini").write_text("path=/nowhere\n")
    provider = LocalEmulatorProvider(
        avd=AVD, sdk_root=str(sdk), avd_home=str(avds), boot_timeout_s=10
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


async def test_a_session_boots_through_the_runs_own_server_and_close_releases_everything(
    provider: LocalEmulatorProvider, record: Path, tmp_path: Path
) -> None:
    session = await provider.open()
    try:
        emulator = _recorded(record, "emulator.json")
        argv, env = emulator["argv"], emulator["env"]
        assert isinstance(argv, list) and isinstance(env, dict)
        console_port = argv[argv.index("-port") + 1]
        assert session.serial == f"emulator-{console_port}"
        assert session.endpoint == AdbEndpoint(
            host="127.0.0.1",
            port=session.endpoint.port,
            kind="server",
            encrypted=False,
            needs_adb_key=False,
        )
        # The emulator is pointed at the run's server and handed the key it wrote.
        assert env["ANDROID_ADB_SERVER_PORT"] == str(session.endpoint.port)
        adb_home = _recorded(record, "adb.json")["home"]
        assert env["ANDROID_EMULATOR_HOME"] == f"{adb_home}/.android"
        assert Path(env["ANDROID_EMULATOR_HOME"], "adbkey.pub").read_text() == "run key"
        assert env["ANDROID_AVD_HOME"] == str(tmp_path / "avd")
        assert {"-read-only", "-no-snapshot-save", "-no-window"} <= set(argv)
    finally:
        await provider.close(session)

    assert _gone(_recorded(record, "emulator.json")["pid"])
    assert _gone(_recorded(record, "adb.json")["pid"])
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


async def test_a_missing_avd_is_named_with_the_avds_that_are_there(
    provider: LocalEmulatorProvider, record: Path
) -> None:
    provider.avd = "no_such_avd"

    with pytest.raises(
        FileNotFoundError, match=r"no AVD named 'no_such_avd' .* \(found: test_avd\)"
    ):
        await provider.open()
    assert list(record.iterdir()) == [], "nothing was started"


def test_the_emulator_runs_headless_on_the_console_port_and_saves_no_snapshot() -> None:
    assert emulator_command(Path("/sdk/emulator/emulator"), "pixel", 5556, read_only=False) == [
        "/sdk/emulator/emulator",
        "-avd",
        "pixel",
        "-port",
        "5556",
        "-no-window",
        "-no-audio",
        "-no-boot-anim",
        "-no-snapshot-save",
        "-gpu",
        "swiftshader_indirect",
    ]


def test_the_emulator_environment_names_the_runs_server_home_and_avds_and_skips_the_proxy() -> None:
    base = {
        "PATH": "/usr/bin",
        "ANDROID_USER_HOME": "/Users/someone/.android",
        "ANDROID_SDK_HOME": "/Users/someone",
        "ADB_SERVER_SOCKET": "tcp:localhost:5037",
        "NO_PROXY": "intranet.example",
    }

    env = emulator_environment(
        base, adb_port=40123, emulator_home=Path("/runs/r1/.android"), avd_home=Path("/avds")
    )

    assert env == {
        "PATH": "/usr/bin",
        "ANDROID_ADB_SERVER_PORT": "40123",
        "ANDROID_EMULATOR_HOME": "/runs/r1/.android",
        "ANDROID_AVD_HOME": "/avds",
        "NO_PROXY": "localhost,127.0.0.1,::1,intranet.example",
        "no_proxy": "localhost,127.0.0.1,::1,intranet.example",
    }


def test_the_sdk_is_the_configured_one_else_the_first_named_by_the_environment(
    provider: LocalEmulatorProvider, tmp_path: Path
) -> None:
    sdk = tmp_path / "sdk"

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

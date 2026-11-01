"""A run's own adb server: its own HOME and key, no scans, stopped through its own handle.

The lifecycle runs a stand-in ``adb`` executable (a few lines of Python) so it
needs no SDK; the real server is started by the emulator test module
(``test_android_emulator_live.py``).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from spectragent.core.capabilities.android.adb_server import (
    PrivateAdbServer,
    server_environment,
)

#: A stand-in for ``adb -P <port> server nodaemon``. It records how it was
#: started, writes a key pair under ``$HOME/.android`` as adb does, and serves
#: the port until it is stopped. ``FAKE_ADB_MODE=exit`` exits at once with
#: code 3; ``FAKE_ADB_MODE=deaf`` writes its key and never listens.
FAKE_ADB = f"""#!{sys.executable}
import json, os, signal, socket, sys
home = os.environ["HOME"]
os.makedirs(os.path.join(home, ".android"), exist_ok=True)
with open(os.path.join(home, "started.json"), "w") as out:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid()}}, out)
mode = os.environ.get("FAKE_ADB_MODE", "serve")
if mode == "exit":
    sys.exit(3)
for name in ("adbkey", "adbkey.pub"):
    with open(os.path.join(home, ".android", name), "w") as key:
        key.write("key")
if mode == "deaf":
    signal.pause()
listener = socket.socket()
listener.bind(("127.0.0.1", int(sys.argv[sys.argv.index("-P") + 1])))
listener.listen()
while True:
    listener.accept()[0].close()
"""


@pytest.fixture
def adb(tmp_path: Path) -> str:
    path = tmp_path / "bin" / "adb"
    path.parent.mkdir()
    path.write_text(FAKE_ADB)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _started(home: Path) -> dict[str, object]:
    return json.loads((home / "started.json").read_text())


def test_the_server_environment_moves_home_turns_the_scans_off_and_drops_foreign_keys() -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/Users/someone",
        "ADB_VENDOR_KEYS": "/Users/someone/.android/other.key",
        "ADB_SERVER_SOCKET": "tcp:localhost:5037",
        "ANDROID_ADB_SERVER_ADDRESS": "elsewhere",
        "ANDROID_ADB_SERVER_PORT": "5037",
    }

    env = server_environment(Path("/runs/r1"), base)

    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/runs/r1",
        "ADB_EMU": "0",
        "ADB_USB": "0",
        "ADB_MDNS": "0",
    }


async def test_a_started_server_listens_on_its_port_and_stops_when_asked(
    adb: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "run"
    home.mkdir()
    monkeypatch.setenv("ADB_VENDOR_KEYS", "/elsewhere/key")

    server = await PrivateAdbServer.start(adb, home)
    try:
        started = _started(home)
        assert started["argv"] == ["-P", str(server.port), "server", "nodaemon"]
        env = started["env"]
        assert isinstance(env, dict)
        assert env["HOME"] == str(home) and "ADB_VENDOR_KEYS" not in env
        assert server.public_key.read_text() == "key"
    finally:
        await server.stop()

    assert server.process.returncode is not None


async def test_a_server_that_exits_before_listening_is_reported_with_its_exit_code(
    adb: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "exit")

    with pytest.raises(RuntimeError, match="exited with code 3"):
        await PrivateAdbServer.start(adb, tmp_path)


async def test_a_server_that_never_gets_ready_is_stopped(
    adb: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "deaf")

    with pytest.raises(TimeoutError, match="not ready within 0.5 s"):
        await PrivateAdbServer.start(adb, tmp_path, ready_timeout_s=0.5)

    pid = _started(tmp_path)["pid"]
    assert isinstance(pid, int)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # signal 0 delivers nothing; it only asks whether the PID exists

"""A provider only adopts and releases the Chrome resources it actually created."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from spectragent.core.capabilities.browser.backends import local_chrome
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient


class Process:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.kill_error: Exception | None = None
        self.wait_gate: asyncio.Event | None = None

    def send_signal(self, signum: int) -> None:
        if self.kill_error:
            raise self.kill_error
        self.signals.append(signum)
        if self.wait_gate is None:
            self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.wait_gate is not None:
            await self.wait_gate.wait()
            self.returncode = 0
        assert self.returncode is not None
        return self.returncode


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.process = Process()
        self.created: list[Path] = []
        self.commands: list[tuple[str, ...]] = []
        self.polled = 0
        self.probed = 0
        self.browser_pids: list[int] = [self.process.pid]
        self.poll_exit = False
        self.probe_error: Exception | None = None
        self.port = 9222
        self.spawn_error: Exception | None = None
        self.spawn_entered: asyncio.Event | None = None
        self.spawn_gate: asyncio.Event | None = None
        self.spawn_options: list[dict[str, Any]] = []
        #: The tether's reading end of each spawn, held as the child would hold it.
        self.chrome_ends: list[int] = []
        #: (process group, return code of its browser when it was killed). The
        #: PIDs here are made up: a real ``killpg`` would reach whatever group
        #: of this machine has that number.
        self.group_kills: list[tuple[int, int | None]] = []
        harness = self

        def profile(**_: Any) -> str:
            path = tmp_path / f"temporary-{len(self.created)}"
            path.mkdir()
            self.created.append(path)
            return str(path)

        async def spawn(*cmd: str, **options: Any) -> Process:
            self.commands.append(cmd)
            self.spawn_options.append(options)
            if isinstance(options.get("stdin"), int):
                self.chrome_ends.append(os.dup(options["stdin"]))
            if self.spawn_entered is not None:
                self.spawn_entered.set()
            if self.spawn_gate is not None:
                await self.spawn_gate.wait()
            if self.spawn_error:
                raise self.spawn_error
            self.port = int(
                next(
                    arg.split("=", 1)[1]
                    for arg in cmd
                    if arg.startswith("--remote-debugging-port=")
                )
            )
            return self.process

        class Response:
            status_code = 200

            def json(self) -> dict[str, str]:
                return {
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{harness.port}/devtools/browser/owned"
                }

        class Client:
            def __init__(self, **kwargs: Any) -> None:
                assert kwargs["trust_env"] is False

            async def __aenter__(self) -> Client:
                return self

            async def __aexit__(self, *_: Any) -> None:
                pass

            async def get(self, *_: Any, **__: Any) -> Response:
                harness.polled += 1
                if harness.poll_exit:
                    harness.process.returncode = 0
                return Response()

        class Probe:
            def __init__(self, _url: str, **kwargs: Any) -> None:
                assert kwargs["proxy"] is None

            async def __aenter__(self) -> Probe:
                harness.probed += 1
                return self

            async def __aexit__(self, *_: Any) -> None:
                pass

            async def send(self, method: str) -> dict[str, Any]:
                assert method == "SystemInfo.getProcessInfo"
                if harness.probe_error:
                    raise harness.probe_error
                return {
                    "processInfo": [{"type": "browser", "id": pid} for pid in harness.browser_pids]
                }

        def kill_group(group: int) -> None:
            self.group_kills.append((group, self.process.returncode))

        monkeypatch.setattr(local_chrome, "_kill_process_group", kill_group)
        monkeypatch.setattr(local_chrome.tempfile, "mkdtemp", profile)
        monkeypatch.setattr(local_chrome.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(local_chrome.httpx, "AsyncClient", Client)
        monkeypatch.setattr(local_chrome, "CDPClient", Probe)

    def provider(self, **kwargs: Any) -> LocalChromeProvider:
        return LocalChromeProvider(executable_path="/fake/chrome", **kwargs)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(monkeypatch, tmp_path)
    yield harness
    for end in harness.chrome_ends:
        os.close(end)


async def test_occupied_port_is_refused_before_starting_or_connecting(harness: Harness) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match=f"remote_debugging_port={port} is unavailable"):
            await harness.provider(remote_debugging_port=port).open()
        assert listener.getsockname()[1] == port
    assert harness.commands == []
    assert harness.created == []
    assert harness.polled == 0


@pytest.mark.parametrize("during_poll", [False, True])
async def test_exited_process_never_adopts_a_responding_old_endpoint(
    harness: Harness,
    during_poll: bool,
) -> None:
    if during_poll:
        harness.poll_exit = True
    else:
        harness.process.returncode = 0
    with pytest.raises(RuntimeError, match="exited with code 0"):
        await harness.provider().open()
    assert harness.probed == 0
    assert not harness.created[0].exists()


@pytest.mark.parametrize("browser_pids", [[9876], [], [4321, 9876]])
async def test_process_identity_must_be_unique_and_match_the_created_process(
    harness: Harness,
    browser_pids: list[int],
) -> None:
    harness.browser_pids = browser_pids
    with pytest.raises(RuntimeError, match="endpoint ownership mismatch"):
        await harness.provider().open()
    assert harness.process.signals
    assert harness.process.returncode == 0
    assert not harness.created[0].exists()


async def test_unavailable_ownership_proof_fails_closed(harness: Harness) -> None:
    harness.probe_error = RuntimeError("SystemInfo command refused")
    with pytest.raises(RuntimeError, match="cannot establish endpoint ownership.*SystemInfo"):
        await harness.provider().open()
    assert harness.process.signals
    assert not harness.created[0].exists()


async def test_raw_overrides_describe_the_actual_owned_resources(
    harness: Harness, tmp_path: Path
) -> None:
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    sentinel = persistent / "keep"
    sentinel.write_text("user-owned")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    provider = harness.provider(
        extra_args=[f"--user-data-dir={persistent}", f"--remote-debugging-port={port}"]
    )
    session = await provider.open()
    assert session.metadata["user_data_dir"] == str(persistent)
    assert session.metadata["port"] == port
    assert session.metadata["pid"] == harness.process.pid
    assert harness.created == []
    assert all(
        arg == f"--user-data-dir={persistent}"
        for arg in harness.commands[0]
        if arg.startswith("--user-data-dir=")
    )
    await provider.close(session)
    assert sentinel.read_text() == "user-owned"


@pytest.mark.parametrize("persistent", [False, True])
async def test_spawn_failure_only_removes_a_temporary_profile(
    harness: Harness,
    tmp_path: Path,
    persistent: bool,
) -> None:
    profile = tmp_path / "user-profile"
    profile.mkdir()
    harness.spawn_error = OSError("executable stopped being executable")
    with pytest.raises(OSError, match="executable stopped"):
        await harness.provider(user_data_dir=str(profile) if persistent else None).open()
    assert profile.exists()
    assert all(not path.exists() for path in harness.created)


async def test_temporary_profile_cleanup_failure_preserves_the_spawn_failure(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.spawn_error = OSError("process could not start")

    def fail_remove(_path: str) -> None:
        raise PermissionError("profile cleanup denied")

    monkeypatch.setattr(local_chrome.shutil, "rmtree", fail_remove)
    with pytest.raises(OSError, match="process could not start") as caught:
        await harness.provider().open()
    assert "profile cleanup denied" in " ".join(caught.value.__notes__)


async def test_cancelled_spawn_finishes_acquisition_then_releases_owned_process(
    harness: Harness,
) -> None:
    harness.spawn_entered = asyncio.Event()
    harness.spawn_gate = asyncio.Event()
    provider = harness.provider()
    opening = asyncio.create_task(provider.open())
    await harness.spawn_entered.wait()
    opening.cancel()
    await asyncio.sleep(0)
    assert not opening.done()
    harness.spawn_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert harness.process.returncode == 0
    assert harness.process.signals
    assert not harness.created[0].exists()


async def test_repeated_cancellation_waits_for_process_exit_before_deleting_profile(
    harness: Harness,
) -> None:
    provider = harness.provider()
    session = await provider.open()
    harness.process.wait_gate = asyncio.Event()
    closing = asyncio.create_task(provider.close(session))
    while not harness.process.signals:
        await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    assert harness.created[0].exists()
    harness.process.wait_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not harness.created[0].exists()
    await provider.close(session)


async def test_failed_cleanup_keeps_ownership_for_retry_and_does_not_delete_live_profile(
    harness: Harness,
) -> None:
    provider = harness.provider()
    session = await provider.open()
    harness.process.kill_error = PermissionError("termination denied")
    with pytest.raises(PermissionError, match="termination denied"):
        await provider.close(session)
    assert harness.created[0].exists()
    harness.process.kill_error = None
    await provider.close(session)
    assert not harness.created[0].exists()


async def test_cancelled_readiness_cleans_its_process_and_profile(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def wait(*_: Any) -> str:
        entered.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr(LocalChromeProvider, "_wait_for_cdp", wait)
    provider = harness.provider()
    opening = asyncio.create_task(provider.open())
    await entered.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert harness.process.returncode == 0
    assert not harness.created[0].exists()


async def test_close_all_attempts_other_sessions_after_one_cleanup_failure(
    harness: Harness,
) -> None:
    provider = harness.provider()
    first_process = harness.process
    first = await provider.open()
    first_process.kill_error = PermissionError("first cannot stop")
    harness.process = Process(4322)
    harness.browser_pids = [4322]
    second = await provider.open()
    with pytest.raises(PermissionError, match="first cannot stop"):
        await provider.close()
    assert Path(first.metadata["user_data_dir"]).exists()
    assert not Path(second.metadata["user_data_dir"]).exists()
    assert first_process.returncode is None
    assert harness.process.returncode == 0
    first_process.kill_error = None
    await provider.close()
    assert not Path(first.metadata["user_data_dir"]).exists()


async def test_cancelled_spawn_preserves_cancellation_if_process_creation_also_fails(
    harness: Harness,
) -> None:
    harness.spawn_entered = asyncio.Event()
    harness.spawn_gate = asyncio.Event()
    harness.spawn_error = OSError("spawn failed")
    opening = asyncio.create_task(harness.provider().open())
    await harness.spawn_entered.wait()
    opening.cancel()
    await asyncio.sleep(0)
    harness.spawn_gate.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await opening
    assert "spawn failed" in " ".join(caught.value.__notes__)
    assert not harness.created[0].exists()


async def test_cancellation_still_propagates_when_profile_cleanup_fails(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = harness.provider()
    session = await provider.open()
    harness.process.wait_gate = asyncio.Event()
    remove_tree = local_chrome.shutil.rmtree

    def fail_remove(path: str) -> None:
        raise PermissionError("profile removal denied")

    monkeypatch.setattr(local_chrome.shutil, "rmtree", fail_remove)
    closing = asyncio.create_task(provider.close(session))
    while not harness.process.signals:
        await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    harness.process.wait_gate.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await closing
    assert "profile removal denied" in " ".join(caught.value.__notes__)
    assert harness.created[0].exists()
    monkeypatch.setattr(local_chrome.shutil, "rmtree", remove_tree)
    await provider.close()
    assert not harness.created[0].exists()


# ── The launcher's lifetime bounds Chrome's ────────────────────────

tethered = pytest.mark.skipif(not local_chrome._TETHERED, reason="Chrome is tethered on POSIX")


def _tether_closed(end: int) -> bool:
    """Whether every writing end of the pipe whose reading end is ``end`` is closed."""
    os.set_blocking(end, False)
    try:
        return os.read(end, 1) == b""
    except BlockingIOError:
        return False


def _open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


@tethered
async def test_chrome_runs_tethered_and_leads_its_own_process_group(harness: Harness) -> None:
    provider = harness.provider()
    session = await provider.open()
    command, options = harness.commands[0], harness.spawn_options[0]
    tether = local_chrome._TETHER_EXEC
    assert command[: len(tether)] == tether
    assert list(command[len(tether) :]) == session.metadata["command_line"]
    assert "--remote-debugging-pipe" in session.metadata["command_line"]
    assert "--remote-debugging-pipe" in session.metadata["launch_flags"]
    assert options["process_group"] == 0
    writing_end = provider._launches[session.session_id].tether
    assert writing_end is not None and not os.get_inheritable(writing_end)
    [chrome_end] = harness.chrome_ends
    assert not _tether_closed(chrome_end)
    await provider.close(session)
    assert _tether_closed(chrome_end)


@tethered
async def test_leftover_helpers_are_killed_once_the_browser_is_seen_to_exit(
    harness: Harness,
) -> None:
    provider = harness.provider()
    session = await provider.open()
    await provider.close(session)
    assert harness.group_kills == [(harness.process.pid, 0)]


@tethered
async def test_a_browser_that_exited_unseen_leaves_its_group_number_alone(
    harness: Harness,
) -> None:
    """When it exited is unknown, so its process group ID may be another's by now."""
    provider = harness.provider()
    session = await provider.open()
    harness.process.returncode = 1
    await provider.close(session)
    assert harness.group_kills == []
    assert _tether_closed(harness.chrome_ends[0])


@tethered
async def test_a_failed_stop_keeps_chrome_tethered_for_the_retry(harness: Harness) -> None:
    provider = harness.provider()
    session = await provider.open()
    [chrome_end] = harness.chrome_ends
    harness.process.kill_error = PermissionError("termination denied")
    with pytest.raises(PermissionError, match="termination denied"):
        await provider.close(session)
    assert not _tether_closed(chrome_end)
    assert harness.group_kills == []
    harness.process.kill_error = None
    await provider.close(session)
    assert _tether_closed(chrome_end)
    assert harness.group_kills == [(harness.process.pid, 0)]


@tethered
@pytest.mark.parametrize("failure", [None, "spawn", "proof"])
async def test_no_tether_descriptor_outlives_its_launch(
    harness: Harness, failure: str | None
) -> None:
    before = _open_descriptors()
    provider = harness.provider()
    if failure is None:
        await provider.close(await provider.open())
    else:
        if failure == "spawn":
            harness.spawn_error = OSError("process could not start")
        else:
            harness.probe_error = RuntimeError("SystemInfo command refused")
        with pytest.raises((OSError, RuntimeError)):
            await provider.open()
    assert _open_descriptors() == before + len(harness.chrome_ends)


@tethered
@pytest.mark.parametrize("executable", ["missing", "not-executable"])
async def test_live_an_executable_the_shell_cannot_run_is_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: str
) -> None:
    """Launched directly, these raised ``FileNotFoundError`` / ``PermissionError``."""
    path = tmp_path / executable
    if executable == "not-executable":
        path.write_text("not a program")
    profiles: list[Path] = []

    def profile(**_: Any) -> str:
        profiles.append(tmp_path / f"temporary-{len(profiles)}")
        profiles[-1].mkdir()
        return str(profiles[-1])

    monkeypatch.setattr(local_chrome.tempfile, "mkdtemp", profile)
    provider = LocalChromeProvider(executable_path=str(path), cdp_wait_timeout=20)
    with pytest.raises(RuntimeError, match="the Chrome executable is missing or cannot be"):
        await provider.open()
    assert not provider._launches
    assert profiles and not profiles[0].exists()


#: A launcher that dies abruptly: it opens a session, reports it and waits to be killed.
_LAUNCHER = """
import asyncio, json
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider

async def main():
    session = await LocalChromeProvider().open()
    metadata = session.metadata
    print(json.dumps({"pid": metadata["pid"], "profile": metadata["user_data_dir"]}), flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
"""


def _processes_of(profile: str) -> list[int]:
    """Every live process whose command line names ``profile``: a browser and its helpers."""
    listing = subprocess.run(
        ["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, check=True
    ).stdout
    return [
        int(pid)
        for pid, _, command in (line.strip().partition(" ") for line in listing.splitlines())
        if f"--user-data-dir={profile}" in command
    ]


def _kill_processes_of(profile: str) -> None:
    """A failed test's cleanup: only processes found by this test's own profile path."""
    for pid in _processes_of(profile):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


async def _until_gone(profile: str, seconds: float) -> list[int]:
    """Wait for every process of ``profile`` to exit; what is left at the deadline."""
    left = _processes_of(profile)
    for _ in range(int(seconds * 10)):
        if not left:
            break
        await asyncio.sleep(0.1)
        left = _processes_of(profile)
    return left


@tethered
@pytest.mark.requires_chrome
async def test_live_chrome_exits_when_its_launcher_is_killed() -> None:
    launcher = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _LAUNCHER,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    profile: str | None = None
    try:
        assert launcher.stdout is not None and launcher.stderr is not None
        line = await asyncio.wait_for(launcher.stdout.readline(), 60)
        if not line:
            await launcher.wait()
            pytest.fail(f"the launcher exited first: {(await launcher.stderr.read()).decode()}")
        launched = json.loads(line)
        pid, profile = launched["pid"], launched["profile"]
        assert profile is not None
        processes = _processes_of(profile)
        assert pid in processes and len(processes) > 1, "a browser and its helpers"
        assert os.getpgid(pid) == pid

        launcher.kill()
        assert await launcher.wait() == -signal.SIGKILL
        # Measured about 1.2 s with the default switches, about 7 s without them.
        left = await _until_gone(profile, 30)
        assert not left, f"Chrome processes outlived their launcher: {left}"
    finally:
        if launcher.returncode is None:
            launcher.kill()
            await launcher.wait()
        if profile is not None:
            _kill_processes_of(profile)
            # Nobody is left to delete the temporary profile on this path.
            shutil.rmtree(profile, ignore_errors=True)


@tethered
@pytest.mark.requires_chrome
@pytest.mark.parametrize("browser", ["answers SIGTERM", "is stopped"])
async def test_live_close_kills_everything_left_in_chromes_process_group(browser: str) -> None:
    """A process in Chrome's group that does not exit by itself is killed by close().

    Chrome's own helpers exit soon after their browser (measured up to 4 s
    after a SIGKILL), and stopping them does not hold them: the kernel hangs
    up a stopped, orphaned process group. So a plain process placed in the
    group stands in for a helper that never notices its browser is gone.
    """
    provider = LocalChromeProvider(shutdown_timeout=0.5)
    session = await provider.open()
    pid, profile = session.metadata["pid"], session.metadata["user_data_dir"]
    straggler: subprocess.Popen[bytes] | None = None
    try:
        assert os.getpgid(pid) == pid
        assert len(_processes_of(profile)) > 1
        straggler = subprocess.Popen(["sleep", "300"], process_group=pid)
        if browser == "is stopped":
            os.kill(pid, signal.SIGSTOP)  # close() has to SIGKILL it
        await provider.close(session)
        assert straggler.wait(timeout=10) == -signal.SIGKILL
        left = await _until_gone(profile, 10)
        assert not left, f"Chrome processes outlived close(): {left}"
        assert not Path(profile).exists()
    finally:
        if straggler is not None and straggler.poll() is None:
            straggler.kill()
            straggler.wait()
        _kill_processes_of(profile)
        await provider.close()


async def _browser_pid(session: Any) -> int:
    async with CDPClient(session.cdp_url, proxy=None) as probe:
        result = await probe.send("SystemInfo.getProcessInfo")
        return next(item["id"] for item in result["processInfo"] if item["type"] == "browser")


@pytest.mark.requires_chrome
async def test_live_concurrent_sessions_keep_process_endpoint_profile_and_cleanup_separate() -> (
    None
):
    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        body = b"<title>Ownership fixture</title><p>local page</p>"
        writer.write(
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    page = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    provider = LocalChromeProvider(startup_url=page)
    try:
        first, second = await asyncio.gather(provider.open(), provider.open())
        assert first.session_id != second.session_id
        for name in ("pid", "port", "user_data_dir"):
            assert first.metadata[name] != second.metadata[name]
        assert await _browser_pid(first) == first.metadata["pid"]
        assert await _browser_pid(second) == second.metadata["pid"]
        assert second.cdp_url is not None
        async with CDPClient(second.cdp_url, proxy=None) as probe:
            targets = await probe.send("Target.getTargets")
            assert any(target["url"] == page for target in targets["targetInfos"])
        await provider.close(first)
        assert not Path(first.metadata["user_data_dir"]).exists()
        assert Path(second.metadata["user_data_dir"]).exists()
        assert await _browser_pid(second) == second.metadata["pid"]
        await provider.close(second)
        assert not Path(second.metadata["user_data_dir"]).exists()
    finally:
        await provider.close()
        server.close()
        await server.wait_closed()


@pytest.mark.requires_chrome
async def test_live_external_endpoint_keeps_its_process_profile_and_existing_tabs(
    tmp_path: Path,
) -> None:
    from spectragent.core.capabilities.browser import BrowserCapability

    profile = tmp_path / "external-profile"
    profile.mkdir()
    sentinel = profile / "caller-owned.txt"
    sentinel.write_text("keep")
    owner = LocalChromeProvider(headless=True, user_data_dir=str(profile))
    session = await owner.open()
    assert session.cdp_url is not None
    cap = BrowserCapability(cdp_url=session.cdp_url)
    try:
        async with CDPClient(session.cdp_url, proxy=None) as probe:
            await probe.send("Target.createTarget", {"url": "about:blank"})
            before = await probe.send("Target.getTargets")
            existing = {
                info["targetId"] for info in before["targetInfos"] if info["type"] == "page"
            }
            assert len(existing) >= 2
            await cap.setup()
            assert cap._session is None
            await cap.teardown()
            after = await probe.send("Target.getTargets")
            remaining = {
                info["targetId"] for info in after["targetInfos"] if info["type"] == "page"
            }
            assert existing <= remaining
            assert await _browser_pid(session) == session.metadata["pid"]
            assert sentinel.read_text() == "keep"
    finally:
        await cap.teardown()
        await owner.close(session)
    assert sentinel.read_text() == "keep"


@pytest.mark.requires_chrome
async def test_live_existing_chrome_survives_port_conflict_and_profile_handoff(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "persistent-profile"
    profile.mkdir()
    sentinel = profile / "user-owned"
    sentinel.write_text("preserve")
    owner = LocalChromeProvider(user_data_dir=str(profile))
    contender = LocalChromeProvider(cdp_wait_timeout=5)
    original = await owner.open()
    try:
        with pytest.raises(RuntimeError, match="remote_debugging_port=.*unavailable"):
            await contender.open(remote_debugging_port=original.metadata["port"])
        assert await _browser_pid(original) == original.metadata["pid"]
        with pytest.raises(RuntimeError, match="exited with code"):
            await contender.open(user_data_dir=str(profile))
        assert await _browser_pid(original) == original.metadata["pid"]
        assert sentinel.read_text() == "preserve"
    finally:
        await contender.close()
        await owner.close()
    assert sentinel.read_text() == "preserve"


@pytest.mark.requires_chrome
async def test_live_cancelled_startup_only_removes_the_new_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = asyncio.Event()
    launched: dict[str, Any] = {}
    wait_for_cdp = LocalChromeProvider._wait_for_cdp

    async def pause_after_proof(
        self: LocalChromeProvider, port: int, process: Any, profile: str
    ) -> str:
        endpoint = await wait_for_cdp(self, port, process, profile)
        launched.update(process=process, profile=Path(profile), endpoint=endpoint)
        ready.set()
        await asyncio.Future()
        return endpoint

    monkeypatch.setattr(LocalChromeProvider, "_wait_for_cdp", pause_after_proof)
    provider = LocalChromeProvider()
    opening = asyncio.create_task(provider.open())
    try:
        await asyncio.wait_for(ready.wait(), 15)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert launched["process"].returncode is not None
        assert not launched["profile"].exists()
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.TransportError):
                await client.get(
                    launched["endpoint"].replace("ws://", "http://").split("/devtools/")[0]
                    + "/json/version"
                )
    finally:
        opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)
        await provider.close()


async def test_late_temporary_profile_writes_are_retried_after_process_exit(harness, monkeypatch):
    import errno

    provider = harness.provider()
    session = await provider.open()
    remove = local_chrome.shutil.rmtree
    sleep = asyncio.sleep
    attempts = []
    delays = []

    def churn(path):
        assert harness.process.returncode is not None
        attempts.append(path)
        if len(attempts) < 3:
            raise OSError(errno.ENOTEMPTY, "directory repopulated", path)
        remove(path)

    async def backoff(delay):
        delays.append(delay)
        await sleep(0)

    monkeypatch.setattr(local_chrome.shutil, "rmtree", churn)
    monkeypatch.setattr(local_chrome.asyncio, "sleep", backoff)
    await provider.close(session)
    assert len(attempts) == 3 and delays == [0.05, 0.1]
    assert not harness.created[0].exists()
    await provider.close(session)
    assert len(attempts) == 3


async def test_profile_churn_retry_is_bounded_and_failure_remains_retryable(harness, monkeypatch):
    import errno

    provider = harness.provider()
    session = await provider.open()
    remove = local_chrome.shutil.rmtree
    sleep = asyncio.sleep
    attempts = []

    def churn(path):
        attempts.append(path)
        raise OSError(errno.ENOTEMPTY, "persistent churn", path)

    async def backoff(delay):
        await sleep(0)

    monkeypatch.setattr(local_chrome.shutil, "rmtree", churn)
    monkeypatch.setattr(local_chrome.asyncio, "sleep", backoff)
    with pytest.raises(OSError) as failure:
        await provider.close(session)
    assert failure.value.errno == errno.ENOTEMPTY and len(attempts) == 5
    assert harness.created[0].exists()
    monkeypatch.setattr(local_chrome.shutil, "rmtree", remove)
    await provider.close(session)
    assert not harness.created[0].exists()

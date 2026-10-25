"""A real foreground viewer releases its listening port after terminal signals."""

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from spectragent.viewer import server


def available_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(predicate, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        if process.poll() is not None:
            raise AssertionError(process.communicate()[0])
        time.sleep(0.02)
    raise AssertionError("Viewer did not become ready")


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(0.1)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def assert_rebind(port):
    with socket.socket() as replacement:
        replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        replacement.bind(("127.0.0.1", port))
        replacement.listen()


def reap(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


@pytest.mark.parametrize("through_uv", [False, True])
@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_foreground_signal_exits_and_port_can_be_reused(tmp_path, through_uv, stop_signal):
    port = available_port()
    prefix = (
        ["uv", "run", "--no-sync", "spectragent"]
        if through_uv
        else [sys.executable, "-m", "spectragent.cli"]
    )
    command = [*prefix, "view", "--root", str(tmp_path), "--port", str(port)]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True
    )
    try:
        wait_for(lambda: listening(port), process)
        # Ctrl-C from a terminal targets the foreground process group, including uv.
        os.killpg(process.pid, stop_signal)
        process.communicate(timeout=6)
        assert process.returncode != 0
        assert not listening(port)
        assert_rebind(port)
    finally:
        reap(process)


def test_slow_request_cannot_hold_shutdown_indefinitely(tmp_path):
    port = available_port()
    script = """
import asyncio
import sys
from pathlib import Path
from spectragent.viewer import server
original = server.create_app
def create(root, **kwargs):
    app = original(root, **kwargs)
    @app.get('/slow')
    async def slow():
        (root / 'request-started').write_text('ready')
        await asyncio.Event().wait()
    return app
server.create_app = create
server.serve(Path(sys.argv[1]), port=int(sys.argv[2]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for(lambda: listening(port), process)
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            connection.sendall(b"GET /slow HTTP/1.1\r\nHost: localhost\r\n\r\n")
            wait_for(lambda: (tmp_path / "request-started").exists(), process)
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=6)
        assert not listening(port)
        assert_rebind(port)
    finally:
        reap(process)


def test_listener_is_closed_even_if_server_raises_before_shutdown(tmp_path, monkeypatch):
    listeners = []
    original_bind = server.uvicorn.Config.bind_socket

    def bind(config):
        listener = original_bind(config)
        listeners.append((listener, listener.getsockname()[1]))
        return listener

    def interrupted(self, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(server.uvicorn.Config, "bind_socket", bind)
    monkeypatch.setattr(server.uvicorn.Server, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        server.serve(tmp_path, port=0)
    assert listeners[0][0].fileno() == -1
    assert_rebind(listeners[0][1])


def test_busy_port_never_opens_an_unrelated_existing_viewer(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server.webbrowser, "open", lambda *args: pytest.fail("opened a busy port"))
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with pytest.raises(SystemExit):
            server.serve(tmp_path, port=occupied.getsockname()[1], open_browser=True)
        assert occupied.fileno() != -1
    assert "Spectragent viewer:" not in capsys.readouterr().out

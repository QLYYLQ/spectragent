"""A stand-in for AndroidWorld's task service, started exactly as the oracle starts the real one.

:func:`fake_installation` lays out an AndroidWorld installation the
``androidworld`` oracle accepts: ``src`` is a checkout at the pinned commit, the
forwarder APK is a placeholder, ``.venv/bin/python`` is a script that runs
this file under Spectragent's interpreter with the arguments the oracle gives
the launcher, and a stand-in for ffmpeg goes first on the test's PATH. So a
test drives the oracle's real spawn: its argv, its
whitelisted environment, its process group.

Run as a program, this file is the service: it answers the endpoints the oracle
calls (``/health``, ``/suite/reinitialize``, ``/task/goal``,
``/task/initialize``, ``/reset``, ``/execute_action``, ``/task/score``,
``/task/tear_down``) as the real one does, and, like it, listens only once its
startup is over. What it
does is read from ``<root>/behaviour.json``, again for every request, so a test
can change an endpoint's answer while the service runs: per path, ``{"status": 500}``
answers an error, ``{"hang": true}`` never answers, ``{"text": ...}`` answers
that text, ``{"json": ...}`` answers that object; the top level may also say
``start_delay_s`` (how long before it listens), ``exit_code`` (exit at once
with it) and ``ignore_sigterm``. It records every request in
``<root>/record/requests.jsonl`` (its JSON body too, when it has one), its PID,
group, argv and environment in
``<root>/record/service.json``, and leaves a ``sleep`` in its process group, as
the real one leaves ``adb logcat``. It imports nothing but the standard
library: the oracle gives it no ``PYTHONPATH``.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

if TYPE_CHECKING:  # run as a program, this file imports only the standard library
    import pytest
    from pydantic import JsonValue

#: What each endpoint answers when the behaviour file says nothing about it.
DEFAULT_ANSWERS: dict[str, dict[str, JsonValue]] = {
    "/health": {"status": "success"},
    "/suite/reinitialize": {"status": "success", "message": "Task suite re-initialized."},
    "/task/goal": {"goal": "Turn brightness to the max value."},
    "/task/initialize": {"status": "success", "message": "Task initialized."},
    "/reset": {"status": "success", "message": "Environment reset."},
    "/execute_action": {"status": "success", "message": "Action executed."},
    "/task/score": {"score": 1.0},
    "/task/tear_down": {"status": "success", "message": "Task torn down."},
}


def fake_installation(
    root: Path, monkeypatch: pytest.MonkeyPatch, behaviour: dict[str, JsonValue] | None = None
) -> Path:
    """An AndroidWorld installation at *root* whose service is this file, behaving as *behaviour* says.

    A stand-in for ffmpeg goes in ``<root>/host-bin``, first on this process's
    PATH until the test ends.
    """
    from spectragent.core.capabilities.android.oracle.androidworld import (
        FORWARDER_APK,
        PINNED_COMMIT,
    )

    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(__file__)} "$@"\n'
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    host_bin = root / "host-bin"
    host_bin.mkdir()
    (host_bin / "ffmpeg").write_text("#!/bin/sh\n")
    (host_bin / "ffmpeg").chmod(0o755)
    monkeypatch.setenv("PATH", f"{host_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    server = root / "src" / "server"
    server.mkdir(parents=True)
    (server / "android_server.py").write_text("")
    (root / "src" / ".git").mkdir()
    (root / "src" / ".git" / "HEAD").write_text(f"{PINNED_COMMIT}\n")
    (root / "cache" / "a11y").mkdir(parents=True)
    (root / "cache" / "a11y" / FORWARDER_APK).write_bytes(b"apk")
    (root / "record").mkdir()
    write_behaviour(root, behaviour or {})
    return root


def write_behaviour(root: Path, behaviour: dict[str, JsonValue]) -> None:
    (root / "behaviour.json").write_text(json.dumps(behaviour))


def requests(root: Path) -> list[dict[str, JsonValue]]:
    """Every request the service received, in order: method, path, query and JSON body (or ``None``)."""
    log = root / "record" / "requests.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


def service(root: Path) -> dict[str, JsonValue]:
    """The service's PID, process group, ``sleep`` child, argv and environment."""
    return json.loads((root / "record" / "service.json").read_text())


def _serve(argv: list[str]) -> None:
    port = int(argv[argv.index("--port") + 1])
    root = Path(argv[argv.index("--src") + 1]).parent
    behaviour = json.loads((root / "behaviour.json").read_text())
    record = root / "record"
    child = subprocess.Popen(["sleep", "300"])
    (record / "service.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "group": os.getpgid(0),
                "child": child.pid,
                "argv": argv,
                "env": dict(os.environ),
            }
        )
    )
    print("fake AndroidWorld service: starting", flush=True)
    if behaviour.get("exit_code") is not None:
        print("fake AndroidWorld service: giving up", flush=True)
        sys.exit(behaviour["exit_code"])
    if behaviour.get("ignore_sigterm"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(behaviour.get("start_delay_s", 0))
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            url = urlsplit(self.path)
            sent = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            with lock, (record / "requests.jsonl").open("a") as log:
                entry = {
                    "method": self.command,
                    "path": url.path,
                    "query": dict(parse_qsl(url.query)),
                    "body": json.loads(sent) if sent else None,
                }
                log.write(json.dumps(entry) + "\n")
            rule = json.loads((root / "behaviour.json").read_text()).get(url.path, {})
            if rule.get("hang"):
                time.sleep(3600)
            status, body, kind = (
                200,
                json.dumps(DEFAULT_ANSWERS.get(url.path, {})),
                "application/json",
            )
            if "status" in rule:
                status, body, kind = rule["status"], "Internal Server Error", "text/plain"
            elif "text" in rule:
                body, kind = rule["text"], "text/plain"
            elif "json" in rule:
                body = json.dumps(rule["json"])
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _answer

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    _serve(sys.argv[1:])

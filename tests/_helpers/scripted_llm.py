"""A loopback OpenAI-compatible chat endpoint that answers from a script.

For live runs that must exercise the whole runtime — composer, agent, loop,
trace hook, browser — without spending on a real model: point an ``openai``
LLM's ``base_url`` at :attr:`ScriptedLLMServer.base_url` and every
``POST /v1/chat/completions`` pops the next scripted reply (the last one is
repeated once the script runs out, so a loop that asks again gets a
well-formed answer rather than a hang). Requests are kept for assertions.

A reply is the text itself, or a callable ``(request_body) -> str`` that
writes it from the request — M31's branch runs pick an element index out of
the prompt's DOM, which a fresh browser numbers anew every launch.

The recorder of M29 uses it with UI-TARS replies; M31 and M32 reuse it.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: One scripted reply: the text, or what writes it from the request body.
Reply = str | Callable[[dict[str, Any]], str]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, *_args: Any) -> None:
        return None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        reply = self.server.owner._next(body)
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, owner: ScriptedLLMServer) -> None:
        self.owner = owner
        super().__init__(("127.0.0.1", 0), _Handler)


class ScriptedLLMServer:
    """The endpoint; a context manager."""

    def __init__(self, replies: Sequence[Reply], *, model: str = "scripted-fixture") -> None:
        if not replies:
            raise ValueError("a scripted LLM needs at least one reply")
        self.replies = list(replies)
        self.model = model
        self.requests: list[dict[str, Any]] = []
        #: What a callable reply raised (the request then gets an empty reply,
        #: which the agent cannot parse): a test asserts this is empty.
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None, "not started"
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> ScriptedLLMServer:
        self._server = _Server(self)
        self._thread = threading.Thread(
            target=lambda: self._server and self._server.serve_forever(poll_interval=0.02),
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _next(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.requests.append(body)
            index = min(len(self.requests) - 1, len(self.replies) - 1)
            reply = self.replies[index]
            try:
                content = reply(body) if callable(reply) else reply
            except Exception as exc:  # a scripted reply that cannot answer this request
                self.errors.append(f"request {len(self.requests)}: {type(exc).__name__}: {exc}")
                content = ""
        return {
            "id": f"chatcmpl-scripted-{len(self.requests)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

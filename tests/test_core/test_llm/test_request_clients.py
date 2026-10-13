"""A provider closes the client it built for a request before the request returns (arch §4.7).

Each provider family builds a new SDK client — a connection pool — for a request. Left to
the garbage collector it is not merely leaked: openai, anthropic, groq and google-genai
clients schedule ``aclose()`` on the running loop from ``__del__``. When the collector
reaches one in a reference cycle, the same pass closes its pooled socket, the next socket
takes the freed descriptor number, and the late ``aclose()`` unregisters that *number* from
the loop's selector — the next model call's connect callback with it, so that call never
connects, whatever its timeout (the flywheel rehearsal: kettle → sign-in → drift, drift's step 1).

Each family's real SDK runs here against a loopback HTTP/1.1 keep-alive endpoint, with the
collector off so nothing but the provider can close anything:

- the connection a request opened is hung up by the time ``ainvoke`` returns, on success
  and on an error reply alike;
- the hang itself, provoked: the run holding the provider is dropped and collected, a new socket
  takes the lowest free descriptor number — the request's, had the collector freed it — and
  a reader registered on it must still fire after whatever the collection queued has run;
- an injected ``http_client`` is its injector's, and stays open.
"""

from __future__ import annotations

import asyncio
import gc
import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from spectragent.core.llm.exceptions import ModelProviderError
from spectragent.core.llm.messages import UserMessage

_OPENAI_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "stub",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_ANTHROPIC_REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "stub",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
_GEMINI_REPLY = {
    "candidates": [
        {"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP"}
    ],
    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, *_args: Any) -> None:
        return None

    def setup(self) -> None:
        super().setup()
        self.server.opened()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status, body = self.server.answer
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def finish(self) -> None:
        # Reached once the client hangs up: the keep-alive loop in ``handle``
        # reads requests until the connection is closed from the other side.
        super().finish()
        self.server.hung_up()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, answer: tuple[int, dict[str, Any]]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.answer = answer
        self._state = threading.Condition()
        self._open = 0
        self._hung_up = 0

    def opened(self) -> None:
        with self._state:
            self._open += 1

    def hung_up(self) -> None:
        with self._state:
            self._hung_up += 1
            self._state.notify_all()

    def all_hung_up(self, timeout: float) -> bool:
        """Whether every connection the endpoint accepted has been closed by the client."""
        with self._state:
            return self._state.wait_for(lambda: self._hung_up == self._open > 0, timeout)


@contextmanager
def _endpoint(answer: tuple[int, dict[str, Any]]) -> Iterator[_Server]:
    server = _Server(answer)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _root(server: _Server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


@dataclass(frozen=True)
class _Family:
    id: str
    reply: dict[str, Any]
    build: Callable[..., Any]


def _openai(root: str, **kw: Any) -> Any:
    from spectragent.core.llm.providers.openai import ChatOpenAI

    return ChatOpenAI(model="gpt-4o", api_key="stub", base_url=f"{root}/v1", max_retries=0, **kw)


def _groq(root: str) -> Any:
    from spectragent.core.llm.providers.openai_groq import ChatGroq

    return ChatGroq(model="llama-3.3-70b-versatile", api_key="stub", base_url=root, max_retries=0)


def _anthropic(root: str, **kw: Any) -> Any:
    from spectragent.core.llm.providers.anthropic import ChatAnthropic

    return ChatAnthropic(
        model="claude-sonnet-4-5", api_key="stub", base_url=root, max_retries=0, **kw
    )


def _google(root: str) -> Any:
    from spectragent.core.llm.providers.google import ChatGoogle

    return ChatGoogle(model="gemini-2.5-flash", api_key="stub", base_url=root, max_retries=1)


_FAMILIES = [
    _Family("openai", _OPENAI_REPLY, _openai),
    _Family("groq", _OPENAI_REPLY, _groq),
    _Family("anthropic", _ANTHROPIC_REPLY, _anthropic),
    _Family("google", _GEMINI_REPLY, _google),
]
_IDS = [family.id for family in _FAMILIES]


@pytest.fixture(autouse=True)
def _loopback_bypasses_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDKs' httpx clients read the environment's proxy; loopback must not go through it.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@contextmanager
def _collector_off() -> Iterator[None]:
    """Nothing but the code under test closes anything while this holds.

    Earlier tests' garbage is collected first: what a later collection frees is this test's.
    """
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
async def test_the_request_hangs_up_before_it_returns(family: _Family) -> None:
    with _endpoint((200, family.reply)) as server, _collector_off():
        provider = family.build(_root(server))
        result = await provider.ainvoke([UserMessage(content="hi")])
        assert result.completion == "hi"
        assert server.all_hung_up(timeout=5), (
            f"{family.id}: the request's connection is still open after ainvoke returned — "
            "its client was left for the garbage collector to close"
        )


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
async def test_an_error_reply_hangs_up_too(family: _Family) -> None:
    error = {"error": {"type": "invalid_request_error", "message": "bad request", "code": 400}}
    with _endpoint((400, error)) as server, _collector_off():
        provider = family.build(_root(server))
        with pytest.raises(ModelProviderError):
            await provider.ainvoke([UserMessage(content="hi")])
        assert server.all_hung_up(timeout=5), (
            f"{family.id}: a failed request left its connection open"
        )


class _ComposedRun:
    """Stands in for a task's composed run: it holds the provider and, like the real one, is a
    reference cycle — so dropping it leaves the provider to the cyclic collector."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.itself = self


@pytest.mark.parametrize("family", _FAMILIES, ids=_IDS)
async def test_a_reused_descriptor_keeps_its_registration(family: _Family) -> None:
    loop = asyncio.get_running_loop()
    with _endpoint((200, family.reply)) as server:
        with _collector_off():
            run = _ComposedRun(family.build(_root(server)))
            await run.provider.ainvoke([UserMessage(content="hi")])
            # The benchmark runner drops a task's composed run when the task ends; the
            # collector then finalizes whatever the provider left open, in one pass with
            # the SDK's ``__del__`` that queues ``aclose()``.
            del run
            gc.collect()
            # The lowest free numbers: the request's socket's, had the collector closed it.
            reader, writer = socket.socketpair()
        try:
            fired = asyncio.Event()
            loop.add_reader(reader, fired.set)
            writer.send(b"x")
            try:
                await asyncio.wait_for(fired.wait(), timeout=2)
            except TimeoutError:
                pytest.fail(
                    f"{family.id}: a late close of the request's client unregistered descriptor "
                    f"{reader.fileno()}, which a new socket owns — the next model call's "
                    "connect would never complete (arch §4.7)"
                )
        finally:
            loop.remove_reader(reader)
            reader.close()
            writer.close()


@pytest.mark.parametrize("build", [_openai, _anthropic], ids=["openai", "anthropic"])
async def test_an_injected_http_client_is_left_to_its_owner(build: Callable[..., Any]) -> None:
    reply = _OPENAI_REPLY if build is _openai else _ANTHROPIC_REPLY
    with _endpoint((200, reply)) as server:
        injected = httpx.AsyncClient()
        try:
            provider = build(_root(server), http_client=injected)
            await provider.ainvoke([UserMessage(content="hi")])
            await provider.ainvoke([UserMessage(content="again")])
            assert not injected.is_closed, "the provider closed a client it does not own"
        finally:
            await injected.aclose()

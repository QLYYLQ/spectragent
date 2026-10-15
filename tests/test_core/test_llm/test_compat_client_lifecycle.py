"""Execution ownership against the actual OpenAI, Anthropic and Groq SDKs.

MockTransport replaces only the wire. SDK construction, requests and public close
methods remain real, including the distinction between SDK and HTTP ownership.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from spectragent.core.llm.exceptions import ModelProviderError
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.providers.anthropic import ChatAnthropic
from spectragent.core.llm.providers.anthropic_aws import ChatAnthropicAWS
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI
from spectragent.core.llm.providers.openai_cerebras import ChatCerebras
from spectragent.core.llm.providers.openai_deepseek import ChatDeepSeek
from spectragent.core.llm.providers.openai_groq import ChatGroq
from spectragent.core.llm.providers.openai_openrouter import ChatOpenRouter
from spectragent.core.llm.providers.openai_vercel import ChatVercel

_OPENAI_REPLY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
}
_ANTHROPIC_REPLY = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 2, "output_tokens": 1},
}


@dataclass(frozen=True)
class _Provider:
    build: Callable[..., Any]
    sdk_name: str = "AsyncOpenAI"
    options: dict[str, Any] = field(default_factory=dict)
    anthropic: bool = False

    @property
    def sdk_module(self) -> str:
        return "groq" if self.build is ChatGroq else self.build.__module__

    def model(self, **kwargs: Any) -> Any:
        params = {
            "model": "claude-sonnet-4-5" if self.anthropic else "gpt-4o",
            "api_key": "test-key",
            "max_retries": 0,
            **self.options,
            **kwargs,
        }
        return self.build(**params)

    def reply(self) -> dict[str, Any]:
        return _ANTHROPIC_REPLY if self.anthropic else _OPENAI_REPLY


_PROVIDERS = [
    _Provider(ChatOpenAI),
    _Provider(
        ChatAzureOpenAI, "AsyncAzureOpenAIClient", {"azure_endpoint": "https://test.invalid"}
    ),
    _Provider(ChatDeepSeek),
    _Provider(ChatCerebras),
    _Provider(ChatOpenRouter),
    _Provider(ChatVercel),
    _Provider(ChatGroq, "AsyncGroq"),
    _Provider(ChatAnthropic, "AsyncAnthropic", anthropic=True),
    _Provider(
        ChatAnthropicAWS,
        "AsyncAnthropicBedrock",
        {"aws_access_key": "test", "aws_secret_key": "test", "aws_region": "us-east-1"},
        anthropic=True,
    ),
]
_IDS = [case.build.__name__ for case in _PROVIDERS]
_MESSAGES = [UserMessage(content="hello")]


class _CountingClient(httpx.AsyncClient):
    def __init__(self, *, fail_close: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.close_calls = 0
        self.fail_close = fail_close

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("transport close failed")
        await super().aclose()


def _capture_clients(
    case: _Provider,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_close: bool = False,
) -> tuple[list[Any], list[_CountingClient], list[httpx.Request]]:
    """Keep the real SDK and substitute its HTTP client only when it owns one."""
    module = importlib.import_module(case.sdk_module)
    constructor = getattr(module, case.sdk_name)
    sdks: list[Any] = []
    transports: list[_CountingClient] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=case.reply())

    def transport(**kwargs: Any) -> _CountingClient:
        client = _CountingClient(
            transport=httpx.MockTransport(respond), fail_close=fail_close, **kwargs
        )
        transports.append(client)
        return client

    def create(**kwargs: Any) -> Any:
        if kwargs.get("http_client") is None:
            kwargs["http_client"] = transport()
        client = constructor(**kwargs)
        sdks.append(client)
        return client

    if case.build is ChatAzureOpenAI:
        # Azure explicitly constructs HTTPX before creating its SDK. Keep that
        # real construction boundary, including a failing SDK initializer.
        monkeypatch.setattr(
            module, "httpx", SimpleNamespace(AsyncClient=transport, Limits=httpx.Limits)
        )
    monkeypatch.setattr(module, case.sdk_name, create)
    return sdks, transports, requests


@pytest.mark.parametrize("case", _PROVIDERS, ids=_IDS)
async def test_real_sdk_is_lazy_reused_then_replaced(
    case: _Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdks, transports, requests = _capture_clients(case, monkeypatch)
    model = case.model()
    assert await model.aclose() == ()
    assert not sdks and not transports

    first = await model.ainvoke(_MESSAGES)
    second = await model.ainvoke(_MESSAGES)
    assert first.completion == second.completion == "hi"
    assert first.usage is not None and first.usage.total_tokens == 3
    assert len(sdks) == len(transports) == 1
    assert len(requests) == 2
    assert not transports[0].is_closed
    assert await model.aclose() == ()
    assert transports[0].is_closed and transports[0].close_calls == 1
    assert await model.aclose() == ()
    assert transports[0].close_calls == 1

    await model.ainvoke(_MESSAGES)
    assert len(sdks) == len(transports) == 2
    assert not transports[1].is_closed
    assert await model.aclose() == ()
    assert transports[1].is_closed and transports[1].close_calls == 1


@pytest.mark.parametrize("case", _PROVIDERS, ids=_IDS)
@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_injected_transport_survives_every_exit(case: _Provider, outcome: str) -> None:
    entered = asyncio.Event()
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entered.set()
        if outcome == "cancelled":
            await asyncio.Event().wait()
        if outcome == "error":
            return httpx.Response(
                400, json={"error": {"type": "invalid_request_error", "message": "bad"}}
            )
        return httpx.Response(200, json=case.reply())

    injected = _CountingClient(transport=httpx.MockTransport(respond))
    model = case.model(http_client=injected)
    try:
        if outcome == "cancelled":
            call = asyncio.create_task(model.ainvoke(_MESSAGES))
            await asyncio.wait_for(entered.wait(), timeout=2)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        elif outcome == "error":
            with pytest.raises(ModelProviderError):
                await model.ainvoke(_MESSAGES)
        else:
            assert (await model.ainvoke(_MESSAGES)).completion == "hi"
            assert (await model.ainvoke(_MESSAGES)).completion == "hi"
        assert requests, "the injected transport must be used, never silently ignored"
        assert await model.aclose() == ()
        assert await model.aclose() == ()
        assert not injected.is_closed and injected.close_calls == 0
    finally:
        await injected.aclose()


@pytest.mark.parametrize("case", _PROVIDERS, ids=_IDS)
async def test_close_failure_does_not_repeat_a_successful_request(
    case: _Provider, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, transports, requests = _capture_clients(case, monkeypatch, fail_close=True)
    model = case.model()
    result = await model.ainvoke(_MESSAGES)
    assert result.completion == "hi"
    assert result.usage is not None and result.usage.total_tokens == 3
    diagnostics = await model.aclose()
    assert len(diagnostics) == 1 and diagnostics[0].status == "failed"
    assert "transport close failed" in diagnostics[0].detail
    assert "transport close failed" in caplog.text
    assert len(requests) == 1
    transports[0].fail_close = False
    await transports[0].aclose()


@pytest.mark.parametrize("build", [ChatDeepSeek, ChatCerebras])
async def test_transport_in_extra_client_arguments_is_also_borrowed(
    build: Callable[..., Any],
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_OPENAI_REPLY)

    async with _CountingClient(transport=httpx.MockTransport(respond)) as injected:
        model = build(model="gpt-4o", api_key="test", client_params={"http_client": injected})
        assert (await model.ainvoke(_MESSAGES)).completion == "hi"
        assert await model.aclose() == ()
        assert requests
        assert injected.close_calls == 0 and not injected.is_closed


@pytest.mark.parametrize("build", [ChatDeepSeek, ChatCerebras])
async def test_conflicting_transport_injections_are_refused(build: Callable[..., Any]) -> None:
    async with httpx.AsyncClient() as first, httpx.AsyncClient() as second:
        model = build(
            model="gpt-4o", api_key="test", http_client=first, client_params={"http_client": second}
        )
        with pytest.raises(ModelProviderError, match="different transports"):
            await model.ainvoke(_MESSAGES)
        assert await model.aclose() == ()
        assert not first.is_closed and not second.is_closed


async def test_azure_constructor_failure_still_releases_its_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = next(case for case in _PROVIDERS if case.build is ChatAzureOpenAI)
    _, transports, _ = _capture_clients(case, monkeypatch)
    module = importlib.import_module(case.sdk_module)

    def fail(**kwargs: Any) -> Any:
        raise RuntimeError("SDK initialization failed")

    monkeypatch.setattr(module, case.sdk_name, fail)
    model = case.model()
    with pytest.raises(ModelProviderError, match="SDK initialization failed"):
        await model.ainvoke(_MESSAGES)
    assert len(transports) == 1 and not transports[0].is_closed
    assert await model.aclose() == ()
    assert transports[0].is_closed and transports[0].close_calls == 1


async def test_invalid_cleanup_budget_keeps_the_owned_client_available_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _PROVIDERS[0]
    _, transports, _ = _capture_clients(case, monkeypatch)
    model = case.model()
    try:
        await model.ainvoke(_MESSAGES)
        with pytest.raises(ValueError, match="positive"):
            await model.aclose(timeout=0)
        assert await model.aclose() == ()
        assert transports[0].is_closed, "invalid cleanup arguments must not lose resource ownership"
    finally:
        if transports and not transports[0].is_closed:
            await transports[0].aclose()


async def test_cleanup_reports_an_active_constructor_before_it_registers_a_client() -> None:
    import threading

    from spectragent.core.llm.client_resources import ClientResources

    clients = ClientResources("constructing-sdk")
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def construct() -> object:
        entered.set()
        release.wait(timeout=2)
        return object()

    def request() -> None:
        clients.get_or_create("sdk", construct, close=lambda _: closed.set())

    running = asyncio.create_task(clients.run_sync(request))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    try:
        diagnostics = await clients.aclose(timeout=0.01)
        assert diagnostics, "a still-active constructor cannot be reported as fully cleaned up"
        assert all(item.status == "in_use" for item in diagnostics)
    finally:
        release.set()
        await running
        for _ in range(100):
            if closed.is_set():
                break
            await asyncio.sleep(0.001)
        assert closed.is_set()

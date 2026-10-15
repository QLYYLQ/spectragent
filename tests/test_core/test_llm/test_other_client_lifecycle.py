"""Execution ownership for native, HTTP, and synchronous model providers."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from google.genai.types import HttpOptions
from pydantic import BaseModel

from spectragent.core.llm.exceptions import ModelProviderError
from spectragent.core.llm.lifecycle import model_session
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.providers.bedrock import ChatBedrock
from spectragent.core.llm.providers.browser_use import ChatBrowserUse
from spectragent.core.llm.providers.google import ChatGoogle
from spectragent.core.llm.providers.mistral import ChatMistral
from spectragent.core.llm.providers.oci import ChatOCI
from spectragent.core.llm.providers.ollama import ChatOllama


class _Endpoint(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, reply: dict[str, Any]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.reply = reply
        self.requests: list[dict[str, Any]] = []
        self.opened = 0
        self.closed = 0
        self.condition = threading.Condition()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def all_closed(self) -> bool:
        with self.condition:
            return self.condition.wait_for(lambda: self.closed == self.opened > 0, timeout=2)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Endpoint

    def log_message(self, *_args: Any) -> None:
        pass

    def setup(self) -> None:
        super().setup()
        with self.server.condition:
            self.server.opened += 1

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append(json.loads(body))
        response = json.dumps(self.server.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def finish(self) -> None:
        super().finish()
        with self.server.condition:
            self.server.closed += 1
            self.server.condition.notify_all()


@contextmanager
def _endpoint(reply: dict[str, Any]) -> Iterator[_Endpoint]:
    server = _Endpoint(reply)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def _reply(provider: str) -> dict[str, Any]:
    if provider == "google":
        return {
            "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 1,
                "candidatesTokenCount": 2,
                "totalTokenCount": 3,
            },
        }
    if provider == "ollama":
        return {"message": {"role": "assistant", "content": "hi"}, "done": True}
    if provider == "bedrock":
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
            "usage": {"inputTokens": 1, "outputTokens": 2, "totalTokens": 3},
            "stopReason": "end_turn",
        }
    if provider == "oci":
        return {"chatResponse": {"apiFormat": "COHERE", "text": "hi"}}
    if provider == "browser_use":
        return {"completion": "hi"}
    return {"choices": [{"message": {"content": "hi"}}]}


def _loopback_model(provider: str, endpoint: _Endpoint, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    if provider == "google":
        return ChatGoogle(model="gemini-2.5-flash", api_key="test", base_url=endpoint.url)
    if provider == "ollama":
        return ChatOllama(model="test", host=endpoint.url)
    if provider == "mistral":
        return ChatMistral(model="test", api_key="test", base_url=endpoint.url)
    if provider == "browser_use":
        return ChatBrowserUse(api_key="test", base_url=endpoint.url)
    if provider == "bedrock":
        import boto3

        model = ChatBedrock(model="test")
        monkeypatch.setattr(
            model,
            "_get_client",
            lambda: boto3.client(
                "bedrock-runtime",
                endpoint_url=endpoint.url,
                region_name="us-east-1",
                aws_access_key_id="test",
                aws_secret_access_key="test",
            ),
        )
        return model
    import oci

    class _UnsignedSigner(oci.auth.signers.SecurityTokenSigner):
        def __init__(self) -> None:
            pass

        def __call__(self, request: Any, *args: Any, **kwargs: Any) -> Any:
            return request

    model = ChatOCI(model_id="test", compartment_id="test", service_endpoint=endpoint.url)
    monkeypatch.setattr(
        model,
        "_get_oci_client",
        lambda _clients: oci.generative_ai_inference.GenerativeAiInferenceClient(
            {},
            signer=_UnsignedSigner(),
            service_endpoint=endpoint.url,
            retry_strategy=oci.retry.NoneRetryStrategy(),
        ),
    )
    return model


@pytest.mark.parametrize(
    "provider", ["google", "mistral", "ollama", "bedrock", "oci", "browser_use"]
)
async def test_real_clients_reuse_connections_and_close_between_executions(provider, monkeypatch):
    with _endpoint(_reply(provider)) as endpoint:
        model = _loopback_model(provider, endpoint, monkeypatch)
        factory_name = {
            "google": "get_client",
            "ollama": "get_client",
            "bedrock": "_get_client",
            "oci": "_get_oci_client",
        }.get(provider)
        factory = None
        if factory_name:
            factory = Mock(wraps=getattr(model, factory_name))
            monkeypatch.setattr(model, factory_name, factory)
        assert await model.aclose() == ()
        assert endpoint.opened == 0
        async with model_session(model):
            for _ in range(2):
                result = await model.ainvoke([UserMessage(content="hello")])
                assert result.completion == "hi"
            assert len(endpoint.requests) == 2
            if factory is not None:
                assert factory.call_count == 1
            if provider != "google":
                assert endpoint.opened == 1
                assert endpoint.closed == 0
        assert await asyncio.to_thread(endpoint.all_closed)
        opened = endpoint.opened
        async with model_session(model):
            await model.ainvoke([UserMessage(content="again")])
            assert endpoint.opened > opened
            if factory is not None:
                assert factory.call_count == 2
        assert await asyncio.to_thread(endpoint.all_closed)
        assert await model.aclose() == ()


@pytest.mark.parametrize("provider", ["google", "mistral", "browser_use"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_injected_http_clients_remain_open_for_all_outcomes(provider, outcome):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        entered.set()
        if outcome == "error":
            raise RuntimeError("request failed")
        if outcome == "cancel":
            await release.wait()
        return httpx.Response(200, json=_reply(provider))

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    sync_transport = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    if provider == "google":
        model: Any = ChatGoogle(
            model="gemini-2.5-flash",
            api_key="test",
            max_retries=1,
            http_options=HttpOptions(httpx_async_client=transport, httpx_client=sync_transport),
        )
    elif provider == "mistral":
        model = ChatMistral(model="test", api_key="test", http_client=transport)
    else:
        model = ChatBrowserUse(api_key="test", http_client=transport, max_retries=1)
    try:
        task = asyncio.create_task(model.ainvoke([UserMessage(content="hello")]))
        await asyncio.wait_for(entered.wait(), timeout=2)
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "error":
            with pytest.raises((ModelProviderError, ValueError), match="request failed"):
                await task
        else:
            assert (await task).completion == "hi"
        assert await model.aclose() == ()
        assert await model.aclose() == ()
        assert not transport.is_closed
        assert not sync_transport.is_closed
    finally:
        release.set()
        await transport.aclose()
        sync_transport.close()


@pytest.mark.parametrize("failing_half", ["sync", "async"])
async def test_google_attempts_both_independent_closes_without_changing_reply(
    failing_half, monkeypatch
):
    from google.genai import types

    generate = AsyncMock(
        return_value=types.GenerateContentResponse.model_validate(_reply("google"))
    )
    sync_close = Mock(side_effect=RuntimeError("sync close") if failing_half == "sync" else None)
    async_close = AsyncMock(
        side_effect=RuntimeError("async close") if failing_half == "async" else None
    )
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate), aclose=async_close),
        close=sync_close,
    )
    factory = Mock(return_value=client)
    model = ChatGoogle(model="gemini-2.5-flash", max_retries=3)
    monkeypatch.setattr(model, "get_client", factory)
    result = await model.ainvoke([UserMessage(content="hello")])
    diagnostics = await model.aclose()
    assert result.completion == "hi"
    assert result.usage is not None and result.usage.total_tokens == 3
    assert len(diagnostics) == 1
    assert diagnostics[0].resource == "google." + failing_half
    assert diagnostics[0].status == "failed"
    generate.assert_awaited_once()
    factory.assert_called_once()
    sync_close.assert_called_once()
    async_close.assert_awaited_once()
    assert await model.aclose() == diagnostics
    sync_close.assert_called_once()
    async_close.assert_awaited_once()


async def test_google_partial_client_registration_still_closes_sync_resource(monkeypatch):
    close = Mock()

    class _BrokenClient:
        @property
        def aio(self):
            raise RuntimeError("async initialization")

    client = _BrokenClient()
    client.close = close  # type: ignore[attr-defined]
    model = ChatGoogle(model="test", max_retries=1)
    monkeypatch.setattr(model, "get_client", lambda: client)
    with pytest.raises(ModelProviderError, match="async initialization"):
        await model.ainvoke([UserMessage(content="hello")])
    assert await model.aclose() == ()
    close.assert_called_once()


async def test_ollama_rejects_unsupported_http_client_injection():
    model = ChatOllama(model="test", client_params={"http_client": object()})
    with pytest.raises(ModelProviderError, match="does not support http_client"):
        await model.ainvoke([UserMessage(content="hello")])
    assert await model.aclose() == ()


@pytest.mark.parametrize("provider", ["bedrock", "oci"])
async def test_sync_request_cancellation_does_not_close_client_while_worker_uses_it(
    provider, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def reply(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        assert not closed.is_set()
        if provider == "bedrock":
            return _reply(provider)
        return SimpleNamespace(data=SimpleNamespace(chat_response=SimpleNamespace(text="hi")))

    if provider == "bedrock":
        model: Any = ChatBedrock(model="test")
        monkeypatch.setattr(
            model, "_get_client", lambda: SimpleNamespace(converse=reply, close=closed.set)
        )
    else:
        model = ChatOCI(
            model_id="test", service_endpoint="https://unused.invalid", compartment_id="test"
        )
        monkeypatch.setattr(
            model,
            "_get_oci_client",
            lambda _clients: SimpleNamespace(
                chat=reply, base_client=SimpleNamespace(session=SimpleNamespace(close=closed.set))
            ),
        )
    task = asyncio.create_task(model.ainvoke([UserMessage(content="hello")]))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        diagnostics = await model.aclose(timeout=0.02)
        assert any(item.status == "in_use" for item in diagnostics)
        assert not closed.is_set()
    finally:
        release.set()
    assert await asyncio.to_thread(closed.wait, 2)


async def test_mistral_partial_http_client_initialization_closes_created_transport(monkeypatch):
    transport = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(
        "spectragent.core.llm.providers.mistral.httpx.AsyncHTTPTransport", lambda **_: transport
    )
    monkeypatch.setattr(
        "spectragent.core.llm.providers.mistral.httpx.AsyncClient",
        Mock(side_effect=RuntimeError("client initialization")),
    )
    model = ChatMistral(model="test", api_key="test")
    with pytest.raises(ModelProviderError, match="client initialization"):
        await model.ainvoke([UserMessage(content="hello")])
    assert await model.aclose() == ()
    transport.aclose.assert_awaited_once()


async def test_oci_signer_and_inference_sessions_are_independent(monkeypatch):
    from oci.auth.signers.instance_principals_security_token_signer import (
        InstancePrincipalsSecurityTokenSigner,
    )

    signer_close = Mock(side_effect=RuntimeError("signer cleanup"))
    inference_close = Mock()
    signer = object.__new__(InstancePrincipalsSecurityTokenSigner)
    signer.federation_client = SimpleNamespace(requests_session=SimpleNamespace(close=signer_close))
    monkeypatch.setattr("oci.auth.signers.InstancePrincipalsSecurityTokenSigner", lambda: signer)
    monkeypatch.setattr(
        "spectragent.core.llm.providers.oci.GenerativeAiInferenceClient",
        lambda **_: SimpleNamespace(
            chat=lambda _: SimpleNamespace(
                data=SimpleNamespace(chat_response=SimpleNamespace(text="hi"))
            ),
            base_client=SimpleNamespace(session=SimpleNamespace(close=inference_close)),
        ),
    )
    model = ChatOCI(
        model_id="test",
        service_endpoint="https://unused.invalid",
        compartment_id="test",
        auth_type="INSTANCE_PRINCIPAL",
    )
    assert (await model.ainvoke([UserMessage(content="hello")])).completion == "hi"
    diagnostics = await model.aclose()
    assert len(diagnostics) == 1 and diagnostics[0].resource == "oci.signer.federation"
    signer_close.assert_called_once()
    inference_close.assert_called_once()


class _Shape(BaseModel):
    number: int


@pytest.mark.parametrize("provider", ["mistral", "ollama", "browser_use"])
async def test_invalid_structured_reply_still_releases_owned_connection(provider, monkeypatch):
    reply = _reply(provider)
    if provider == "ollama":
        reply["message"]["content"] = '{"number":"bad"}'
    elif provider == "browser_use":
        reply["completion"] = {"number": "bad"}
    else:
        reply["choices"][0]["message"]["content"] = '{"number":"bad"}'
    with _endpoint(reply) as endpoint:
        model = _loopback_model(provider, endpoint, monkeypatch)
        with pytest.raises(ModelProviderError):
            async with model_session(model):
                await model.ainvoke([UserMessage(content="hello")], output_format=_Shape)
        assert await asyncio.to_thread(endpoint.all_closed)


@pytest.mark.parametrize("placement", ["transport", "mounts"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_ollama_sdk_closes_but_borrowed_transport_stays_open(placement, outcome, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        entered.set()
        if outcome == "error":
            return httpx.Response(500, json={"error": "request failed"})
        if outcome == "cancel":
            await release.wait()
        return httpx.Response(200, json=_reply("ollama"))

    transport = httpx.MockTransport(handle)
    close_transport = AsyncMock(wraps=transport.aclose)
    monkeypatch.setattr(transport, "aclose", close_transport)
    params: dict[str, Any] = (
        {"transport": transport} if placement == "transport" else {"mounts": {"all://": transport}}
    )
    params["trust_env"] = False
    model = ChatOllama(model="test", client_params=params)
    original_factory = model.get_client
    closes = []

    def factory():
        client = original_factory()
        close = AsyncMock(wraps=client.close)
        monkeypatch.setattr(client, "close", close)
        closes.append(close)
        return client

    monkeypatch.setattr(model, "get_client", factory)
    task = asyncio.create_task(model.ainvoke([UserMessage(content="hello")]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "error":
            with pytest.raises(ModelProviderError, match="request failed"):
                await task
        else:
            assert (await task).completion == "hi"
        assert await model.aclose() == ()
        assert await model.aclose() == ()
        assert len(closes) == 1
        closes[0].assert_awaited_once()
        close_transport.assert_not_awaited()
    finally:
        release.set()
        await transport.aclose()


@pytest.mark.parametrize("arguments", ["client_args", "async_client_args"])
@pytest.mark.parametrize("key", ["transport", "mounts", "connector"])
async def test_google_rejects_nested_transport_ownership_it_cannot_honor(arguments, key):
    options = HttpOptions.model_validate({arguments: {key: object()}})
    model = ChatGoogle(model="test", api_key="test", http_options=options, max_retries=1)
    with pytest.raises(ModelProviderError, match="cannot borrow transports"):
        await model.ainvoke([UserMessage(content="hello")])
    assert await model.aclose() == ()


async def test_google_retries_reuse_the_execution_client(monkeypatch):
    from google.genai import types

    generate = AsyncMock(
        side_effect=[
            ModelProviderError(message="temporarily unavailable", status_code=503, model="test"),
            types.GenerateContentResponse.model_validate(_reply("google")),
        ]
    )
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate), aclose=AsyncMock()),
        close=Mock(),
    )
    factory = Mock(return_value=client)
    model = ChatGoogle(model="gemini-2.5-flash", max_retries=2, retry_base_delay=0)
    monkeypatch.setattr(model, "get_client", factory)
    async with model_session(model):
        assert (await model.ainvoke([UserMessage(content="hello")])).completion == "hi"
    factory.assert_called_once()
    assert generate.await_count == 2
    client.close.assert_called_once()
    client.aio.aclose.assert_awaited_once()


async def test_browser_use_retry_reuses_owned_client_and_cleanup_does_not_retry_success(
    monkeypatch,
):
    requests = 0

    def handle(request):
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        return httpx.Response(200, json={"completion": "hi"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    original_close = client.aclose
    close = AsyncMock(side_effect=RuntimeError("close failed"))
    monkeypatch.setattr(client, "aclose", close)
    factory = Mock(return_value=client)
    monkeypatch.setattr("spectragent.core.llm.providers.browser_use.httpx.AsyncClient", factory)
    model = ChatBrowserUse(api_key="test", max_retries=2, retry_base_delay=0)
    try:
        result = await model.ainvoke([UserMessage(content="hello")])
        diagnostics = await model.aclose()
        assert result.completion == "hi"
        assert len(diagnostics) == 1 and diagnostics[0].status == "failed"
        assert requests == 2
        factory.assert_called_once()
        close.assert_awaited_once()
    finally:
        await original_close()


@pytest.mark.parametrize("kind", ["federation", "ephemeral", "nested"])
async def test_oci_resource_principal_signer_sessions_close_independently(kind, monkeypatch):
    from oci.auth.signers.instance_principals_security_token_signer import (
        InstancePrincipalsSecurityTokenSigner,
    )
    from oci.auth.signers.resource_principals_signer import (
        EphemeralResourcePrincipalV21Signer,
        NestedResourcePrincipals,
        ResourcePrincipalsFederationSigner,
    )

    closes = []

    def session():
        close = Mock()
        closes.append(close)
        return SimpleNamespace(close=close)

    instance = object.__new__(InstancePrincipalsSecurityTokenSigner)
    instance.federation_client = SimpleNamespace(requests_session=session())
    if kind == "federation":
        signer = object.__new__(ResourcePrincipalsFederationSigner)
        monkeypatch.setattr(
            signer, "base_client", SimpleNamespace(session=session()), raising=False
        )
        signer.instance_principal_signer = instance
    elif kind == "ephemeral":
        # This signer has no nested instance-principal client.
        closes.clear()
        signer = object.__new__(EphemeralResourcePrincipalV21Signer)
        monkeypatch.setattr(
            signer, "base_client", SimpleNamespace(session=session()), raising=False
        )
    else:
        signer = object.__new__(NestedResourcePrincipals)
        monkeypatch.setattr(
            signer, "base_client", SimpleNamespace(session=session()), raising=False
        )
        signer.sub_resource_rp_signer = instance
    monkeypatch.setattr("oci.auth.signers.get_resource_principals_signer", lambda: signer)
    monkeypatch.setattr(
        "spectragent.core.llm.providers.oci.GenerativeAiInferenceClient",
        Mock(side_effect=RuntimeError("inference initialization")),
    )
    model = ChatOCI(
        model_id="test",
        service_endpoint="https://unused.invalid",
        compartment_id="test",
        auth_type="RESOURCE_PRINCIPAL",
    )
    with pytest.raises(ModelProviderError, match="inference initialization"):
        await model.ainvoke([UserMessage(content="hello")])
    assert await model.aclose() == ()
    for close in closes:
        close.assert_called_once()


def test_browser_use_rejects_unsupported_transport_injection():
    arguments: dict[str, Any] = {"api_key": "test", "transport": object()}
    with pytest.raises(TypeError, match="transport"):
        ChatBrowserUse(**arguments)


async def test_oci_signer_created_by_sdk_configuration_is_owned(monkeypatch):
    from oci.auth.signers.instance_principals_security_token_signer import (
        InstancePrincipalsSecurityTokenSigner,
    )

    signer_close = Mock()
    inference_close = Mock()
    signer = object.__new__(InstancePrincipalsSecurityTokenSigner)
    signer.federation_client = SimpleNamespace(requests_session=SimpleNamespace(close=signer_close))
    monkeypatch.setattr(
        "oci.config.from_file", lambda *_: {"authentication_type": "instance_principal"}
    )
    factory = Mock(
        return_value=SimpleNamespace(
            chat=lambda _: SimpleNamespace(
                data=SimpleNamespace(chat_response=SimpleNamespace(text="hi"))
            ),
            base_client=SimpleNamespace(
                signer=signer, session=SimpleNamespace(close=inference_close)
            ),
        )
    )
    monkeypatch.setattr("spectragent.core.llm.providers.oci.GenerativeAiInferenceClient", factory)
    model = ChatOCI(
        model_id="test",
        service_endpoint="https://unused.invalid",
        compartment_id="test",
    )
    async with model_session(model):
        assert (await model.ainvoke([UserMessage(content="hello")])).completion == "hi"
    assert "signer" not in factory.call_args.kwargs
    signer_close.assert_called_once()
    inference_close.assert_called_once()

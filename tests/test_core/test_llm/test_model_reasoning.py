"""Every provider records the reasoning its reply already carries, and asks for none.

The user's ruling of 2026-10-14: record the model's hidden reasoning, whatever each
provider returns, in the call record's existing ``thinking`` field; do not change the
requests sent to the model; do not feed the reasoning back; the agent does not read it.
``docs/architecture_design.md`` §4.7 states the rule.

Pinned here, for every registered provider and every return path (text, structured
output, tool calls):

- the reasoning a reply carries lands on ``ChatInvokeCompletion.thinking``, and the
  opaque form some providers return on ``redacted_thinking``;
- a reply that carries none, or only whitespace, records ``None``;
- the reasoning is never part of ``completion``, which is the one field the Loop puts
  into memory and so into the next prompt.

And, for the OpenAI-compatible and Anthropic routes, that each request is exactly what
these providers sent before they recorded reasoning (the expected bodies were captured
from that code). A difference is a change to what is sent, and needs a reason of its own.

Most stubs are the real SDK behind ``httpx.MockTransport``, so the wire JSON goes through
the SDK's own parsing: the reasoning fields gateways add are not in the OpenAI SDK's
message model, and Gemini's ``thought`` flag is a JSON key the SDK has to map.
"""

from __future__ import annotations

import base64
import importlib
import json
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

import spectragent.core.llm.providers as providers_pkg
from spectragent.core.llm._registrar import LLMRegistrar
from spectragent.core.llm.exceptions import StructuredOutputValidationError
from spectragent.core.llm.messages import SystemMessage, UserMessage
from spectragent.core.llm.providers.anthropic import ChatAnthropic
from spectragent.core.llm.providers.anthropic_aws import ChatAnthropicAWS
from spectragent.core.llm.providers.bedrock import ChatBedrock
from spectragent.core.llm.providers.browser_use import ChatBrowserUse
from spectragent.core.llm.providers.google import ChatGoogle
from spectragent.core.llm.providers.mistral import ChatMistral
from spectragent.core.llm.providers.oci import ChatOCI
from spectragent.core.llm.providers.ollama import ChatOllama
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI
from spectragent.core.llm.providers.openai_cerebras import ChatCerebras
from spectragent.core.llm.providers.openai_deepseek import ChatDeepSeek
from spectragent.core.llm.providers.openai_groq import ChatGroq
from spectragent.core.llm.providers.openai_openrouter import ChatOpenRouter
from spectragent.core.llm.providers.openai_vercel import ChatVercel
from spectragent.core.llm.tools import ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion, join_reasoning
from tests._helpers.llm_clients import FakeClient

_THINKING = "The search box is the second field, so I type there first."
_REDACTED = "EuYBCkQYAiJAopaque-reasoning-blob"
_BLANK = " \n\t "
_GATEWAY = "https://gateway.test/v1"


class Reply(BaseModel):
    answer: str


class _Args(BaseModel):
    city: str


_TOOLS = [ToolDefinition.from_model(_Args, name="lookup", description="Look a city up")]
_PROMPT = [SystemMessage(content="You are terse."), UserMessage(content="Say hi.")]
_REPLY_TEXT = "Hi."
_REPLY_JSON = '{"answer": "Hi."}'
_HI = Reply(answer="Hi.")


def _import_every_provider() -> None:
    """Populate the registry: the case table is checked against it."""
    for module in pkgutil.iter_modules(providers_pkg.__path__):
        if not module.name.startswith("_"):
            importlib.import_module(f"{providers_pkg.__name__}.{module.name}")


_import_every_provider()


# ── The one rule every provider goes through ──────────────────────────


class TestJoiningReasoning:
    def test_pieces_keep_their_order_one_newline_apart(self) -> None:
        assert join_reasoning(["first", "second"]) == "first\nsecond"

    def test_a_kept_piece_is_not_trimmed(self) -> None:
        assert join_reasoning(["  indented\n"]) == "  indented\n"

    def test_blank_pieces_and_non_strings_are_dropped(self) -> None:
        assert join_reasoning(["", _BLANK, None, b"bytes", 7, "kept"]) == "kept"

    def test_nothing_left_is_none_not_an_empty_string(self) -> None:
        assert join_reasoning([]) is None
        assert join_reasoning(["", _BLANK, None]) is None


# ── The wire, as each SDK sees it ─────────────────────────────────────


class _Wire:
    """One scripted HTTP answer, and every request that reached it."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.answer)

    @property
    def body(self) -> Any:
        (request,) = self.requests
        return json.loads(request.content)


def _chat_completion(
    content: str | None,
    *,
    reasoning: str | None,
    field: str = "reasoning_content",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """An OpenAI-compatible chat completion, its reasoning under *field*."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message[field] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "stub",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def _tool_call(name: str, arguments: str) -> dict[str, Any]:
    return {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}


def _responses_reply(
    text: str,
    *,
    content: list[str] | None = None,
    summary: list[str] | None = None,
    encrypted: str | None = None,
) -> dict[str, Any]:
    """A Responses API reply, with one reasoning item when there is anything to put in it."""
    output: list[dict[str, Any]] = []
    if content is not None or summary or encrypted is not None:
        item: dict[str, Any] = {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": s} for s in summary or []],
        }
        if content is not None:
            item["content"] = [{"type": "reasoning_text", "text": c} for c in content]
        if encrypted is not None:
            item["encrypted_content"] = encrypted
        output.append(item)
    output.append(
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    )
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-5.1-codex",
        "status": "completed",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }


def _anthropic_message(
    reply: list[dict[str, Any]], *, thinking: str | None, redacted: str | None
) -> dict[str, Any]:
    """An Anthropic Messages reply: the reasoning blocks first, as the API orders them."""
    blocks: list[dict[str, Any]] = []
    if thinking is not None:
        blocks.append({"type": "thinking", "thinking": thinking, "signature": "sig"})
    if redacted is not None:
        blocks.append({"type": "redacted_thinking", "data": redacted})
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "stub",
        "content": [*blocks, *reply],
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in reply) else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }


_ANTHROPIC_TEXT = [{"type": "text", "text": _REPLY_TEXT}]
_ANTHROPIC_JSON = [{"type": "text", "text": _REPLY_JSON}]
_ANTHROPIC_SCHEMA_TOOL = [
    {"type": "tool_use", "id": "tu_1", "name": "Reply", "input": {"answer": "Hi."}}
]
_ANTHROPIC_LOOKUP = [
    {"type": "tool_use", "id": "tu_1", "name": "lookup", "input": {"city": "Paris"}}
]


def _gemini_reply(reply_parts: list[dict[str, Any]], *, thinking: str | None) -> dict[str, Any]:
    parts = [{"text": thinking, "thought": True}] if thinking is not None else []
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [*parts, *reply_parts]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
    }


def _ollama_reply(content: str, *, thinking: str | None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if thinking is not None:
        message["thinking"] = thinking
    return {
        "model": "qwen3",
        "created_at": "2026-10-14T00:00:00Z",
        "done": True,
        "done_reason": "stop",
        "message": message,
        "prompt_eval_count": 3,
        "eval_count": 2,
    }


def _mistral_reply(text: str, *, thinking: str | None) -> dict[str, Any]:
    """A reasoning model's content: a thinking chunk, then the reply's text chunk."""
    chunks: list[dict[str, Any]] = []
    if thinking is not None:
        chunks.append({"type": "thinking", "thinking": [{"type": "text", "text": thinking}]})
    chunks.append({"type": "text", "text": text})
    message = {"role": "assistant", "content": chunks}
    return {
        "id": "m1",
        "object": "chat.completion",
        "model": "magistral-medium-latest",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


# ── Providers, pointed at the wire ────────────────────────────────────


def _on_sdk(chat: Any, wire: _Wire, sdk: str = "openai") -> Any:
    """*chat* with the SDK client it builds per request pointed at *wire*."""
    from groq import AsyncGroq
    from openai import AsyncAzureOpenAI, AsyncOpenAI

    client = wire.client
    if sdk == "azure":
        chat.get_client = lambda: AsyncAzureOpenAI(  # type: ignore[method-assign]
            api_key="stub",
            api_version="2024-12-01-preview",
            azure_endpoint="https://azure.test",
            max_retries=0,
            http_client=client(),
        )
    elif sdk == "groq":
        chat.get_client = lambda: AsyncGroq(  # type: ignore[method-assign]
            api_key="stub", max_retries=0, http_client=client()
        )
    else:
        chat.get_client = lambda: AsyncOpenAI(  # type: ignore[method-assign]
            api_key="stub", base_url=_GATEWAY, max_retries=0, http_client=client()
        )
    return chat


def _anthropic(wire: _Wire, **fields: Any) -> ChatAnthropic:
    return ChatAnthropic(
        model="claude-sonnet-4-5",
        api_key="stub",
        base_url="https://gateway.test",
        max_retries=0,
        http_client=wire.client(),
        **fields,
    )


def _gemini(wire: _Wire, **fields: Any) -> ChatGoogle:
    from google.genai import types

    return ChatGoogle(
        model="gemini-2.5-flash",
        api_key="stub",
        max_retries=1,
        http_options=types.HttpOptions(
            base_url="https://gateway.test", httpx_async_client=wire.client()
        ),
        **fields,
    )


def _oci(text: str, *, thinking: str | None, provider: str = "meta") -> ChatOCI:
    """The OCI SDK's own response models, stubbed above the signed request."""
    from oci.generative_ai_inference import models

    usage = models.Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    if provider == "cohere":
        chat_response: Any = models.CohereChatResponse(api_format="COHERE", text=text, usage=usage)
    else:
        message = models.AssistantMessage(
            role="ASSISTANT",
            content=[models.TextContent(type="TEXT", text=text)],
            reasoning_content=thinking,
        )
        chat_response = models.GenericChatResponse(
            api_format="GENERIC",
            choices=[models.ChatChoice(index=0, message=message, finish_reason="stop")],
            usage=usage,
        )
    chat = ChatOCI(
        model_id="ocid1.generativeaimodel.oc1..stub",
        service_endpoint="https://oci.test",
        compartment_id="ocid1.compartment.oc1..stub",
        oci_provider=provider,
    )

    async def _request(_messages: Any) -> Any:
        return SimpleNamespace(data=SimpleNamespace(chat_response=chat_response))

    chat._make_request = _request  # type: ignore[method-assign]
    return chat


def _bedrock(final: dict[str, Any], *, thinking: str | None, redacted: str | None) -> ChatBedrock:
    """Converse answers in dicts; redacted reasoning arrives as bytes."""
    content: list[dict[str, Any]] = []
    if thinking is not None:
        content.append(
            {"reasoningContent": {"reasoningText": {"text": thinking, "signature": "s"}}}
        )
    if redacted is not None:
        content.append({"reasoningContent": {"redactedContent": redacted.encode()}})
    answer = {
        "output": {"message": {"role": "assistant", "content": [*content, final]}},
        "usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
        "stopReason": "end_turn",
    }
    chat = ChatBedrock(model="anthropic.claude-sonnet-4-5")
    chat._get_client = lambda: SimpleNamespace(converse=lambda **_kw: answer)  # type: ignore[method-assign]
    return chat


# ── Routes: one provider path, answering with the reasoning it is handed ──

#: ``(thinking, redacted)`` → the completion of a reply that carried them
#: (``None``: the reply carried no such thing).
Run = Callable[[str | None, str | None], Awaitable[ChatInvokeCompletion[Any]]]


async def _ask(chat: Any, output_format: type[BaseModel] | None, tools: bool) -> Any:
    if tools:
        return await chat.ainvoke(_PROMPT, output_format, tools=_TOOLS)
    return await chat.ainvoke(_PROMPT, output_format)


def _openai_route(
    make: Callable[[], Any],
    *,
    reply: str | None,
    output_format: type[BaseModel] | None = None,
    field: str = "reasoning_content",
    tool_call: dict[str, Any] | None = None,
    sdk: str = "openai",
) -> Run:
    async def run(thinking: str | None, _redacted: str | None) -> Any:
        tool_calls = [tool_call] if tool_call else None
        wire = _Wire(
            _chat_completion(reply, reasoning=thinking, field=field, tool_calls=tool_calls)
        )
        chat = _on_sdk(make(), wire, sdk)
        return await _ask(
            chat, output_format, tools=tool_call is not None and output_format is None
        )

    return run


def _responses_route(*, summary: list[str] | None = None) -> Run:
    async def run(thinking: str | None, redacted: str | None) -> Any:
        content = [thinking] if thinking is not None else None
        wire = _Wire(
            _responses_reply(_REPLY_TEXT, content=content, summary=summary, encrypted=redacted)
        )
        chat = ChatAzureOpenAI(
            model="gpt-5.1-codex", api_key="stub", azure_endpoint="https://azure.test"
        )
        assert chat._should_use_responses_api(), "this case no longer exercises the Responses API"
        return await _on_sdk(chat, wire, "azure").ainvoke(_PROMPT)

    return run


def _anthropic_route(
    reply: list[dict[str, Any]],
    *,
    output_format: type[BaseModel] | None = None,
    tools: bool = False,
    **fields: Any,
) -> Run:
    async def run(thinking: str | None, redacted: str | None) -> Any:
        wire = _Wire(_anthropic_message(reply, thinking=thinking, redacted=redacted))
        return await _ask(_anthropic(wire, **fields), output_format, tools)

    return run


async def _anthropic_aws(thinking: str | None, redacted: str | None) -> Any:
    """A real ``Message``: the AWS client signs requests, so it is stubbed above the wire."""
    from anthropic.types import Message

    message = Message.model_validate(
        _anthropic_message(_ANTHROPIC_TEXT, thinking=thinking, redacted=redacted)
    )

    class _Messages:
        async def create(self, **_kw: Any) -> Any:
            return message

    chat = ChatAnthropicAWS(model="claude-sonnet-4-5", aws_region="us-east-1")
    chat.get_client = lambda: FakeClient(messages=_Messages())  # type: ignore[method-assign]
    return await chat.ainvoke(_PROMPT)


def _gemini_route(
    reply: list[dict[str, Any]],
    *,
    output_format: type[BaseModel] | None = None,
    tools: bool = False,
    **fields: Any,
) -> Run:
    async def run(thinking: str | None, _redacted: str | None) -> Any:
        wire = _Wire(_gemini_reply(reply, thinking=thinking))
        return await _ask(_gemini(wire, **fields), output_format, tools)

    return run


def _ollama_route(content: str, output_format: type[BaseModel] | None = None) -> Run:
    async def run(thinking: str | None, _redacted: str | None) -> Any:
        wire = _Wire(_ollama_reply(content, thinking=thinking))
        chat = ChatOllama(
            model="qwen3", host="http://ollama.test", client_params={"transport": wire.transport()}
        )
        return await _ask(chat, output_format, tools=False)

    return run


def _mistral_route(text: str, output_format: type[BaseModel] | None = None) -> Run:
    async def run(thinking: str | None, _redacted: str | None) -> Any:
        wire = _Wire(_mistral_reply(text, thinking=thinking))
        chat = ChatMistral(
            model="magistral-medium-latest", api_key="stub", http_client=wire.client()
        )
        return await _ask(chat, output_format, tools=False)

    return run


def _bedrock_route(final: dict[str, Any], output_format: type[BaseModel] | None = None) -> Run:
    async def run(thinking: str | None, redacted: str | None) -> Any:
        chat = _bedrock(final, thinking=thinking, redacted=redacted)
        return await _ask(chat, output_format, tools=False)

    return run


def _oci_route(text: str, output_format: type[BaseModel] | None = None) -> Run:
    async def run(thinking: str | None, _redacted: str | None) -> Any:
        return await _ask(_oci(text, thinking=thinking), output_format, tools=False)

    return run


# ── One case per provider path ────────────────────────────────────────


def _as_is(value: str) -> str:
    return value


def _base64_of(value: str) -> str:
    return base64.b64encode(value.encode()).decode("ascii")


@dataclass(frozen=True)
class _Case:
    """One provider path, answering with whatever reasoning the test hands it.

    ``key`` is the registry key the case covers, so a provider added to
    ``providers/`` without a case fails :func:`test_every_registered_provider_is_covered`.
    ``reply`` is what ``completion`` must hold. ``redacted`` says how the provider
    records reasoning that arrived in opaque form, ``None`` when it has no such form.
    """

    id: str
    key: str
    run: Run
    reply: Any
    redacted: Callable[[str], str] | None = None
    tool_calls: bool = False


_LOOKUP = _tool_call("lookup", '{"city": "Paris"}')

_CASES: list[_Case] = [
    _Case(
        "openai.direct[text]",
        "openai.direct",
        _openai_route(lambda: ChatOpenAI(model="qwen3.8-flash", api_key="stub"), reply=_REPLY_TEXT),
        _REPLY_TEXT,
    ),
    _Case(
        "openai.direct[structured]",
        "openai.direct",
        _openai_route(
            lambda: ChatOpenAI(model="qwen3.8-flash", api_key="stub"),
            reply=_REPLY_JSON,
            output_format=Reply,
        ),
        _HI,
    ),
    _Case(
        "openai.direct[tool call]",
        "openai.direct",
        _openai_route(
            lambda: ChatOpenAI(model="qwen3.8-flash", api_key="stub"), reply=None, tool_call=_LOOKUP
        ),
        "",
        tool_calls=True,
    ),
    _Case(
        "openai.openrouter[text]",
        "openai.openrouter",
        _openai_route(
            lambda: ChatOpenRouter(model="deepseek/deepseek-r1", api_key="stub"),
            reply=_REPLY_TEXT,
            field="reasoning",
        ),
        _REPLY_TEXT,
    ),
    _Case(
        "openai.deepseek[text]",
        "openai.deepseek",
        _openai_route(
            lambda: ChatDeepSeek(model="deepseek-reasoner", api_key="stub"), reply=_REPLY_TEXT
        ),
        _REPLY_TEXT,
    ),
    _Case(
        # Forced tool calling: the reply is a tool call, the reasoning sits beside it.
        "openai.deepseek[structured]",
        "openai.deepseek",
        _openai_route(
            lambda: ChatDeepSeek(model="deepseek-chat", api_key="stub"),
            reply=None,
            output_format=Reply,
            tool_call=_tool_call("Reply", _REPLY_JSON),
        ),
        _HI,
    ),
    _Case(
        "openai.cerebras[structured]",
        "openai.cerebras",
        _openai_route(
            lambda: ChatCerebras(model="gpt-oss-120b", api_key="stub"),
            reply=_REPLY_JSON,
            output_format=Reply,
            field="reasoning",
        ),
        _HI,
    ),
    _Case(
        "openai.vercel[prompt]",
        "openai.vercel",
        _openai_route(
            lambda: ChatVercel(model="anthropic/claude-sonnet-4-5", api_key="stub"),
            reply=_REPLY_JSON,
            output_format=Reply,
            field="reasoning",
        ),
        _HI,
    ),
    _Case(
        "openai.vercel[response_format]",
        "openai.vercel",
        _openai_route(
            lambda: ChatVercel(model="openai/gpt-4o", api_key="stub"),
            reply=_REPLY_JSON,
            output_format=Reply,
            field="reasoning",
        ),
        _HI,
    ),
    _Case(
        "openai.azure[chat]",
        "openai.azure",
        _openai_route(
            lambda: ChatAzureOpenAI(
                model="gpt-4o",
                api_key="stub",
                azure_endpoint="https://azure.test",
                use_responses_api="false",
            ),
            reply=_REPLY_TEXT,
            sdk="azure",
        ),
        _REPLY_TEXT,
    ),
    _Case(
        "openai.azure[responses]", "openai.azure", _responses_route(), _REPLY_TEXT, redacted=_as_is
    ),
    _Case(
        # Groq's SDK declares the field: ``message.reasoning``.
        "openai.groq[text]",
        "openai.groq",
        _openai_route(
            lambda: ChatGroq(model="openai/gpt-oss-20b", api_key="stub"),
            reply=_REPLY_TEXT,
            field="reasoning",
            sdk="groq",
        ),
        _REPLY_TEXT,
    ),
    _Case(
        "openai.groq[structured]",
        "openai.groq",
        _openai_route(
            lambda: ChatGroq(model="openai/gpt-oss-20b", api_key="stub"),
            reply=_REPLY_JSON,
            output_format=Reply,
            field="reasoning",
            sdk="groq",
        ),
        _HI,
    ),
    _Case(
        "anthropic.direct[text]",
        "anthropic.direct",
        _anthropic_route(_ANTHROPIC_TEXT),
        _REPLY_TEXT,
        redacted=_as_is,
    ),
    _Case(
        "anthropic.direct[native]",
        "anthropic.direct",
        _anthropic_route(_ANTHROPIC_JSON, output_format=Reply),
        _HI,
        redacted=_as_is,
    ),
    _Case(
        "anthropic.direct[forced tool]",
        "anthropic.direct",
        _anthropic_route(
            _ANTHROPIC_SCHEMA_TOOL, output_format=Reply, use_native_structured_output=False
        ),
        _HI,
        redacted=_as_is,
    ),
    _Case(
        "anthropic.direct[tool call]",
        "anthropic.direct",
        _anthropic_route(_ANTHROPIC_LOOKUP, tools=True),
        "",
        redacted=_as_is,
        tool_calls=True,
    ),
    _Case("anthropic.aws", "anthropic.aws", _anthropic_aws, _REPLY_TEXT, redacted=_as_is),
    _Case(
        "google.default[text]",
        "google.default",
        _gemini_route([{"text": _REPLY_TEXT}]),
        _REPLY_TEXT,
    ),
    _Case(
        "google.default[native]",
        "google.default",
        _gemini_route([{"text": _REPLY_JSON}], output_format=Reply),
        _HI,
    ),
    _Case(
        "google.default[prompt]",
        "google.default",
        _gemini_route(
            [{"text": _REPLY_JSON}], output_format=Reply, supports_structured_output=False
        ),
        _HI,
    ),
    _Case(
        "google.default[tool call]",
        "google.default",
        _gemini_route(
            [{"functionCall": {"name": "lookup", "args": {"city": "Paris"}}}], tools=True
        ),
        "",
        tool_calls=True,
    ),
    _Case("ollama.default[text]", "ollama.default", _ollama_route(_REPLY_TEXT), _REPLY_TEXT),
    _Case("ollama.default[structured]", "ollama.default", _ollama_route(_REPLY_JSON, Reply), _HI),
    _Case("mistral.default[text]", "mistral.default", _mistral_route(_REPLY_TEXT), _REPLY_TEXT),
    _Case(
        "mistral.default[structured]", "mistral.default", _mistral_route(_REPLY_JSON, Reply), _HI
    ),
    _Case(
        "bedrock.default[text]",
        "bedrock.default",
        _bedrock_route({"text": _REPLY_TEXT}),
        _REPLY_TEXT,
        redacted=_base64_of,
    ),
    _Case(
        "bedrock.default[structured]",
        "bedrock.default",
        _bedrock_route(
            {"toolUse": {"toolUseId": "t1", "name": "extract_reply", "input": {"answer": "Hi."}}},
            Reply,
        ),
        _HI,
        redacted=_base64_of,
    ),
    _Case("oci.default[text]", "oci.default", _oci_route(_REPLY_TEXT), _REPLY_TEXT),
    _Case("oci.default[structured]", "oci.default", _oci_route(_REPLY_JSON, Reply), _HI),
]

#: Providers whose reply has no reasoning to read, and why.
_CARRIES_NONE = {
    "browser_use.default": "the endpoint's reply has only `completion` and `usage`",
}


def test_every_registered_provider_is_covered() -> None:
    """A new provider declares how it reads reasoning, or that its reply carries none."""
    uncovered = set(LLMRegistrar.get_all()) - {case.key for case in _CASES} - set(_CARRIES_NONE)
    assert uncovered == set(), (
        "These providers have no reasoning case: add one that answers with the reasoning "
        "their response carries, or name them in _CARRIES_NONE with the reason: "
        f"{sorted(uncovered)}"
    )


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
async def test_the_reasoning_a_reply_carried_is_recorded(case: _Case) -> None:
    completion = await case.run(_THINKING, _REDACTED if case.redacted else None)

    assert completion.thinking == _THINKING
    assert completion.redacted_thinking == (case.redacted(_REDACTED) if case.redacted else None)
    # The reply is the reply: the reasoning is beside it, not in it.
    assert completion.completion == case.reply
    assert bool(completion.tool_calls) is case.tool_calls


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
async def test_a_reply_without_reasoning_records_none(case: _Case) -> None:
    completion = await case.run(None, None)

    assert completion.thinking is None
    assert completion.redacted_thinking is None
    assert completion.completion == case.reply


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
async def test_blank_reasoning_records_none(case: _Case) -> None:
    completion = await case.run(_BLANK, "" if case.redacted else None)

    assert completion.thinking is None
    assert completion.redacted_thinking is None


async def test_the_browser_use_endpoint_records_none() -> None:
    chat = ChatBrowserUse(api_key="stub")

    async def _request(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"completion": _REPLY_TEXT, "usage": None}

    chat._make_request = _request  # type: ignore[method-assign]
    completion = await chat.ainvoke(_PROMPT)
    assert (completion.thinking, completion.redacted_thinking) == (None, None)


# ── Where a reply keeps its reasoning apart ───────────────────────────


class TestTheReplyNeverHoldsTheReasoning:
    """The places reasoning could have leaked into ``completion``, which the Loop remembers."""

    async def test_anthropic_structured_reply_after_a_thinking_block(self) -> None:
        """The reply is the first block that is not reasoning.

        This path took ``content[0]``. With a thinking block first, that was the
        block itself: the stringified block, reasoning and all, failed validation as
        ``raw_output`` and the Loop fed it back to the model as its own reply.
        """
        wire = _Wire(_anthropic_message(_ANTHROPIC_JSON, thinking=_THINKING, redacted=None))
        completion = await _anthropic(wire).ainvoke(_PROMPT, Reply)
        assert completion.completion == _HI
        assert completion.thinking == _THINKING

    async def test_anthropic_reply_of_reasoning_alone_is_empty(self) -> None:
        """Nothing but reasoning: the reply is empty, not the stringified block."""
        wire = _Wire(_anthropic_message([], thinking=_THINKING, redacted=_REDACTED))
        completion = await _anthropic(wire).ainvoke(_PROMPT)
        assert completion.completion == ""
        assert (completion.thinking, completion.redacted_thinking) == (_THINKING, _REDACTED)

    async def test_anthropic_structured_reply_of_reasoning_alone_holds_no_reasoning(self) -> None:
        """Refused, and what goes back to the model as its reply holds no reasoning."""
        wire = _Wire(_anthropic_message([], thinking=_THINKING, redacted=None))
        with pytest.raises(StructuredOutputValidationError) as refused:
            await _anthropic(wire).ainvoke(_PROMPT, Reply)
        assert refused.value.raw_output is not None
        assert _THINKING not in refused.value.raw_output

    async def test_anthropic_blocks_join_in_order(self) -> None:
        reply = _anthropic_message([], thinking=None, redacted=None)
        reply["content"] = [
            {"type": "thinking", "thinking": "first", "signature": "s1"},
            {"type": "redacted_thinking", "data": "opaque-1"},
            {"type": "text", "text": _REPLY_TEXT},
            {"type": "thinking", "thinking": "second", "signature": "s2"},
            {"type": "redacted_thinking", "data": "opaque-2"},
        ]
        completion = await _anthropic(_Wire(reply)).ainvoke(_PROMPT)
        assert completion.completion == _REPLY_TEXT
        assert completion.thinking == "first\nsecond"
        assert completion.redacted_thinking == "opaque-1\nopaque-2"

    async def test_a_cohere_reply_on_oci_has_no_reasoning_field(self) -> None:
        chat = _oci(_REPLY_TEXT, thinking=None, provider="cohere")
        completion = await chat.ainvoke(_PROMPT)
        assert completion.completion == _REPLY_TEXT
        assert completion.thinking is None


class TestTheResponsesApi:
    async def test_the_summary_stands_in_when_the_reasoning_text_is_absent(self) -> None:
        run = _responses_route(summary=["Looked at the form.", "Typed."])
        completion = await run(None, None)
        assert completion.thinking == "Looked at the form.\nTyped."

    async def test_the_reasoning_text_is_preferred_over_its_summary(self) -> None:
        completion = await _responses_route(summary=["A summary."])(_THINKING, None)
        assert completion.thinking == _THINKING


# ── The request is the one sent before reasoning was recorded ─────────

_MESSAGES = [
    {"content": "You are terse.", "role": "system"},
    {"content": "Say hi.", "role": "user"},
]
_REPLY_SCHEMA = {
    "additionalProperties": False,
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "type": "object",
}
_LOOKUP_SCHEMA = {
    "additionalProperties": False,
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "type": "object",
}
_OPENAI_BODY = {
    "frequency_penalty": 0.3,
    "max_completion_tokens": 4096,
    "messages": _MESSAGES,
    "model": "qwen3.8-flash",
    "reasoning_effort": "low",
    "temperature": 0.2,
}
_ANTHROPIC_BODY = {
    "max_tokens": 8192,
    "messages": [{"content": "Say hi.", "role": "user"}],
    "model": "claude-sonnet-4-5",
    "system": "You are terse.",
}


def _openai_gateway(wire: _Wire) -> ChatOpenAI:
    return ChatOpenAI(
        model="qwen3.8-flash",
        api_key="stub",
        base_url=_GATEWAY,
        reasoning_effort="low",
        http_client=wire.client(),
    )


@dataclass(frozen=True)
class _Sent:
    """One call whose reply carries reasoning, and the request it must still send.

    ``body`` is what the provider sent for this call before it recorded reasoning.
    """

    id: str
    answer: dict[str, Any]
    call: Callable[[_Wire], Awaitable[Any]]
    path: str
    body: dict[str, Any]
    beta: str | None = None


_SENT: list[_Sent] = [
    _Sent(
        "openai-compatible text",
        _chat_completion(_REPLY_TEXT, reasoning=_THINKING),
        lambda wire: _openai_gateway(wire).ainvoke(_PROMPT),
        "/v1/chat/completions",
        _OPENAI_BODY,
    ),
    _Sent(
        "openai-compatible structured",
        _chat_completion(_REPLY_JSON, reasoning=_THINKING),
        lambda wire: _openai_gateway(wire).ainvoke(_PROMPT, Reply),
        "/v1/chat/completions",
        {
            **_OPENAI_BODY,
            "response_format": {
                "json_schema": {"name": "agent_output", "schema": _REPLY_SCHEMA, "strict": False},
                "type": "json_schema",
            },
        },
    ),
    _Sent(
        "openai-compatible tools",
        _chat_completion(None, reasoning=_THINKING, tool_calls=[_LOOKUP]),
        lambda wire: _openai_gateway(wire).ainvoke(_PROMPT, tools=_TOOLS),
        "/v1/chat/completions",
        {
            **_OPENAI_BODY,
            "tool_choice": "auto",
            "tools": [
                {
                    "function": {
                        "description": "Look a city up",
                        "name": "lookup",
                        "parameters": _LOOKUP_SCHEMA,
                    },
                    "type": "function",
                }
            ],
        },
    ),
    _Sent(
        "anthropic text",
        _anthropic_message(_ANTHROPIC_TEXT, thinking=_THINKING, redacted=_REDACTED),
        lambda wire: _anthropic(wire).ainvoke(_PROMPT),
        "/v1/messages",
        _ANTHROPIC_BODY,
    ),
    _Sent(
        "anthropic native structured",
        _anthropic_message(_ANTHROPIC_JSON, thinking=_THINKING, redacted=_REDACTED),
        lambda wire: _anthropic(wire).ainvoke(_PROMPT, Reply),
        "/v1/messages",
        {
            **_ANTHROPIC_BODY,
            "output_config": {"format": {"schema": _REPLY_SCHEMA, "type": "json_schema"}},
        },
        beta="structured-outputs-2025-12-15",
    ),
    _Sent(
        "anthropic forced tool",
        _anthropic_message(_ANTHROPIC_SCHEMA_TOOL, thinking=_THINKING, redacted=_REDACTED),
        lambda wire: _anthropic(wire, use_native_structured_output=False).ainvoke(_PROMPT, Reply),
        "/v1/messages",
        {
            **_ANTHROPIC_BODY,
            "tool_choice": {"name": "Reply", "type": "tool"},
            "tools": [
                {
                    "cache_control": {"type": "ephemeral"},
                    "description": "Extract information in the format of Reply",
                    "input_schema": _REPLY_SCHEMA,
                    "name": "Reply",
                }
            ],
        },
    ),
    _Sent(
        "anthropic tools",
        _anthropic_message(_ANTHROPIC_LOOKUP, thinking=_THINKING, redacted=_REDACTED),
        lambda wire: _anthropic(wire).ainvoke(_PROMPT, tools=_TOOLS),
        "/v1/messages",
        {
            **_ANTHROPIC_BODY,
            "tool_choice": {"type": "auto"},
            "tools": [
                {"description": "Look a city up", "input_schema": _LOOKUP_SCHEMA, "name": "lookup"}
            ],
        },
    ),
]


@pytest.mark.parametrize("sent", _SENT, ids=[s.id for s in _SENT])
async def test_the_request_is_the_one_sent_before_reasoning_was_recorded(sent: _Sent) -> None:
    """The reply carries reasoning; the request neither asks for it nor changes."""
    wire = _Wire(sent.answer)
    completion = await sent.call(wire)

    (request,) = wire.requests
    assert request.url.path == sent.path
    assert request.headers.get("anthropic-beta") == sent.beta
    assert wire.body == sent.body
    assert completion.thinking == _THINKING


async def test_gemini_never_asks_for_its_thoughts() -> None:
    """Thoughts are read when they arrive; ``include_thoughts`` is never sent."""
    wire = _Wire(_gemini_reply([{"text": _REPLY_TEXT}], thinking=_THINKING))
    completion = await _gemini(wire).ainvoke(_PROMPT)

    assert completion.thinking == _THINKING
    sent = json.dumps(wire.body)
    assert "include_thoughts" not in sent and "includeThoughts" not in sent
    assert wire.body["generationConfig"]["thinkingConfig"] == {"thinking_budget": -1}

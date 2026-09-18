"""One answer to "the model wrote a bad parameter", for every provider.

A structured reply that does not fit the schema is the one provider error
the *model* can fix. The Loop knows that: it turns
``StructuredOutputValidationError`` into a synthetic completion, puts the
model's own text through the agent's parser and hands back one located
line. Every other provider error — a 429, a dead socket — ends the run.

That only holds if every provider raises the typed error. A provider that
calls ``output_format.model_validate*`` itself raises a bare
``ValidationError``, which its own ``except Exception`` turns into a
generic ``ModelProviderError``, and the Loop ends the episode. The
behaviour would then depend on which provider key the YAML happens to
name — the per-provider split the LLM layer exists to remove, reappearing
in the one place a researcher would never think to look.

So the seam is scanned rather than trusted: no module under
``providers/`` validates an output format by hand.

Calling the validator is only half of it, though, and the half a scan can
see. The error still has to survive the trip out: ``AnthropicCompatBase``
called ``_validate_structured`` correctly on both its structured paths and
then caught its own typed error in a trailing ``except Exception`` that
re-raised a generic ``ModelProviderError`` — passing the scan while
costing the run exactly what the scan exists to prevent. So the second
half of this file drives every registered provider through a stub client
that answers badly, and asserts on the type the *Loop* sees.
"""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import spectragent.core.llm.providers as providers_pkg
from spectragent.core.llm._registrar import ChatModelBase, LLMRegistrar
from spectragent.core.llm.exceptions import (
    ModelProviderError,
    StructuredOutputValidationError,
)
from spectragent.core.llm.messages import UserMessage

_PROVIDERS_DIR = Path(providers_pkg.__file__).parent

#: Modules allowed to validate by hand, and why. The exemption is not a
#: to-do: ``try_parse_groq_failed_generation`` is a salvage attempt inside
#: ``except GroqStatusError``, whose caller discards anything it raises and
#: lets the original status error stand. A recoverable-error type raised
#: there could not reach the Loop even in principle — the request already
#: failed, and that failure is the provider's, not the model's.
_EXEMPT = {"_groq_parser.py"}

_VALIDATORS = {"model_validate", "model_validate_json", "model_validate_strings"}


def _hand_validations(path: Path) -> list[str]:
    """``model_validate*`` calls in *path*, as ``name.attr`` at line N."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _VALIDATORS:
            continue
        target = func.value
        # ``self._validate_structured`` is the sanctioned route; what this
        # hunts is a *format* being validated directly, which is always a
        # bare name (``output_format``) or a subscript of one.
        if isinstance(target, ast.Name):
            found.append(f"{path.name}:{node.lineno}: {target.id}.{func.attr}(...)")
    return found


def test_no_provider_validates_an_output_format_by_hand() -> None:
    offenders: list[str] = []
    for path in sorted(_PROVIDERS_DIR.glob("*.py")):
        if path.name in _EXEMPT:
            continue
        offenders.extend(_hand_validations(path))
    assert offenders == [], (
        "These providers validate a structured reply themselves, so a "
        "mistyped parameter ends the run there while the same mistake is "
        "recoverable elsewhere. Route them through "
        "ChatModelBase._validate_structured(output_format, payload, "
        "usage=usage):\n  " + "\n  ".join(offenders)
    )


def test_the_exemption_still_exists() -> None:
    """A stale exemption is a hole nobody is looking at."""
    for name in _EXEMPT:
        assert (_PROVIDERS_DIR / name).exists()


class _Shape(BaseModel):
    index: int


class _Provider(ChatModelBase):
    """The shared validator, exercised the way a provider calls it."""

    __abstract__ = True

    name = "stub-provider"
    model = "stub-model"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param('{"index": "not-an-int"}', id="text-wrong-type"),
        pytest.param({"index": "not-an-int"}, id="mapping-wrong-type"),
        pytest.param("{not json at all", id="malformed-json"),
    ],
)
def test_a_bad_reply_is_recoverable_and_keeps_what_it_cost(payload: object) -> None:
    provider = _Provider()
    with pytest.raises(StructuredOutputValidationError) as exc:
        provider._validate_structured(_Shape, payload, usage="7 tokens")  # type: ignore[arg-type]
    # Three things the Loop needs and a generic error cannot carry: the
    # model's own text to feed back, what the call cost, and a status that
    # says "the model can fix this".
    assert exc.value.raw_output
    assert exc.value.usage == "7 tokens"
    assert exc.value.status_code == 422
    assert "stub-provider" in str(exc.value)


def test_a_good_reply_is_returned_as_the_declared_type() -> None:
    provider = _Provider()
    assert provider._validate_structured(_Shape, '{"index": 3}').index == 3
    assert provider._validate_structured(_Shape, {"index": 3}).index == 3


# ── The error has to survive the trip out, for every provider ──


def _import_every_provider() -> None:
    """Populate the registry — the table below is checked against it."""
    for module in pkgutil.iter_modules(providers_pkg.__path__):
        if module.name.startswith("_"):
            continue
        importlib.import_module(f"{providers_pkg.__name__}.{module.name}")


_import_every_provider()

#: The reply every stub gives back: the right field, the wrong type. Chosen
#: over malformed JSON because it reaches the *validator* rather than the
#: json decoder, which is the path a real model's mistake takes.
_BAD = '{"index": "not-an-int"}'


@dataclass(frozen=True)
class _Case:
    """One provider path, stubbed just far enough to answer badly.

    ``key`` is the registry key the case covers, so a provider added to
    ``providers/`` without a case here fails
    :func:`test_every_registered_provider_is_covered` rather than quietly
    keeping its own error semantics.
    """

    id: str
    key: str
    build: Callable[[], Any]


# -- stub clients, one per wire shape -------------------------------------


def _openai_client(content: str | None = _BAD, *, tool_arguments: str | None = None) -> Any:
    """``client.chat.completions.create``, answering once and badly."""

    message = SimpleNamespace(
        content=content,
        tool_calls=(
            [SimpleNamespace(function=SimpleNamespace(arguments=tool_arguments))]
            if tool_arguments is not None
            else None
        ),
    )

    class _Completions:
        async def create(self, **_kw: Any) -> Any:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=None,
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


def _anthropic_client(blocks: list[Any]) -> Any:
    """A real ``Message`` — the provider ``isinstance``-checks the reply."""
    from anthropic.types import Message, Usage

    class _Messages:
        async def create(self, **_kw: Any) -> Any:
            return Message(
                id="msg_stub",
                content=blocks,
                model="claude-stub",
                role="assistant",
                stop_reason="end_turn",
                stop_sequence=None,
                type="message",
                usage=Usage(input_tokens=1, output_tokens=1),
            )

    return SimpleNamespace(messages=_Messages())


# -- builders -------------------------------------------------------------


def _anthropic_native() -> Any:
    from anthropic.types.text_block import TextBlock

    from spectragent.core.llm.providers.anthropic import ChatAnthropic

    chat = ChatAnthropic(model="claude-sonnet-4-5", api_key="stub")
    chat.get_client = lambda: _anthropic_client(  # type: ignore[method-assign]
        [TextBlock(type="text", text=_BAD, citations=None)]
    )
    return chat


def _anthropic_tool_use() -> Any:
    from anthropic.types.tool_use_block import ToolUseBlock

    from spectragent.core.llm.providers.anthropic import ChatAnthropic

    chat = ChatAnthropic(
        model="claude-sonnet-4-5", api_key="stub", use_native_structured_output=False
    )
    chat.get_client = lambda: _anthropic_client(  # type: ignore[method-assign]
        [ToolUseBlock(type="tool_use", id="tu_stub", name="_Shape", input=json.loads(_BAD))]
    )
    return chat


def _anthropic_aws() -> Any:
    from anthropic.types.text_block import TextBlock

    from spectragent.core.llm.providers.anthropic_aws import ChatAnthropicAWS

    chat = ChatAnthropicAWS(model="claude-sonnet-4-5", aws_region="us-east-1")
    chat.get_client = lambda: _anthropic_client(  # type: ignore[method-assign]
        [TextBlock(type="text", text=_BAD, citations=None)]
    )
    return chat


def _bedrock() -> Any:
    from spectragent.core.llm.providers.bedrock import ChatBedrock

    class _Converse:
        def converse(self, **_kw: Any) -> dict[str, Any]:
            return {
                "output": {"message": {"content": [{"toolUse": {"input": json.loads(_BAD)}}]}},
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            }

    chat = ChatBedrock(model="anthropic.claude-3-5-sonnet-20240620-v1:0")
    chat._get_client = lambda: _Converse()  # type: ignore[method-assign]
    return chat


def _browser_use() -> Any:
    from spectragent.core.llm.providers.browser_use import ChatBrowserUse

    chat = ChatBrowserUse(model="bu-1-0", api_key="stub")

    async def _request(_payload: dict) -> dict:
        return {"completion": _BAD, "usage": None}

    chat._make_request = _request  # type: ignore[method-assign]
    return chat


def _google_native() -> Any:
    from spectragent.core.llm.providers.google import ChatGoogle

    class _Models:
        async def generate_content(self, **_kw: Any) -> Any:
            return SimpleNamespace(parsed=None, text=_BAD, usage_metadata=None, candidates=[])

    chat = ChatGoogle(model="gemini-2.5-flash", api_key="stub")
    chat.get_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
        aio=SimpleNamespace(models=_Models())
    )
    return chat


def _google_prompt_fallback() -> Any:
    chat = _google_native()
    chat.supports_structured_output = False
    return chat


def _mistral() -> Any:
    from spectragent.core.llm.providers.mistral import ChatMistral

    chat = ChatMistral(model="mistral-large-latest", api_key="stub")

    async def _post(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"message": {"content": _BAD}}], "usage": None}

    chat._post = _post  # type: ignore[method-assign]
    return chat


def _oci() -> Any:
    from spectragent.core.llm.providers.oci import ChatOCI

    chat = ChatOCI(
        model_id="ocid1.generativeaimodel.oc1..stub",
        service_endpoint="https://stub.invalid",
        compartment_id="ocid1.compartment.oc1..stub",
    )

    async def _request(_messages: Any) -> Any:
        return SimpleNamespace(data=SimpleNamespace(chat_response=SimpleNamespace(text=_BAD)))

    chat._make_request = _request  # type: ignore[method-assign]
    return chat


def _ollama() -> Any:
    from spectragent.core.llm.providers.ollama import ChatOllama

    class _Client:
        async def chat(self, **_kw: Any) -> Any:
            return SimpleNamespace(
                message=SimpleNamespace(content=_BAD),
                prompt_eval_count=1,
                eval_count=1,
            )

    chat = ChatOllama(model="llama3.1")
    chat.get_client = lambda: _Client()  # type: ignore[method-assign]
    return chat


def _openai_direct() -> Any:
    from spectragent.core.llm.providers.openai import ChatOpenAI

    chat = ChatOpenAI(model="gpt-4o", api_key="stub")
    chat.get_client = lambda: _openai_client()  # type: ignore[method-assign]
    return chat


def _openrouter() -> Any:
    from spectragent.core.llm.providers.openai_openrouter import ChatOpenRouter

    chat = ChatOpenRouter(model="openai/gpt-4o", api_key="stub")
    chat.get_client = lambda: _openai_client()  # type: ignore[method-assign]
    return chat


def _azure_chat() -> Any:
    from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI

    chat = ChatAzureOpenAI(
        model="gpt-4o",
        api_key="stub",
        azure_endpoint="https://stub.invalid",
        use_responses_api="false",
    )
    chat.get_client = lambda: _openai_client()  # type: ignore[method-assign]
    return chat


def _azure_responses() -> Any:
    from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI

    class _Responses:
        async def create(self, **_kw: Any) -> Any:
            return SimpleNamespace(output_text=_BAD, status="completed", usage=None)

    chat = ChatAzureOpenAI(
        model="gpt-4o",
        api_key="stub",
        azure_endpoint="https://stub.invalid",
        use_responses_api="true",
    )
    chat.get_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
        responses=_Responses()
    )
    return chat


def _cerebras() -> Any:
    from spectragent.core.llm.providers.openai_cerebras import ChatCerebras

    chat = ChatCerebras(model="llama3.1-8b", api_key="stub")
    chat.get_client = lambda: _openai_client()  # type: ignore[method-assign]
    return chat


def _deepseek() -> Any:
    from spectragent.core.llm.providers.openai_deepseek import ChatDeepSeek

    chat = ChatDeepSeek(model="deepseek-chat", api_key="stub")
    chat.get_client = lambda: _openai_client(  # type: ignore[method-assign]
        content=None, tool_arguments=_BAD
    )
    return chat


def _groq() -> Any:
    from spectragent.core.llm.providers.openai_groq import ChatGroq

    chat = ChatGroq(model="llama-3.3-70b-versatile", api_key="stub")
    chat.get_client = lambda: _openai_client()  # type: ignore[method-assign]
    return chat


def _vercel_response_format() -> Any:
    from spectragent.core.llm.providers.openai_vercel import ChatVercel

    chat = ChatVercel(model="openai/gpt-4o", api_key="stub")
    chat.get_client = lambda: _openai_client()  # type: ignore[method-assign]
    return chat


def _vercel_prompt_json() -> Any:
    from spectragent.core.llm.providers.openai_vercel import ChatVercel

    chat = ChatVercel(model="anthropic/claude-sonnet-4-5", api_key="stub")
    chat.get_client = lambda: _openai_client()  # type: ignore[method-assign]
    assert chat._vercel_needs_prompt_json(), "this case no longer exercises the prompt path"
    return chat


#: One entry per registry key, plus a second entry wherever a provider has
#: two structured paths guarded by two different ``try`` blocks — which is
#: where the Anthropic regression lived, on both of them.
_CASES: list[_Case] = [
    _Case("anthropic.direct[native]", "anthropic.direct", _anthropic_native),
    _Case("anthropic.direct[tool-use]", "anthropic.direct", _anthropic_tool_use),
    _Case("anthropic.aws", "anthropic.aws", _anthropic_aws),
    _Case("bedrock.default", "bedrock.default", _bedrock),
    _Case("browser_use.default", "browser_use.default", _browser_use),
    _Case("google.default[native]", "google.default", _google_native),
    _Case("google.default[prompt]", "google.default", _google_prompt_fallback),
    _Case("mistral.default", "mistral.default", _mistral),
    _Case("oci.default", "oci.default", _oci),
    _Case("ollama.default", "ollama.default", _ollama),
    _Case("openai.direct", "openai.direct", _openai_direct),
    _Case("openai.openrouter", "openai.openrouter", _openrouter),
    _Case("openai.azure[chat]", "openai.azure", _azure_chat),
    _Case("openai.azure[responses]", "openai.azure", _azure_responses),
    _Case("openai.cerebras", "openai.cerebras", _cerebras),
    _Case("openai.deepseek", "openai.deepseek", _deepseek),
    _Case("openai.groq", "openai.groq", _groq),
    _Case("openai.vercel[response_format]", "openai.vercel", _vercel_response_format),
    _Case("openai.vercel[prompt]", "openai.vercel", _vercel_prompt_json),
]


def test_every_registered_provider_is_covered() -> None:
    """A new provider is covered by construction, or this fails.

    The scan above proves a provider *calls* the shared validator. Only a
    real call proves the typed error still is one by the time the Loop
    catches it, so every registry key owes one.
    """
    uncovered = set(LLMRegistrar.get_all()) - {case.key for case in _CASES}
    assert uncovered == set(), (
        "These providers have no behavioural case, so nothing would notice "
        "if a catch-all in them flattened StructuredOutputValidationError "
        "into a generic ModelProviderError and ended runs over one mistyped "
        f"parameter: {sorted(uncovered)}"
    )


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
async def test_a_bad_reply_reaches_the_caller_still_recoverable(case: _Case) -> None:
    provider = case.build()
    with pytest.raises(StructuredOutputValidationError) as exc:
        await provider.ainvoke([UserMessage(content="hi")], _Shape)

    # What the Loop needs and a flattened error cannot give it: the model's
    # own words to hand back, and a status that says the model can fix this.
    assert json.loads(exc.value.raw_output) == {"index": "not-an-int"}
    assert exc.value.status_code == 422


# ── Row 2: the closed union is the shape af actually ships ──


def _closed_union_model() -> type[BaseModel]:
    """A plain ``Union`` of single-key models, each ``extra="forbid"``.

    The shape ``agents/_action_union.py`` builds: ``anyOf`` of objects that
    each name one verb as a *property*, so nothing beyond
    ``object``/``properties``/``required``/``anyOf`` is needed — the
    intersection of the OpenAI, Anthropic and Gemini dialects.
    """
    from typing import Union

    from pydantic import ConfigDict, create_model

    make: Any = create_model

    def verb(name: str, **fields: Any) -> Any:
        params = make(f"{name}Params", __config__=ConfigDict(extra="forbid"), **fields)
        return make(
            f"{name}Item",
            __config__=ConfigDict(extra="forbid"),
            **{name: (params, ...)},
        )

    click = verb("click", index=(int, ...))
    type_ = verb("type", index=(int, ...), text=(str, ...))
    return make(
        "_ClosedUnionOut",
        __config__=ConfigDict(extra="forbid"),
        action=(list[Union[click, type_]], ...),  # noqa: UP007
    )


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
async def test_a_closed_union_output_format_compiles_on_every_provider(
    case: _Case,
) -> None:
    """The second behaviour every provider owes: it can *ship* this schema.

    ``_Shape`` above is one integer field — flat, ``$ref``-free, and so it
    exercises none of what af actually sends. Every real run ships a closed
    action union, and that is where providers differ: Bedrock used to strip
    an array's ``items``, Gemini's ``response_schema`` rejects
    ``additionalProperties`` inside an ``anyOf`` branch, Ollama compiles the
    schema into a decoding grammar, and ``google-genai`` refuses a
    ``discriminator`` in its own local validation before any request.

    A provider whose schema compilation cannot express the union fails
    *here* — while building the request — with something other than
    ``StructuredOutputValidationError``. Reaching the validator at all
    means the schema went out and a reply came back, which is the thing
    a per-provider split would silently take away.
    """
    provider = case.build()
    with pytest.raises(StructuredOutputValidationError) as exc:
        await provider.ainvoke([UserMessage(content="hi")], _closed_union_model())

    # Still the model's mistake to fix, not the transport's — the stub
    # answers `{"index": ...}`, which names no verb at all.
    assert exc.value.status_code == 422
    assert exc.value.raw_output


def test_the_groq_salvage_path_is_exempt_on_purpose() -> None:
    """The one place a bad reply is *not* recoverable, asserted not assumed.

    ``try_parse_groq_failed_generation`` runs inside ``except
    GroqStatusError``: the request already failed, and the caller discards
    whatever the salvage raises so the original status error stands. The
    resulting error is therefore a plain provider failure — which is
    correct, because the failure is the provider's, and a run that retried
    the model here would be retrying against a 400.
    """
    import httpx
    from groq import APIStatusError as GroqStatusError

    from spectragent.core.llm.providers.openai_groq import ChatGroq

    status_error = GroqStatusError(
        "failed_generation",
        response=httpx.Response(400, request=httpx.Request("POST", "https://stub.invalid")),
        body={"error": {"failed_generation": _BAD}},
    )

    class _Completions:
        async def create(self, **_kw: Any) -> Any:
            raise status_error

    chat = ChatGroq(model="llama-3.3-70b-versatile", api_key="stub")
    chat.get_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
        chat=SimpleNamespace(completions=_Completions())
    )

    import asyncio

    with pytest.raises(ModelProviderError) as exc:
        asyncio.run(chat.ainvoke([UserMessage(content="hi")], _Shape))

    assert not isinstance(exc.value, StructuredOutputValidationError)
    assert exc.value.status_code == 400

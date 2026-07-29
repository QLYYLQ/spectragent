"""Provider invocation-path regressions (§6.4.1, §6.4.4, §6.4.5, §6.4.6)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

from spectragent.core.llm.messages import (
    ContentPartTextParam,
    SystemMessage,
    UserMessage,
)


class _Out(BaseModel):
    memory: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class _Inner(BaseModel):
    index: int


class _Nested(BaseModel):
    memory: str
    inner: _Inner
    items: list[_Inner] = Field(default_factory=list)


# ── §6.4.1 ChatOCI must not mutate the caller's messages ───────────


def _make_oci():
    from spectragent.core.llm.providers.oci import ChatOCI

    return ChatOCI(
        model_id="ocid1.generativeaimodel.oc1..test",
        service_endpoint="https://example.test",
        compartment_id="ocid1.compartment.oc1..test",
    )


class TestOCIDoesNotMutateCaller:
    async def test_system_message_is_not_grown_in_place(self, monkeypatch) -> None:
        chat = _make_oci()
        system = SystemMessage(content="BASE SYSTEM")
        messages = [system, UserMessage(content="hi")]

        seen: list[Any] = []

        async def _fake_request(msgs):
            seen.append(msgs)
            return SimpleNamespace()

        monkeypatch.setattr(chat, "_make_request", _fake_request)
        monkeypatch.setattr(chat, "_extract_content", lambda _r: json.dumps({"memory": "m"}))
        monkeypatch.setattr(chat, "_extract_usage", lambda _r: None)

        await chat.ainvoke(messages, output_format=_Out)
        await chat.ainvoke(messages, output_format=_Out)

        # The caller's message is untouched...
        assert system.content == "BASE SYSTEM"
        # ...and the schema block does not accumulate across steps.
        assert seen[0][0].content == seen[1][0].content
        assert seen[0][0].content.count("You must respond with ONLY") == 1
        assert seen[0][0].content.startswith("BASE SYSTEM")

    async def test_list_content_system_message_is_not_stringified(self, monkeypatch) -> None:
        chat = _make_oci()
        system = SystemMessage(content=[ContentPartTextParam(text="BASE")])
        seen: list[Any] = []

        async def _fake_request(msgs):
            seen.append(msgs)
            return SimpleNamespace()

        monkeypatch.setattr(chat, "_make_request", _fake_request)
        monkeypatch.setattr(chat, "_extract_content", lambda _r: json.dumps({"memory": "m"}))
        monkeypatch.setattr(chat, "_extract_usage", lambda _r: None)

        await chat.ainvoke([system, UserMessage(content="hi")], output_format=_Out)

        sent = seen[0][0]
        assert isinstance(sent.content, list)
        assert sent.content[0].text == "BASE"
        assert "You must respond with ONLY" in sent.content[1].text
        assert isinstance(system.content, list)
        assert system.content[0].text == "BASE"


# ── §6.4.4 usage accounting ────────────────────────────────────────


class _FakeOllamaClient:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class TestOllamaUsage:
    """Ollama is the offline reproducible-benchmark provider — usage matters most here."""

    def _response(self, content: str):
        return SimpleNamespace(
            message=SimpleNamespace(content=content),
            prompt_eval_count=123,
            eval_count=45,
        )

    async def test_text_path_reports_usage(self, monkeypatch) -> None:
        from spectragent.core.llm.providers.ollama import ChatOllama

        chat = ChatOllama(model="llama3.1")
        client = _FakeOllamaClient(self._response("hello"))
        monkeypatch.setattr(chat, "get_client", lambda: client)

        result = await chat.ainvoke([UserMessage(content="hi")])

        assert result.usage is not None
        assert result.usage.prompt_tokens == 123
        assert result.usage.completion_tokens == 45
        assert result.usage.total_tokens == 168

    async def test_structured_path_reports_usage_and_flattens_schema(self, monkeypatch) -> None:
        from spectragent.core.llm.providers.ollama import ChatOllama

        chat = ChatOllama(model="llama3.1")
        client = _FakeOllamaClient(
            self._response(json.dumps({"memory": "m", "inner": {"index": 1}}))
        )
        monkeypatch.setattr(chat, "get_client", lambda: client)

        result = await chat.ainvoke([UserMessage(content="hi")], output_format=_Nested)

        assert result.usage is not None
        assert result.usage.prompt_tokens == 123
        # Local grammar backends do not resolve $ref — the schema must be flat.
        schema = client.calls[0]["format"]
        assert "$defs" not in schema
        assert schema["properties"]["inner"]["properties"]["index"]["type"] == "integer"

    async def test_missing_counters_still_yields_none(self, monkeypatch) -> None:
        from spectragent.core.llm.providers.ollama import ChatOllama

        chat = ChatOllama(model="llama3.1")
        client = _FakeOllamaClient(
            SimpleNamespace(
                message=SimpleNamespace(content="x"), prompt_eval_count=None, eval_count=None
            )
        )
        monkeypatch.setattr(chat, "get_client", lambda: client)
        assert (await chat.ainvoke([UserMessage(content="hi")])).usage is None


class TestDeepSeekUsage:
    async def test_tool_call_path_reports_usage(self, monkeypatch) -> None:
        from spectragent.core.llm.providers.openai_deepseek import ChatDeepSeek

        chat = ChatDeepSeek(model="deepseek-chat", api_key="test")

        tool_call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps({"memory": "m"})))
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(tool_calls=[tool_call]),
                    finish_reason="tool_calls",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                prompt_tokens_details=None,
            ),
        )

        class _Completions:
            async def create(self, **_kwargs):
                return response

        client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        monkeypatch.setattr(chat, "get_client", lambda: client)

        result = await chat.ainvoke([UserMessage(content="hi")], output_format=_Out)

        assert result.usage is not None
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 5
        assert result.stop_reason == "tool_calls"


# ── §6.4.5 Bedrock tool schema ─────────────────────────────────────


class TestBedrockToolSchema:
    """`_format_tools_for_request` used to flatten submodels to {"type": "string"}."""

    def test_nested_models_and_array_items_survive(self) -> None:
        from spectragent.core.llm.providers.bedrock import ChatBedrock

        chat = ChatBedrock(model="anthropic.claude-3-5-sonnet-20240620-v1:0")
        spec = chat._format_tools_for_request(_Nested)[0]["toolSpec"]
        schema = spec["inputSchema"]["json"]

        assert schema["properties"]["inner"]["type"] == "object"
        assert schema["properties"]["inner"]["properties"]["index"]["type"] == "integer"
        assert schema["properties"]["items"]["items"]["properties"]["index"]["type"] == "integer"
        assert "$defs" not in schema

    def test_claude_on_bedrock_is_not_forced_strict(self) -> None:
        from spectragent.core.llm.providers.bedrock import ChatBedrock

        chat = ChatBedrock(model="anthropic.claude-3-5-sonnet-20240620-v1:0")
        schema = chat._format_tools_for_request(_Nested)[0]["toolSpec"]["inputSchema"]["json"]
        # `items` has a default, so it is genuinely optional.
        assert "items" not in schema["required"]


# ── §6.4.6 ChatBrowserUse ──────────────────────────────────────────


class _ActionA(BaseModel):
    kind: str = "a"
    index: int


class _BrowserOut(BaseModel):
    memory: str
    action: list[_ActionA]


class TestChatBrowserUse:
    @pytest.fixture(autouse=True)
    def _api_key(self, monkeypatch):
        monkeypatch.setenv("BROWSER_USE_API_KEY", "test-key")

    def _chat(self, **kwargs):
        from spectragent.core.llm.providers.browser_use import ChatBrowserUse

        return ChatBrowserUse(**kwargs)

    async def test_request_type_defaults_and_is_configurable(self, monkeypatch) -> None:
        sent: list[dict] = []

        async def _fake(payload):
            sent.append(payload)
            return {"completion": "ok"}

        chat = self._chat()
        monkeypatch.setattr(chat, "_make_request", _fake)
        await chat.ainvoke([UserMessage(content="hi")])
        assert sent[-1]["request_type"] == "browser_agent"

        chat2 = self._chat(request_type="swe_agent")
        monkeypatch.setattr(chat2, "_make_request", _fake)
        await chat2.ainvoke([UserMessage(content="hi")])
        assert sent[-1]["request_type"] == "swe_agent"

    async def test_per_call_override_still_works(self, monkeypatch) -> None:
        sent: list[dict] = []

        async def _fake(payload):
            sent.append(payload)
            return {"completion": "ok"}

        chat = self._chat(request_type="swe_agent")
        monkeypatch.setattr(chat, "_make_request", _fake)
        await chat.ainvoke([UserMessage(content="hi")], request_type="one_off")
        assert sent[-1]["request_type"] == "one_off"

    async def test_structured_output_uses_pydantic_not_a_hardcoded_field(self, monkeypatch) -> None:
        async def _fake(_payload):
            return {"completion": {"memory": "m", "action": [{"kind": "a", "index": 7}]}}

        chat = self._chat()
        monkeypatch.setattr(chat, "_make_request", _fake)
        result = await chat.ainvoke([UserMessage(content="hi")], output_format=_BrowserOut)

        assert isinstance(result.completion, _BrowserOut)
        assert result.completion.action[0].index == 7

    async def test_output_model_without_action_field_still_parses(self, monkeypatch) -> None:
        """The old parser only special-cased a field literally named "action"."""

        class _NoAction(BaseModel):
            memory: str
            steps: list[_ActionA]

        async def _fake(_payload):
            return {"completion": {"memory": "m", "steps": [{"kind": "a", "index": 1}]}}

        chat = self._chat()
        monkeypatch.setattr(chat, "_make_request", _fake)
        result = await chat.ainvoke([UserMessage(content="hi")], output_format=_NoAction)
        assert result.completion.steps[0].index == 1


# ── Truncation at the completion-token ceiling ─────────────────────


class _TruncatingOpenAIClient:
    """Replays one OpenAI-shaped chat completion with a chosen finish_reason."""

    def __init__(self, content: str, finish_reason: str, usage: Any = None) -> None:
        self._content = content
        self._finish_reason = finish_reason
        self._usage = usage
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    async def _create(self, **_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self._content, tool_calls=None),
                    finish_reason=self._finish_reason,
                )
            ],
            usage=self._usage,
        )


def _usage(prompt: int, completion: int):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=None,
    )


class TestStructuredOutputTruncation:
    """A step cut off at the token ceiling must say so.

    ``BrowserUseAgent``'s open ``action`` map downgrades the request to
    ``strict: false`` (see ``TestOpenAIStrictModeDowngrade``), so nothing
    constrains a chatty model from spending the whole completion budget on
    one step. What came back then was half-written JSON, and parsing it
    first blamed the *schema*::

        1 validation error for BrowserUseOutputForRun
          Invalid JSON: EOF while parsing an object at line 988 column 649

    — which sends the reader after the model's schema compliance instead of
    the ceiling that actually truncated it.
    """

    def _chat(self, **kw):
        from spectragent.core.llm.providers.openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o", api_key="test", **kw)

    async def test_length_stop_names_the_ceiling_not_the_schema(
        self, monkeypatch
    ) -> None:
        from spectragent.core.llm.exceptions import ModelProviderError

        chat = self._chat(max_completion_tokens=16384)
        client = _TruncatingOpenAIClient(
            '{"memory": "m", "action": [{"browser.cli',  # cut mid-token
            finish_reason="length",
            usage=_usage(2000, 16370),
        )
        monkeypatch.setattr(chat, "get_client", lambda: client)

        with pytest.raises(ModelProviderError) as excinfo:
            await chat.ainvoke([UserMessage(content="hi")], output_format=_Out)

        message = str(excinfo.value)
        assert "truncated" in message
        assert "16384" in message  # the ceiling that cut it off
        assert "16370" in message  # what the call actually cost
        assert "validation error" not in message

    async def test_a_healthy_stop_still_parses(self, monkeypatch) -> None:
        chat = self._chat()
        client = _TruncatingOpenAIClient(
            json.dumps({"memory": "m"}),
            finish_reason="stop",
            usage=_usage(10, 5),
        )
        monkeypatch.setattr(chat, "get_client", lambda: client)

        result = await chat.ainvoke([UserMessage(content="hi")], output_format=_Out)

        assert result.completion.memory == "m"
        assert result.usage is not None
        assert result.usage.completion_tokens == 5

    async def test_genuine_schema_violations_are_still_reported_as_such(
        self, monkeypatch
    ) -> None:
        """The ceiling check must not swallow real non-compliance."""
        from spectragent.core.llm.exceptions import ModelProviderError

        chat = self._chat()
        client = _TruncatingOpenAIClient(
            json.dumps({"wrong_field": 1}), finish_reason="stop"
        )
        monkeypatch.setattr(chat, "get_client", lambda: client)

        with pytest.raises(ModelProviderError) as excinfo:
            await chat.ainvoke([UserMessage(content="hi")], output_format=_Out)

        assert "truncated" not in str(excinfo.value)

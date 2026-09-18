"""Native function calling in the LLM layer (F4.5 / §5.2.5).

``AssistantMessage.tool_calls`` existed with no inbound counterpart: there was
no ``tool`` role, and ``ainvoke`` had no ``tools`` parameter. Native function
calling — the primary interaction mode for tau-bench, ToolBench, GAIA and most
SWE agents — was structurally unrepresentable, not merely unimplemented.

These tests pin three things:

1. the round trip, per provider family, against the *real* serializers;
2. that every provider either sends ``tools`` to the wire or raises — the one
   thing that must never happen is accepting the argument and dropping it;
3. that tool calls come back on ``ChatInvokeCompletion.tool_calls``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from spectragent.core.llm.exceptions import ToolsUnsupportedError
from spectragent.core.llm.messages import (
    AssistantMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    Function,
    ImageURL,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from spectragent.core.llm.tools import ToolDefinition

# ── Shared fixtures ───────────────────────────────────────────────

_PNG = "data:image/png;base64,aGVsbG8="


def _call(id_: str = "call_1", name: str = "get_weather", args: str = '{"city": "SF"}') -> ToolCall:
    return ToolCall(id=id_, function=Function(name=name, arguments=args))


def _assistant(*calls: ToolCall) -> AssistantMessage:
    return AssistantMessage(content=None, tool_calls=list(calls))


def _result(id_: str = "call_1", content: str = "18C", **kw: Any) -> ToolMessage:
    return ToolMessage(tool_call_id=id_, content=content, **kw)


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="get_weather",
        description="Look up the weather.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )


def _google_contents(messages: list[Any]) -> list[Any]:
    """Serialize for Gemini and hand back a plainly-indexable list.

    ``ContentListUnion`` is a union wide enough to include a bare ``Part``, so
    every ``[0].parts`` in these tests would otherwise be a type error about a
    shape the serializer never returns.
    """
    from spectragent.core.llm.serializers.google_format import GoogleMessageSerializer

    contents, _system = GoogleMessageSerializer.serialize_messages(messages)
    return list(contents)  # type: ignore[arg-type]


def _openai_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Serialize for OpenAI as plain dicts.

    The SDK's message params are TypedDicts, so reading a key that only some
    members declare (``tool_call_id``, ``tool_calls``) is a type error about a
    union the serializer never actually returns for a given role.
    """
    from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer

    return [dict(m) for m in OpenAIMessageSerializer.serialize_messages(messages)]


def _anthropic_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Serialize for Anthropic as plain dicts — same TypedDict reason as above."""
    from spectragent.core.llm.serializers.anthropic_format import AnthropicMessageSerializer

    serialized, _system = AnthropicMessageSerializer.serialize_messages(messages)
    return [dict(m) for m in serialized]


def _fake_openai_client(
    captured: dict[str, Any], *, tool_calls: Any = None, content: str | None = "ok"
):
    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content, tool_calls=tool_calls),
                        finish_reason="tool_calls" if tool_calls else "stop",
                    )
                ],
                usage=None,
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


# ── 1. The message model ──────────────────────────────────────────


class TestToolMessageShape:
    def test_tool_message_is_part_of_the_union(self) -> None:
        from spectragent.core.llm.messages import BaseMessage

        class _Holder(BaseModel):
            message: BaseMessage

        holder = _Holder(message=_result())
        assert isinstance(holder.message, ToolMessage)
        assert holder.message.role == "tool"

    def test_round_trips_through_json(self) -> None:
        """Memory and the WAL both persist messages, so this must survive."""
        from spectragent.core.llm.messages import BaseMessage

        class _Holder(BaseModel):
            message: BaseMessage

        original = _result(content="18C", name="get_weather", is_error=True)
        restored = _Holder.model_validate_json(_Holder(message=original).model_dump_json()).message
        assert restored == original

    def test_text_property_matches_the_other_message_types(self) -> None:
        assert _result(content="18C").text == "18C"
        parts = ToolMessage(
            tool_call_id="c",
            content=[
                ContentPartTextParam(text="a"),
                ContentPartImageParam(image_url=ImageURL(url=_PNG)),
                ContentPartTextParam(text="b"),
            ],
        )
        assert parts.text == "a\nb"

    def test_is_error_defaults_false_and_is_carried(self) -> None:
        assert _result().is_error is False
        assert _result(is_error=True).is_error is True


# ── 2. Serializers, per provider family ───────────────────────────


class TestOpenAISerializer:
    def test_tool_result_becomes_a_tool_role_message(self) -> None:
        out = _openai_messages([_assistant(_call()), _result()])
        assert [m["role"] for m in out] == ["assistant", "tool"]
        assert out[1]["tool_call_id"] == "call_1"
        assert out[1]["content"] == "18C"

    def test_assistant_tool_calls_still_serialize(self) -> None:
        out = _openai_messages([_assistant(_call())])
        assert out[0]["tool_calls"][0]["id"] == "call_1"
        assert out[0]["tool_calls"][0]["function"]["name"] == "get_weather"

    def test_images_ride_in_a_companion_user_message(self) -> None:
        """OpenAI rejects image parts in a tool message; dropping them blinds
        every screenshot-returning tool."""
        message = ToolMessage(
            tool_call_id="call_1",
            content=[
                ContentPartTextParam(text="see screenshot"),
                ContentPartImageParam(image_url=ImageURL(url=_PNG)),
            ],
        )
        out = _openai_messages([_assistant(_call()), message])
        assert [m["role"] for m in out] == ["assistant", "tool", "user"]
        assert out[1]["content"] == "see screenshot"
        image_parts = [p for p in out[2]["content"] if p["type"] == "image_url"]
        assert len(image_parts) == 1

    def test_no_companion_message_without_images(self) -> None:
        out = _openai_messages([_assistant(_call()), _result()])
        assert len(out) == 2

    def test_parallel_results_stay_contiguous_around_an_image(self) -> None:
        """OpenAI: "An assistant message with 'tool_calls' must be followed by
        tool messages responding to each tool_call_id."

        Emitting the companion inline put a user turn *between* two tool
        results, so the second one no longer answered the assistant turn and
        the whole request 400'd. Any screenshot tool running in parallel with a
        second tool hit this.
        """
        screenshot = ToolMessage(
            tool_call_id="call_1",
            content=[
                ContentPartTextParam(text="see screenshot"),
                ContentPartImageParam(image_url=ImageURL(url=_PNG)),
            ],
        )
        out = _openai_messages(
            [_assistant(_call("call_1"), _call("call_2")), screenshot, _result("call_2", "ok")]
        )

        assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user"]
        assert [m["tool_call_id"] for m in out[1:3]] == ["call_1", "call_2"]

    def test_every_companion_in_a_run_survives(self) -> None:
        """Two screenshot tools in parallel — neither image may be dropped, and
        both companions land after the run, in order."""

        def _shot(id_: str) -> ToolMessage:
            return ToolMessage(
                tool_call_id=id_,
                content=[
                    ContentPartTextParam(text=id_),
                    ContentPartImageParam(image_url=ImageURL(url=_PNG)),
                ],
            )

        out = _openai_messages(
            [_assistant(_call("call_1"), _call("call_2")), _shot("call_1"), _shot("call_2")]
        )

        assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user", "user"]
        labels = [m["content"][0]["text"] for m in out[3:]]
        assert labels == [
            "Image output of tool call call_1:",
            "Image output of tool call call_2:",
        ]

    def test_companion_is_flushed_before_the_next_turn(self) -> None:
        """The image must still precede whatever the conversation says next."""
        screenshot = ToolMessage(
            tool_call_id="call_1",
            content=[ContentPartImageParam(image_url=ImageURL(url=_PNG))],
        )
        out = _openai_messages(
            [_assistant(_call()), screenshot, UserMessage(content="what do you see?")]
        )

        assert [m["role"] for m in out] == ["assistant", "tool", "user", "user"]
        assert out[2]["content"][0]["text"] == "Image output of tool call call_1:"
        assert out[3]["content"] == "what do you see?"

    def test_tool_declaration_shape(self) -> None:
        from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer

        [serialized] = OpenAIMessageSerializer.serialize_tools([_tool()])
        assert serialized["type"] == "function"
        assert serialized["function"]["name"] == "get_weather"
        assert serialized["function"]["parameters"]["properties"]["city"]["type"] == "string"
        # strict is opt-in and absent by default.
        assert "strict" not in serialized["function"]


class TestAnthropicSerializer:
    def test_tool_result_is_a_user_turn_with_a_tool_result_block(self) -> None:
        messages = _anthropic_messages([_assistant(_call()), _result()])
        assert [m["role"] for m in messages] == ["assistant", "user"]
        block = messages[1]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "call_1"
        assert block["is_error"] is False

    def test_is_error_reaches_the_wire(self) -> None:
        messages = _anthropic_messages([_assistant(_call()), _result(is_error=True)])
        assert messages[1]["content"][0]["is_error"] is True

    def test_parallel_results_coalesce_into_one_user_turn(self) -> None:
        """Anthropic wants every result for one assistant turn in a single
        user message, not one turn per result."""
        messages = _anthropic_messages(
            [
                _assistant(_call("call_1"), _call("call_2", name="get_time")),
                _result("call_1"),
                _result("call_2", content="noon"),
            ]
        )
        assert [m["role"] for m in messages] == ["assistant", "user"]
        blocks = messages[1]["content"]
        assert [b["tool_use_id"] for b in blocks] == ["call_1", "call_2"]

    def test_tool_declaration_uses_input_schema(self) -> None:
        from spectragent.core.llm.serializers.anthropic_format import AnthropicMessageSerializer

        [serialized] = AnthropicMessageSerializer.serialize_tools([_tool()])
        assert serialized["name"] == "get_weather"
        assert serialized["input_schema"]["properties"]["city"]["type"] == "string"

    def test_parse_tool_calls_from_tool_use_blocks(self) -> None:
        from spectragent.core.llm.serializers.anthropic_format import AnthropicMessageSerializer

        blocks = [
            SimpleNamespace(type="text", text="thinking"),
            SimpleNamespace(
                type="tool_use", id="toolu_1", name="get_weather", input={"city": "SF"}
            ),
        ]
        [call] = AnthropicMessageSerializer.parse_tool_calls(blocks)
        assert call.id == "toolu_1"
        assert call.function.name == "get_weather"
        assert json.loads(call.function.arguments) == {"city": "SF"}


class TestGoogleSerializer:
    def test_tool_result_becomes_a_function_response_part(self) -> None:
        contents = _google_contents([_assistant(_call()), _result()])
        assert [c.role for c in contents] == ["model", "user"]
        response = contents[1].parts[0].function_response
        assert response.name == "get_weather"
        assert response.response == {"output": "18C"}

    def test_name_is_recovered_from_the_matching_call(self) -> None:
        """Gemini keys responses by name and gives no id to correlate with."""
        contents = _google_contents([_assistant(_call(name="lookup")), _result()])
        assert contents[1].parts[0].function_response.name == "lookup"

    def test_explicit_name_wins(self) -> None:
        contents = _google_contents([_assistant(_call(name="lookup")), _result(name="explicit")])
        assert contents[1].parts[0].function_response.name == "explicit"

    def test_unresolvable_name_raises_instead_of_guessing(self) -> None:
        from spectragent.core.llm.serializers.google_format import GoogleMessageSerializer

        with pytest.raises(ValueError, match="keys function responses by tool name"):
            GoogleMessageSerializer.serialize_messages([_result("orphan")])

    def test_errors_use_the_error_key(self) -> None:
        contents = _google_contents([_assistant(_call()), _result(content="boom", is_error=True)])
        assert contents[1].parts[0].function_response.response == {"error": "boom"}

    def test_assistant_tool_calls_become_function_call_parts(self) -> None:
        """Previously the Google serializer ignored tool_calls entirely, so an
        assistant turn that only called a tool vanished."""
        contents = _google_contents([_assistant(_call())])
        assert len(contents) == 1
        call = contents[0].parts[0].function_call
        assert call.name == "get_weather"
        assert call.args == {"city": "SF"}


class TestBedrockSerializer:
    def test_tool_result_is_a_user_turn_with_status(self) -> None:
        from spectragent.core.llm.serializers.bedrock_format import AWSBedrockMessageSerializer

        messages, _ = AWSBedrockMessageSerializer.serialize_messages(
            [_assistant(_call()), _result()]
        )
        assert [m["role"] for m in messages] == ["assistant", "user"]
        block = messages[1]["content"][0]["toolResult"]
        assert block["toolUseId"] == "call_1"
        assert block["status"] == "success"

    def test_error_status(self) -> None:
        from spectragent.core.llm.serializers.bedrock_format import AWSBedrockMessageSerializer

        messages, _ = AWSBedrockMessageSerializer.serialize_messages(
            [_assistant(_call()), _result(is_error=True)]
        )
        assert messages[1]["content"][0]["toolResult"]["status"] == "error"

    def test_parallel_results_coalesce(self) -> None:
        from spectragent.core.llm.serializers.bedrock_format import AWSBedrockMessageSerializer

        messages, _ = AWSBedrockMessageSerializer.serialize_messages(
            [
                _assistant(_call("call_1"), _call("call_2")),
                _result("call_1"),
                _result("call_2"),
            ]
        )
        assert [m["role"] for m in messages] == ["assistant", "user"]
        assert len(messages[1]["content"]) == 2


class TestOllamaSerializer:
    def test_tool_result_uses_the_tool_role(self) -> None:
        from spectragent.core.llm.serializers.ollama_format import OllamaMessageSerializer

        out = OllamaMessageSerializer.serialize_messages([_assistant(_call()), _result()])
        assert [m.role for m in out] == ["assistant", "tool"]
        assert out[1].content == "18C"


class TestSynthesizedToolCallIds:
    """Gemini and Ollama return function calls with no id, so one is minted.

    It has to be unique across the *conversation*, not just within one
    response. Deriving it from the tool name and the position in the reply gave
    an agent that calls ``search`` every step the id ``search_0`` on every turn
    — and every id-keyed consumer, memory's pairing repair above all, then
    matched the wrong group. Nothing about a call's content distinguishes two
    identical calls in two turns, so the id must carry entropy.
    """

    @staticmethod
    def _ollama_reply(*names: str) -> Any:
        return SimpleNamespace(
            tool_calls=[
                SimpleNamespace(function=SimpleNamespace(name=n, arguments={"q": "x"}))
                for n in names
            ]
        )

    @staticmethod
    def _google_reply(*names: str) -> Any:
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                function_call=SimpleNamespace(name=n, args={"q": "x"}, id=None)
                            )
                            for n in names
                        ]
                    )
                )
            ]
        )

    def test_ollama_ids_differ_across_turns(self) -> None:
        from spectragent.core.llm.serializers.ollama_format import OllamaMessageSerializer

        first = OllamaMessageSerializer.parse_tool_calls(self._ollama_reply("search"))
        second = OllamaMessageSerializer.parse_tool_calls(self._ollama_reply("search"))
        assert first[0].id != second[0].id

    def test_ollama_ids_differ_within_one_parallel_reply(self) -> None:
        from spectragent.core.llm.serializers.ollama_format import OllamaMessageSerializer

        calls = OllamaMessageSerializer.parse_tool_calls(self._ollama_reply("search", "search"))
        assert len({c.id for c in calls}) == 2

    def test_google_ids_differ_across_turns(self) -> None:
        from spectragent.core.llm.serializers.google_format import GoogleMessageSerializer

        first = GoogleMessageSerializer.parse_tool_calls(self._google_reply("search"))
        second = GoogleMessageSerializer.parse_tool_calls(self._google_reply("search"))
        assert first[0].id != second[0].id

    def test_google_keeps_the_providers_own_id_when_there_is_one(self) -> None:
        """Nothing is invented when Gemini does supply an id."""
        from spectragent.core.llm.serializers.google_format import GoogleMessageSerializer

        reply = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                function_call=SimpleNamespace(
                                    name="search", args={}, id="provider_id"
                                )
                            )
                        ]
                    )
                )
            ]
        )
        assert GoogleMessageSerializer.parse_tool_calls(reply)[0].id == "provider_id"

    def test_the_tool_name_is_still_readable_in_the_id(self) -> None:
        """Traces are read by humans; the entropy is a suffix, not a replacement."""
        from spectragent.core.llm.serializers.ollama_format import OllamaMessageSerializer

        [call] = OllamaMessageSerializer.parse_tool_calls(self._ollama_reply("search"))
        assert call.id.startswith("search_")
        assert call.function.name == "search"


class TestSerializersThatCannotRepresentTools:
    """Two paths raise rather than mangle — both are wired to
    ``supports_tools = False`` on their provider."""

    def test_oci_raises(self) -> None:
        from spectragent.core.llm.serializers.oci_format import OCIRawMessageSerializer

        with pytest.raises(ValueError, match="cannot represent tool results"):
            OCIRawMessageSerializer.serialize_messages([_result()])

    def test_oci_cohere_raises(self) -> None:
        from spectragent.core.llm.serializers.oci_format import OCIRawMessageSerializer

        with pytest.raises(ValueError, match="cannot represent tool results"):
            OCIRawMessageSerializer.serialize_messages_for_cohere([_result()])

    def test_responses_api_raises(self) -> None:
        from spectragent.core.llm.serializers.openai_responses import ResponsesAPIMessageSerializer

        with pytest.raises(ValueError, match="function_call_output"):
            ResponsesAPIMessageSerializer.serialize_messages([_result()])


# ── 3. ToolDefinition ─────────────────────────────────────────────


class TestToolDefinition:
    def test_from_model_flattens_refs(self) -> None:
        class _Nested(BaseModel):
            x: int

        class _Args(BaseModel):
            """Do the thing."""

            nested: _Nested

        tool = ToolDefinition.from_model(_Args)
        assert tool.name == "_Args"
        assert tool.description == "Do the thing."
        assert "$defs" not in tool.parameters
        assert "$ref" not in json.dumps(tool.parameters)
        assert tool.parameters["properties"]["nested"]["properties"]["x"]["type"] == "integer"

    def test_strict_survives_a_closed_model(self) -> None:
        class _Args(BaseModel):
            a: str

        assert ToolDefinition.from_model(_Args, strict=True).strict is True

    def test_strict_downgrades_for_an_open_map(self) -> None:
        """Same rule as the structured-output path — an open ``dict[str, Any]``
        cannot satisfy the strict dialect, so the request gives strict up."""

        class _Args(BaseModel):
            payload: dict[str, Any]

        assert ToolDefinition.from_model(_Args, strict=True).strict is False

    def test_zero_argument_tool_is_valid(self) -> None:
        assert ToolDefinition(name="ping").parameters == {"type": "object", "properties": {}}


# ── 4. Providers: tools reach the wire ────────────────────────────


class TestOpenAIProviderTools:
    def _chat(self):
        from spectragent.core.llm.providers.openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o", api_key="test")

    async def test_tools_reach_the_request(self) -> None:
        captured: dict[str, Any] = {}
        chat = self._chat()
        chat.get_client = lambda: _fake_openai_client(captured)  # type: ignore[method-assign]

        await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])

        assert captured["tools"][0]["function"]["name"] == "get_weather"
        assert captured["tool_choice"] == "auto"

    async def test_tool_choice_is_forwarded(self) -> None:
        captured: dict[str, Any] = {}
        chat = self._chat()
        chat.get_client = lambda: _fake_openai_client(captured)  # type: ignore[method-assign]

        await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()], tool_choice="required")
        assert captured["tool_choice"] == "required"

    async def test_no_tools_key_when_none_passed(self) -> None:
        """Providers must not start sending an empty tools list to every call."""
        captured: dict[str, Any] = {}
        chat = self._chat()
        chat.get_client = lambda: _fake_openai_client(captured)  # type: ignore[method-assign]

        await chat.ainvoke([UserMessage(content="hi")])
        assert "tools" not in captured
        assert "tool_choice" not in captured

    async def test_tool_calls_come_back_on_the_completion(self) -> None:
        captured: dict[str, Any] = {}
        raw = [
            SimpleNamespace(
                id="call_abc",
                function=SimpleNamespace(name="get_weather", arguments='{"city": "SF"}'),
            )
        ]
        chat = self._chat()
        chat.get_client = lambda: _fake_openai_client(  # type: ignore[method-assign]
            captured, tool_calls=raw, content=None
        )

        result = await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])

        assert result.completion == ""
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_abc"
        assert result.tool_calls[0].function.name == "get_weather"
        assert result.stop_reason == "tool_calls"

    async def test_tool_calls_are_empty_on_a_plain_text_reply(self) -> None:
        captured: dict[str, Any] = {}
        chat = self._chat()
        chat.get_client = lambda: _fake_openai_client(captured)  # type: ignore[method-assign]

        result = await chat.ainvoke([UserMessage(content="hi")])
        assert result.tool_calls == []

    async def test_structured_output_alone_still_works(self) -> None:
        """Guard against over-rejecting: output_format without tools is the
        normal path and must be untouched."""

        class _Out(BaseModel):
            a: str

        captured: dict[str, Any] = {}
        chat = self._chat()
        chat.get_client = lambda: _fake_openai_client(  # type: ignore[method-assign]
            captured, content='{"a": "x"}'
        )

        result = await chat.ainvoke([UserMessage(content="hi")], _Out)
        assert result.completion.a == "x"
        assert "response_format" in captured
        assert "tools" not in captured

    async def test_a_full_tool_round_trip_reaches_the_wire(self) -> None:
        """assistant(tool_calls) -> tool result -> next request."""
        captured: dict[str, Any] = {}
        chat = self._chat()
        chat.get_client = lambda: _fake_openai_client(captured)  # type: ignore[method-assign]

        await chat.ainvoke(
            [
                SystemMessage(content="sys"),
                UserMessage(content="weather?"),
                _assistant(_call()),
                _result(),
            ],
            tools=[_tool()],
        )

        assert [m["role"] for m in captured["messages"]] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]


class TestAnthropicProviderTools:
    def _chat(self):
        from spectragent.core.llm.providers.anthropic import ChatAnthropic

        return ChatAnthropic(model="claude-sonnet-4-20250514", api_key="test")

    def _client(self, captured: dict[str, Any], content: list[Any]):
        from anthropic.types import Message

        class _Messages:
            async def create(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return Message.model_construct(
                    id="msg_1",
                    content=content,
                    model="claude-sonnet-4-20250514",
                    role="assistant",
                    stop_reason="tool_use",
                    type="message",
                    usage=SimpleNamespace(
                        input_tokens=1,
                        output_tokens=1,
                        cache_read_input_tokens=None,
                        cache_creation_input_tokens=None,
                    ),
                )

        return SimpleNamespace(messages=_Messages())

    async def test_tools_reach_the_request(self) -> None:
        from anthropic.types.text_block import TextBlock

        captured: dict[str, Any] = {}
        chat = self._chat()
        block = TextBlock.model_construct(type="text", text="hi", citations=None)
        chat.get_client = lambda: self._client(captured, [block])  # type: ignore[method-assign]

        await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])

        assert captured["tools"][0]["name"] == "get_weather"
        assert captured["tool_choice"] == {"type": "auto"}

    async def test_required_maps_to_any(self) -> None:
        from anthropic.types.text_block import TextBlock

        captured: dict[str, Any] = {}
        chat = self._chat()
        block = TextBlock.model_construct(type="text", text="hi", citations=None)
        chat.get_client = lambda: self._client(captured, [block])  # type: ignore[method-assign]

        await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()], tool_choice="required")
        assert captured["tool_choice"] == {"type": "any"}

    async def test_tool_use_reply_produces_tool_calls_and_no_text(self) -> None:
        captured: dict[str, Any] = {}
        chat = self._chat()
        block = SimpleNamespace(
            type="tool_use", id="toolu_1", name="get_weather", input={"city": "SF"}
        )
        chat.get_client = lambda: self._client(captured, [block])  # type: ignore[method-assign]

        result = await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])

        assert result.completion == ""
        assert result.tool_calls[0].function.name == "get_weather"
        assert result.stop_reason == "tool_use"


class TestOtherProvidersSendTools:
    async def test_ollama(self) -> None:
        from spectragent.core.llm.providers.ollama import ChatOllama

        captured: dict[str, Any] = {}

        class _Client:
            async def chat(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return SimpleNamespace(
                    message=SimpleNamespace(content="hi", tool_calls=None),
                    prompt_eval_count=1,
                    eval_count=1,
                )

        chat = ChatOllama(model="llama3.1")
        chat.get_client = lambda: _Client()  # type: ignore[method-assign]

        await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])
        assert captured["tools"][0]["function"]["name"] == "get_weather"

    async def test_ollama_parses_tool_calls(self) -> None:
        from spectragent.core.llm.providers.ollama import ChatOllama

        class _Client:
            async def chat(self, **kwargs: Any) -> Any:
                return SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="get_weather", arguments={"city": "SF"}
                                )
                            )
                        ],
                    ),
                    prompt_eval_count=1,
                    eval_count=1,
                )

        chat = ChatOllama(model="llama3.1")
        chat.get_client = lambda: _Client()  # type: ignore[method-assign]

        result = await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])
        assert result.tool_calls[0].function.name == "get_weather"

    async def test_google(self) -> None:
        from spectragent.core.llm.providers.google import ChatGoogle

        captured: dict[str, Any] = {}

        class _Models:
            async def generate_content(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return SimpleNamespace(
                    text="hi", candidates=[], usage_metadata=None, prompt_feedback=None
                )

        chat = ChatGoogle(model="gemini-2.5-pro", api_key="test")
        chat._client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))  # type: ignore[assignment]

        await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])

        declarations = captured["config"]["tools"][0]["function_declarations"]
        assert declarations[0]["name"] == "get_weather"
        assert captured["config"]["tool_config"]["function_calling_config"]["mode"] == "AUTO"

    async def test_mistral(self) -> None:
        from spectragent.core.llm.providers.mistral import ChatMistral

        captured: dict[str, Any] = {}
        chat = ChatMistral(model="mistral-medium-latest", api_key="test")

        async def _post(payload: dict[str, Any]) -> dict[str, Any]:
            captured.update(payload)
            return {"choices": [{"message": {"content": "hi"}}], "usage": None}

        chat._post = _post  # type: ignore[method-assign]
        await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])
        assert captured["tools"][0]["function"]["name"] == "get_weather"

    async def test_groq_text_path(self) -> None:
        """Only Groq's *structured* path conflicts with tools; plain calls still
        advertise them."""
        from spectragent.core.llm.providers.openai_groq import ChatGroq

        captured: dict[str, Any] = {}
        chat = ChatGroq(model="llama-3.3-70b-versatile", api_key="test")
        chat.get_client = lambda: _fake_openai_client(captured)  # type: ignore[method-assign]

        await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])
        assert captured["tools"][0]["function"]["name"] == "get_weather"
        assert captured["tool_choice"] == "auto"


# ── 5. Fail loudly, never silently drop ───────────────────────────


class TestProvidersThatCannotFailLoudly:
    """The one behaviour that must never happen is accepting ``tools`` and
    ignoring them: the model then simply never calls anything, which is
    indistinguishable from a bad prompt."""

    def test_oci_reports_and_raises(self) -> None:
        from spectragent.core.llm.providers.oci import ChatOCI

        chat = ChatOCI(model_id="ocid1.x", service_endpoint="https://x", compartment_id="ocid1.c")
        assert chat.supports_tools is False
        with pytest.raises(ToolsUnsupportedError, match="does not implement native function"):
            import asyncio

            asyncio.run(chat.ainvoke([UserMessage(content="hi")], tools=[_tool()]))

    def test_browser_use_reports_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BROWSER_USE_API_KEY", "test")
        from spectragent.core.llm.providers.browser_use import ChatBrowserUse

        chat = ChatBrowserUse(model="bu-latest")
        assert chat.supports_tools is False
        with pytest.raises(ToolsUnsupportedError):
            import asyncio

            asyncio.run(chat.ainvoke([UserMessage(content="hi")], tools=[_tool()]))

    async def test_azure_responses_path_raises(self) -> None:
        from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI

        chat = ChatAzureOpenAI(model="gpt-5-codex", api_key="test", azure_endpoint="https://x")
        assert chat._should_use_responses_api() is True
        assert chat.supports_tools is False
        with pytest.raises(ToolsUnsupportedError, match="Responses API"):
            await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])

    async def test_azure_chat_path_supports_tools(self) -> None:
        from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI

        captured: dict[str, Any] = {}
        chat = ChatAzureOpenAI(model="gpt-4o", api_key="test", azure_endpoint="https://x")
        assert chat.supports_tools is True
        chat.get_client = lambda: _fake_openai_client(captured)  # type: ignore[method-assign]

        await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()])
        assert captured["tools"][0]["function"]["name"] == "get_weather"


class TestStructuredOutputAndToolsConflict:
    """Structured output claims the whole reply (or the only tool slot), so
    combining the two is rejected rather than half-honoured — on every family.

    OpenAI's ``response_format`` is the one case where the *wire* would accept
    both, and it was allowed for that reason. But the two replies OpenAI may
    then send — structured JSON, or a tool call with ``content: null`` — are not
    both representable here: ``ChatInvokeCompletion.completion`` is
    non-optional. Forwarding them spent a round trip and turned the tool-call
    reply into a 500-shaped parse failure that discarded the calls. Refusing up
    front is the honest version until ``completion`` can express "called a tool
    instead of answering".
    """

    class _Out(BaseModel):
        a: str

    async def test_anthropic_native_structured_output(self) -> None:
        from spectragent.core.llm.providers.anthropic import ChatAnthropic

        chat = ChatAnthropic(model="claude-sonnet-4-20250514", api_key="test")
        with pytest.raises(ToolsUnsupportedError, match="output_config.format"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_anthropic_forced_tool_fallback(self) -> None:
        from spectragent.core.llm.providers.anthropic import ChatAnthropic

        chat = ChatAnthropic(
            model="claude-sonnet-4-20250514", api_key="test", use_native_structured_output=False
        )
        with pytest.raises(ToolsUnsupportedError, match="forced tool-use fallback"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_deepseek(self) -> None:
        from spectragent.core.llm.providers.openai_deepseek import ChatDeepSeek

        chat = ChatDeepSeek(model="deepseek-chat", api_key="test")
        with pytest.raises(ToolsUnsupportedError, match="no free tool slot"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_cerebras(self) -> None:
        from spectragent.core.llm.providers.openai_cerebras import ChatCerebras

        chat = ChatCerebras(model="llama3.1-8b", api_key="test")
        with pytest.raises(ToolsUnsupportedError, match="JSON only"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_ollama(self) -> None:
        from spectragent.core.llm.providers.ollama import ChatOllama

        chat = ChatOllama(model="llama3.1")
        with pytest.raises(ToolsUnsupportedError, match="decoding grammar"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_google(self) -> None:
        from spectragent.core.llm.providers.google import ChatGoogle

        chat = ChatGoogle(model="gemini-2.5-pro", api_key="test")
        with pytest.raises(ToolsUnsupportedError, match="response schema"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_mistral(self) -> None:
        from spectragent.core.llm.providers.mistral import ChatMistral

        chat = ChatMistral(model="mistral-medium-latest", api_key="test")
        with pytest.raises(ToolsUnsupportedError, match="json_schema response_format"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_vercel_prompt_json_path(self) -> None:
        from spectragent.core.llm.providers.openai_vercel import ChatVercel

        chat = ChatVercel(model="google/gemini-2.5-pro", api_key="test")
        with pytest.raises(ToolsUnsupportedError, match="prompt-JSON fallback"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_openai(self) -> None:
        from spectragent.core.llm.providers.openai import ChatOpenAI

        chat = ChatOpenAI(model="gpt-4o", api_key="test")
        with pytest.raises(ToolsUnsupportedError, match="no value that can represent it"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_openai_refuses_before_the_round_trip(self) -> None:
        """The forced case, which is what made this reachable rather than
        theoretical: ``tool_choice='required'`` reached the wire, so the only
        reply OpenAI was allowed to send was a tool call with ``content=None``
        — which the structured path then raised on, discarding the calls.
        Nothing may be sent now.
        """
        from spectragent.core.llm.providers.openai import ChatOpenAI

        captured: dict[str, Any] = {}
        raw = [
            SimpleNamespace(
                id="call_abc",
                function=SimpleNamespace(name="get_weather", arguments='{"city": "SF"}'),
            )
        ]
        chat = ChatOpenAI(model="gpt-4o", api_key="test")
        chat.get_client = lambda: _fake_openai_client(  # type: ignore[method-assign]
            captured, tool_calls=raw, content=None
        )

        with pytest.raises(ToolsUnsupportedError):
            await chat.ainvoke(
                [UserMessage(content="hi")], self._Out, tools=[_tool()], tool_choice="required"
            )
        assert captured == {}

    async def test_openai_openrouter(self) -> None:
        from spectragent.core.llm.providers.openai_openrouter import ChatOpenRouter

        chat = ChatOpenRouter(model="openai/gpt-4o", api_key="test")
        with pytest.raises(ToolsUnsupportedError, match="no value that can represent it"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_azure_chat_path(self) -> None:
        from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI

        chat = ChatAzureOpenAI(model="gpt-4o", api_key="test", azure_endpoint="https://x")
        with pytest.raises(ToolsUnsupportedError, match="no value that can represent it"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_vercel_native_json_schema_path(self) -> None:
        from spectragent.core.llm.providers.openai_vercel import ChatVercel

        chat = ChatVercel(model="openai/gpt-4o", api_key="test")
        assert chat._vercel_needs_prompt_json() is False
        with pytest.raises(ToolsUnsupportedError, match="no value that can represent it"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])

    async def test_groq(self) -> None:
        """Groq is the one where the *vendor* also refuses: "Streaming and tool
        use are not currently supported with Structured Outputs"."""
        from spectragent.core.llm.providers.openai_groq import ChatGroq

        captured: dict[str, Any] = {}
        chat = ChatGroq(model="llama-3.3-70b-versatile", api_key="test")
        chat.get_client = lambda: _fake_openai_client(captured)  # type: ignore[method-assign]

        with pytest.raises(ToolsUnsupportedError, match="Structured Outputs"):
            await chat.ainvoke([UserMessage(content="hi")], self._Out, tools=[_tool()])
        assert captured == {}


class TestToolChoiceLimits:
    async def test_ollama_rejects_non_auto_tool_choice(self) -> None:
        """Ollama's chat endpoint has no tool_choice; claiming to honour one
        would be a promise the backend cannot keep."""
        from spectragent.core.llm.providers.ollama import ChatOllama

        chat = ChatOllama(model="llama3.1")
        with pytest.raises(ToolsUnsupportedError, match="no tool_choice parameter"):
            await chat.ainvoke([UserMessage(content="hi")], tools=[_tool()], tool_choice="required")

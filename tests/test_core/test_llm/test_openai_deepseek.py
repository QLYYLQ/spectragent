"""What ``openai.deepseek`` sends to DeepSeek's official API, and what it makes of the answer.

Every rule here was measured against https://api.deepseek.com on 2026-10-26
and is stated, with the error strings, in the provider's module docstring:
no ``json_schema`` response format, ``json_object`` only when the prompt names
JSON, ``max_completion_tokens`` silently ignored, ``temperature`` without
effect while thinking, ``frequency_penalty`` without effect at all, and two
``finish_reason`` values that mean the server, not the model, stopped.

The provider runs on the real OpenAI SDK behind ``httpx.MockTransport``, so
the assertions are on the JSON body that would have reached the wire.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field, create_model

from spectragent.core.actions.action_union import action_union
from spectragent.core.llm.exceptions import (
    ModelGenerationInterruptedError,
    ModelOutputTruncatedError,
    StructuredOutputValidationError,
)
from spectragent.core.llm.messages import (
    AssistantMessage,
    BaseMessage,
    ContentPartTextParam,
    SystemMessage,
    UserMessage,
)
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.providers.openai_deepseek import ChatDeepSeek
from spectragent.schemas.action import ActionDescriptor


class _Wire:
    """One scripted chat completion, and every request that reached it."""

    def __init__(self, content: str | None, *, finish_reason: str = "stop") -> None:
        self.answer = {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": "Thinking it over.",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 40,
                "total_tokens": 52,
                "completion_tokens_details": {"reasoning_tokens": 31},
            },
        }
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.answer)

    @property
    def body(self) -> dict[str, Any]:
        (request,) = self.requests
        return json.loads(request.content)


def _deepseek(wire: _Wire, **fields: Any) -> ChatDeepSeek:
    return ChatDeepSeek(
        model="deepseek-flash", api_key="stub", max_retries=0, http_client=wire.client(), **fields
    )


def _agent_output() -> type[BaseModel]:
    """The shape a browser run ships: a closed union of single-key verbs beside free text."""
    click = ActionDescriptor(
        name="browser.click",
        description="Click an element",
        params_schema={
            "type": "object",
            "properties": {"index": {"type": "integer"}},
            "required": ["index"],
        },
    )
    done = ActionDescriptor(
        name="done",
        description="Finish the task",
        params_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    item, _ = action_union([click, done])
    make: Any = create_model  # pydantic's dynamic model factory is typed for literal fields only
    return make(
        "AgentOutput",
        __config__=ConfigDict(extra="forbid"),
        memory=(str, ...),
        action=(list[item], Field(min_length=1)),
    )


_REPLY = json.dumps({"memory": "on the form", "action": [{"browser.click": {"index": 3}}]})
_PROMPT: list[BaseMessage] = [
    SystemMessage(content="You drive a browser."),
    UserMessage(content="Submit the form."),
]


def _system_text(body: dict[str, Any]) -> str:
    first = body["messages"][0]
    assert first["role"] == "system"
    content = first["content"]
    return content if isinstance(content, str) else "".join(part["text"] for part in content)


class TestWhatReachesTheOfficialApi:
    async def test_structured_output_is_json_object_with_the_schema_in_the_prompt(self) -> None:
        wire = _Wire(_REPLY)
        completion = await _deepseek(wire).ainvoke(_PROMPT, _agent_output())

        body = wire.body
        assert body["response_format"] == {"type": "json_object"}
        assert "tools" not in body and "tool_choice" not in body
        system = _system_text(body)
        assert system.startswith("You drive a browser.")
        assert '"browser.click"' in system and '"anyOf"' in system
        assert completion.completion.model_dump() == json.loads(_REPLY)
        assert completion.thinking == "Thinking it over."

    async def test_the_budget_goes_out_as_max_tokens(self) -> None:
        wire = _Wire("hi")
        await _deepseek(wire, max_completion_tokens=40).ainvoke(_PROMPT)

        assert wire.body["max_tokens"] == 40
        assert "max_completion_tokens" not in wire.body

    async def test_the_text_path_sends_no_response_format(self) -> None:
        wire = _Wire("hi")
        await _deepseek(wire).ainvoke(_PROMPT)

        assert "response_format" not in wire.body
        assert wire.body["messages"] == [
            {"role": "system", "content": "You drive a browser."},
            {"role": "user", "content": "Submit the form."},
        ]

    @pytest.mark.parametrize(
        "messages",
        [
            pytest.param([UserMessage(content="Submit the form.")], id="no system message"),
            pytest.param(_PROMPT, id="text system message"),
            pytest.param(
                [
                    SystemMessage(content=[ContentPartTextParam(text="You drive a browser.")]),
                    UserMessage(content="Submit the form."),
                ],
                id="system message of parts",
            ),
        ],
    )
    async def test_the_prompt_always_names_json(self, messages: Sequence[BaseMessage]) -> None:
        """``json_object`` is refused unless the prompt says "json" in some form."""
        wire = _Wire(_REPLY)
        await _deepseek(wire).ainvoke(messages, _agent_output())

        assert "json" in _system_text(wire.body).lower()
        assert wire.body["messages"][-1] == {"role": "user", "content": "Submit the form."}

    async def test_the_callers_messages_are_not_modified(self) -> None:
        messages: list[BaseMessage] = [
            SystemMessage(content="You drive a browser."),
            UserMessage(content="Submit the form."),
        ]
        await _deepseek(_Wire(_REPLY)).ainvoke(messages, _agent_output())
        assert messages[0].content == "You drive a browser."

    @pytest.mark.parametrize(
        ("effort", "sent", "temperature"),
        [
            ("auto", None, None),
            ("none", "none", 0.2),
            ("low", "low", None),
            ("high", "high", None),
            ("max", "max", None),
        ],
    )
    async def test_the_effort_and_the_sampling_values_that_take_effect(
        self, effort: str, sent: str | None, temperature: float | None
    ) -> None:
        """``auto`` sends nothing (the server thinks at ``high``); thinking makes temperature moot."""
        wire = _Wire(_REPLY)
        await _deepseek(wire, reasoning_effort=effort).ainvoke(_PROMPT, _agent_output())

        assert wire.body.get("reasoning_effort") == sent
        assert wire.body.get("temperature") == temperature
        assert "frequency_penalty" not in wire.body
        assert "thinking" not in wire.body

    async def test_a_trailing_assistant_message_is_a_prefix_on_beta(self) -> None:
        messages = [*_PROMPT, AssistantMessage(content='{"memory": "')]
        beta = _Wire("x")
        await _deepseek(beta, base_url="https://api.deepseek.com/beta").ainvoke(messages)
        assert beta.body["messages"][-1]["prefix"] is True

        stable = _Wire("x")
        await _deepseek(stable).ainvoke(messages)
        assert "prefix" not in stable.body["messages"][-1]

    def test_the_timeout_reaches_the_client(self) -> None:
        assert (
            ChatDeepSeek(model="deepseek-flash", api_key="k", timeout=12.5).get_client().timeout
            == 12.5
        )


class TestTheOppositeHoldsElsewhere:
    """The prompt path is the family's rule, not every OpenAI-compatible request's."""

    async def test_a_gpt_model_on_openai_direct_still_sends_json_schema(self) -> None:
        wire = _Wire(_REPLY)
        chat = ChatOpenAI(model="gpt-4o", api_key="stub", max_retries=0, http_client=wire.client())
        await chat.ainvoke(_PROMPT, _agent_output())

        assert wire.body["response_format"]["type"] == "json_schema"
        assert wire.body["messages"][0] == {"role": "system", "content": "You drive a browser."}
        assert wire.body["max_completion_tokens"] == 4096

    async def test_a_claude_model_on_openai_direct_takes_the_schema_in_the_prompt(self) -> None:
        """No ``response_format`` at all: only DeepSeek's endpoint asks for ``json_object``."""
        wire = _Wire(_REPLY)
        chat = ChatOpenAI(
            model="claude-sonnet-4-5", api_key="stub", max_retries=0, http_client=wire.client()
        )
        await chat.ainvoke(_PROMPT, _agent_output())

        assert "response_format" not in wire.body
        assert '"browser.click"' in _system_text(wire.body)


class TestFinishReasons:
    @pytest.mark.parametrize("structured", [False, True], ids=["text", "structured"])
    async def test_length_is_the_ceiling_not_the_schema(self, structured: bool) -> None:
        wire = _Wire('{"memory": "half', finish_reason="length")
        chat = _deepseek(wire, max_completion_tokens=40)
        with pytest.raises(ModelOutputTruncatedError) as caught:
            await chat.ainvoke(_PROMPT, _agent_output() if structured else None)

        # The knob a user raises, even though the wire field is ``max_tokens``.
        assert (caught.value.ceiling_field, caught.value.ceiling) == ("max_completion_tokens", 40)
        assert caught.value.partial_output == '{"memory": "half'
        assert caught.value.usage is not None

    @pytest.mark.parametrize("reason", ["insufficient_system_resource", "aborted"])
    @pytest.mark.parametrize("structured", [False, True], ids=["text", "structured"])
    async def test_a_server_stop_is_a_provider_error(self, reason: str, structured: bool) -> None:
        wire = _Wire('{"memory": "half', finish_reason=reason)
        with pytest.raises(ModelGenerationInterruptedError) as caught:
            await _deepseek(wire).ainvoke(_PROMPT, _agent_output() if structured else None)

        error = caught.value
        assert not isinstance(error, (StructuredOutputValidationError, ModelOutputTruncatedError))
        assert error.status_code == 503
        assert error.stop_reason == reason
        assert error.partial_output == '{"memory": "half'
        assert error.usage is not None and error.usage.completion_reasoning_tokens == 31
        assert "half" not in str(error)

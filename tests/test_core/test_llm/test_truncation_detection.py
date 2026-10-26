"""A reply cut off at the token ceiling must be named as such, on every provider.

Left undetected it arrives downstream as half-written JSON, fails schema
validation, and the run reports that the model would not follow the schema —
charging our own budget to the model under test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock
from google.genai.types import Candidate, Content, FinishReason, FunctionCall, Part
from pydantic import BaseModel

from spectragent.core.llm.exceptions import (
    ModelOutputTruncatedError,
    StructuredOutputValidationError,
)
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.providers.anthropic import ChatAnthropic
from spectragent.core.llm.providers.google import ChatGoogle
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.views import ModelInvokeUsage
from tests._helpers.llm_clients import FakeClient


def _usage(completion_tokens: int = 8192) -> ModelInvokeUsage:
    return ModelInvokeUsage(
        prompt_tokens=1000,
        prompt_cached_tokens=0,
        prompt_cache_creation_tokens=0,
        prompt_image_tokens=None,
        completion_tokens=completion_tokens,
        total_tokens=1000 + completion_tokens,
    )


class TestTheExceptionItself:
    def test_status_code_is_outside_every_retryable_set(self) -> None:
        """The 400 is load-bearing, not decorative.

        ``ModelProviderError`` defaults to 502, which is *inside*
        ``ChatGoogle.retryable_status_codes`` — so a truncation raised with
        the default would be retried five times with backoff, each attempt a
        fresh maximum-length generation. This test ties the two together so
        the pairing cannot drift apart silently.
        """
        exc = ModelOutputTruncatedError(model="m", stop_reason="max_tokens", ceiling=8192)
        assert exc.status_code == 400

        retryable = ChatGoogle(model="gemini-test", api_key="k").retryable_status_codes
        assert exc.status_code not in retryable

    def test_is_not_a_schema_violation(self) -> None:
        """The Loop routes these two to opposite outcomes; they must not overlap."""
        exc = ModelOutputTruncatedError(model="m", stop_reason="max_tokens", ceiling=8192)
        assert not isinstance(exc, StructuredOutputValidationError)

    def test_message_names_the_knob_to_turn(self) -> None:
        exc = ModelOutputTruncatedError(
            model="m",
            stop_reason="max_tokens",
            ceiling_field="max_output_tokens",
            ceiling=4096,
            usage=_usage(4096),
        )
        assert "max_output_tokens=4096" in str(exc)
        assert "completion_tokens=4096" in str(exc)

    def test_carries_the_usage_the_call_was_billed_for(self) -> None:
        usage = _usage()
        exc = ModelOutputTruncatedError(model="m", stop_reason="max_tokens", usage=usage)
        assert exc.usage is usage

    def test_carries_what_the_reply_had_written(self) -> None:
        exc = ModelOutputTruncatedError(
            model="m", stop_reason="max_tokens", partial_output='{"memory": "half'
        )
        assert exc.partial_output == '{"memory": "half'

    def test_partial_output_defaults_to_none_and_is_not_raw_output(self) -> None:
        """``raw_output`` means "as the validator saw it" — nothing validated this."""
        exc = ModelOutputTruncatedError(model="m", stop_reason="max_tokens")
        assert exc.partial_output is None
        assert not hasattr(exc, "raw_output")

    def test_the_partial_text_stays_out_of_the_message(self) -> None:
        """It can be tens of thousands of tokens; the message is a log line."""
        exc = ModelOutputTruncatedError(
            model="m", stop_reason="max_tokens", partial_output="x" * 500
        )
        assert "x" * 50 not in str(exc)


# ── OpenAI-compatible: both raise sites ─────────────────────────────


class _Reply(BaseModel):
    memory: str


class _LengthCappedClient(FakeClient):
    """Replays one OpenAI chat completion that stopped at ``finish_reason="length"``."""

    def __init__(self, content: str | None) -> None:
        super().__init__()
        self._content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **_kwargs: Any) -> object:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self._content, tool_calls=None),
                    finish_reason="length",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=2000,
                completion_tokens=16384,
                total_tokens=18384,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
        )


class TestOpenAI:
    @staticmethod
    def _chat(client: _LengthCappedClient, monkeypatch: pytest.MonkeyPatch) -> ChatOpenAI:
        chat = ChatOpenAI(model="gpt-4o", api_key="test", max_completion_tokens=16384)
        monkeypatch.setattr(chat, "get_client", lambda: client)
        return chat

    async def test_the_text_path_keeps_the_partial_reply(self, monkeypatch) -> None:
        chat = self._chat(_LengthCappedClient("I will first open the pricing pa"), monkeypatch)
        with pytest.raises(ModelOutputTruncatedError) as caught:
            await chat.ainvoke([UserMessage(content="hi")])
        assert caught.value.partial_output == "I will first open the pricing pa"
        assert caught.value.usage is not None

    async def test_the_structured_path_keeps_the_half_written_json(self, monkeypatch) -> None:
        half = '{"memory": "m", "action": [{"browser.cli'
        chat = self._chat(_LengthCappedClient(half), monkeypatch)
        with pytest.raises(ModelOutputTruncatedError) as caught:
            await chat.ainvoke([UserMessage(content="hi")], output_format=_Reply)
        assert caught.value.partial_output == half

    async def test_a_reply_with_no_text_says_so(self, monkeypatch) -> None:
        """``None`` is "no text came back" — not an invented empty string."""
        chat = self._chat(_LengthCappedClient(None), monkeypatch)
        with pytest.raises(ModelOutputTruncatedError) as caught:
            await chat.ainvoke([UserMessage(content="hi")])
        assert caught.value.partial_output is None


class TestAnthropic:
    @staticmethod
    def _response(stop_reason: str, content: list[Any] | None = None) -> object:
        return SimpleNamespace(stop_reason=stop_reason, content=content or [])

    @staticmethod
    def _truncated(llm: ChatAnthropic, *content: Any) -> ModelOutputTruncatedError:
        response = SimpleNamespace(stop_reason="max_tokens", content=list(content))
        with pytest.raises(ModelOutputTruncatedError) as caught:
            llm._reject_if_truncated(response, _usage())  # type: ignore[arg-type]
        return caught.value

    def test_max_tokens_is_refused(self) -> None:
        llm = ChatAnthropic(model="claude-test", api_key="k", max_tokens=8192)
        with pytest.raises(ModelOutputTruncatedError) as caught:
            llm._reject_if_truncated(self._response("max_tokens"), _usage())  # type: ignore[arg-type]
        assert caught.value.ceiling == 8192
        assert caught.value.usage is not None

    def test_the_partial_text_goes_with_the_error(self) -> None:
        llm = ChatAnthropic(model="claude-test", api_key="k")
        exc = self._truncated(llm, TextBlock(type="text", text='{"memory": "half'))
        assert exc.partial_output == '{"memory": "half'

    def test_a_forced_tool_reply_keeps_its_input(self) -> None:
        """With ``use_native_structured_output=False`` the reply *is* the tool input."""
        llm = ChatAnthropic(model="claude-test", api_key="k")
        tool_input = {"memory": "m", "action": [{"browser.click": {"index": 3}}]}
        exc = self._truncated(
            llm, ToolUseBlock(type="tool_use", id="t1", name="Reply", input=tool_input)
        )
        assert exc.partial_output is not None
        assert json.loads(exc.partial_output) == tool_input

    def test_blocks_keep_their_order_and_thinking_is_not_the_reply(self) -> None:
        llm = ChatAnthropic(model="claude-test", api_key="k")
        exc = self._truncated(
            llm,
            ThinkingBlock(type="thinking", thinking="private reasoning", signature="sig"),
            TextBlock(type="text", text="Clicking the button."),
            ToolUseBlock(type="tool_use", id="t1", name="click", input={"index": 3}),
        )
        assert exc.partial_output == 'Clicking the button.\n{"index": 3}'

    def test_a_reply_with_nothing_written_says_so(self) -> None:
        llm = ChatAnthropic(model="claude-test", api_key="k")
        exc = self._truncated(
            llm, ThinkingBlock(type="thinking", thinking="ran out here", signature="sig")
        )
        assert exc.partial_output is None

    @pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use", "stop_sequence"])
    def test_ordinary_stop_reasons_pass(self, stop_reason: str) -> None:
        llm = ChatAnthropic(model="claude-test", api_key="k")
        llm._reject_if_truncated(self._response(stop_reason), _usage())  # type: ignore[arg-type]

    def test_every_return_path_is_guarded(self) -> None:
        """Text, native-structured and tool-use each return separately."""
        import inspect

        from spectragent.core.llm.providers import anthropic_base

        source = inspect.getsource(anthropic_base.AnthropicCompatBase.ainvoke)
        assert source.count("self._reject_if_truncated(") == 3


class TestGoogle:
    @staticmethod
    def _response(finish_reason: FinishReason | None) -> object:
        return SimpleNamespace(candidates=[SimpleNamespace(finish_reason=finish_reason)])

    @staticmethod
    def _truncated(llm: ChatGoogle, *parts: Part) -> ModelOutputTruncatedError:
        candidate = Candidate(
            finish_reason=FinishReason.MAX_TOKENS,
            content=Content(role="model", parts=list(parts)) if parts else None,
        )
        response = SimpleNamespace(candidates=[candidate])
        with pytest.raises(ModelOutputTruncatedError) as caught:
            llm._reject_if_truncated(response, _usage())  # type: ignore[arg-type]
        return caught.value

    def test_max_tokens_is_refused(self) -> None:
        llm = ChatGoogle(model="gemini-test", api_key="k", max_output_tokens=4096)
        exc = self._truncated(llm, Part(text="{}"))
        assert exc.ceiling == 4096

    def test_the_partial_text_goes_with_the_error(self) -> None:
        llm = ChatGoogle(model="gemini-test", api_key="k")
        exc = self._truncated(llm, Part(text='{"memory": "m", '), Part(text='"action": [{"brow'))
        assert exc.partial_output == '{"memory": "m", "action": [{"brow'

    def test_parts_without_text_and_thoughts_are_skipped(self) -> None:
        llm = ChatGoogle(model="gemini-test", api_key="k")
        exc = self._truncated(
            llm,
            Part(text="weighing the options", thought=True),
            Part(function_call=FunctionCall(name="click", args={"index": 3})),
            Part(text="Clicking."),
        )
        assert exc.partial_output == "Clicking."

    def test_a_candidate_with_no_text_says_so(self) -> None:
        llm = ChatGoogle(model="gemini-test", api_key="k")
        assert self._truncated(llm).partial_output is None
        exc = self._truncated(llm, Part(function_call=FunctionCall(name="click", args={})))
        assert exc.partial_output is None

    def test_stop_passes(self) -> None:
        llm = ChatGoogle(model="gemini-test", api_key="k")
        llm._reject_if_truncated(self._response(FinishReason.STOP), _usage())  # type: ignore[arg-type]

    def test_no_candidates_passes(self) -> None:
        llm = ChatGoogle(model="gemini-test", api_key="k")
        llm._reject_if_truncated(SimpleNamespace(candidates=[]), _usage())  # type: ignore[arg-type]

    def test_the_string_form_is_not_what_it_looks_like(self) -> None:
        """Why the guard compares the enum and not ``_get_stop_reason``'s text.

        ``str(FinishReason.MAX_TOKENS)`` is ``'FinishReason.MAX_TOKENS'``, so
        the obvious ``== "MAX_TOKENS"`` never matches: the guard would look
        installed and never fire. If this assertion ever fails because the
        SDK changed its repr, the guard is still correct — but delete this
        test rather than "fixing" the guard to match a string.
        """
        llm = ChatGoogle(model="gemini-test", api_key="k")
        rendered = llm._get_stop_reason(self._response(FinishReason.MAX_TOKENS))  # type: ignore[arg-type]
        assert rendered != "MAX_TOKENS"
        assert rendered == "FinishReason.MAX_TOKENS"

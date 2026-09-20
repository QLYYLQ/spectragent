"""A reply cut off at the token ceiling must be named as such, on every provider.

Left undetected it arrives downstream as half-written JSON, fails schema
validation, and the run reports that the model would not follow the schema —
charging our own budget to the model under test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai.types import FinishReason

from spectragent.core.llm.exceptions import (
    ModelOutputTruncatedError,
    StructuredOutputValidationError,
)
from spectragent.core.llm.providers.anthropic import ChatAnthropic
from spectragent.core.llm.providers.google import ChatGoogle
from spectragent.core.llm.views import ChatInvokeUsage


def _usage(completion_tokens: int = 8192) -> ChatInvokeUsage:
    return ChatInvokeUsage(
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


class TestAnthropic:
    @staticmethod
    def _response(stop_reason: str) -> object:
        return SimpleNamespace(stop_reason=stop_reason)

    def test_max_tokens_is_refused(self) -> None:
        llm = ChatAnthropic(model="claude-test", api_key="k", max_tokens=8192)
        with pytest.raises(ModelOutputTruncatedError) as caught:
            llm._reject_if_truncated(self._response("max_tokens"), _usage())  # type: ignore[arg-type]
        assert caught.value.ceiling == 8192
        assert caught.value.usage is not None

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

    def test_max_tokens_is_refused(self) -> None:
        llm = ChatGoogle(model="gemini-test", api_key="k", max_output_tokens=4096)
        with pytest.raises(ModelOutputTruncatedError) as caught:
            llm._reject_if_truncated(self._response(FinishReason.MAX_TOKENS), _usage())  # type: ignore[arg-type]
        assert caught.value.ceiling == 4096

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

"""Token accounting must be faithful — the framework records tokens, never money.

The bug these guard: Anthropic reports three *disjoint* input counters, and
reading only one or two of them makes a run look cheaper the better its cache
worked. Nothing asserted on this seam, so a cached step reported a
``total_tokens`` smaller than its own ``prompt_tokens`` for as long as caching
had been on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import Message, Usage
from google.genai import types as genai_types
from oci.generative_ai_inference import models as oci_models
from openai.types.chat import ChatCompletion
from openai.types.responses import Response, ResponseUsage
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from spectragent.core.llm.providers.anthropic import ChatAnthropic
from spectragent.core.llm.providers.google import ChatGoogle
from spectragent.core.llm.providers.oci import ChatOCI
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.providers.openai_deepseek import ChatDeepSeek
from spectragent.schemas.agent_result import AgentUsage


def _anthropic_response(
    *, uncached: int, cache_read: int, cache_write: int, output: int
) -> Message:
    """A real SDK reply with independent cache-read, cache-write and input counts."""
    return Message(
        id="msg_usage",
        content=[],
        model="claude-sonnet-4-5",
        role="assistant",
        stop_reason="end_turn",
        stop_sequence=None,
        type="message",
        usage=Usage(
            input_tokens=uncached,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
            output_tokens=output,
        ),
    )


class TestAnthropicUsage:
    def test_all_three_input_counters_are_summed(self) -> None:
        usage = ChatAnthropic(model="claude-sonnet-4-5")._get_usage(
            _anthropic_response(uncached=200, cache_read=8000, cache_write=1500, output=300),
        )

        assert usage.prompt_tokens == 9700, "input_tokens excludes both cache counters"
        assert usage.prompt_cached_tokens == 8000
        assert usage.prompt_cache_creation_tokens == 1500
        assert usage.completion_tokens == 300
        assert usage.total_tokens == 10000

    def test_total_is_never_less_than_prompt(self) -> None:
        """The old arithmetic gave total=500 against prompt=8200 on this input."""
        usage = ChatAnthropic(model="claude-sonnet-4-5")._get_usage(
            _anthropic_response(uncached=200, cache_read=8000, cache_write=1500, output=300),
        )
        assert usage.total_tokens >= usage.prompt_tokens

    def test_uncached_call_is_unaffected(self) -> None:
        usage = ChatAnthropic(model="claude-sonnet-4-5")._get_usage(
            _anthropic_response(uncached=1200, cache_read=0, cache_write=0, output=400),
        )
        assert (usage.prompt_tokens, usage.total_tokens) == (1200, 1600)


class TestAgentUsageAggregation:
    def test_cache_write_tokens_survive_aggregation(self) -> None:
        """They are real billed tokens; dropping them under-reports the run."""
        total = AgentUsage()
        for _ in range(3):
            total.add_llm_usage(
                SimpleNamespace(
                    prompt_tokens=9700,
                    completion_tokens=300,
                    prompt_cached_tokens=8000,
                    prompt_cache_creation_tokens=1500,
                    total_tokens=10000,
                ),
                model="claude-opus-5",
            )

        assert total.total_prompt_cache_creation_tokens == 4500
        assert total.by_model is not None
        assert total.by_model["claude-opus-5"]["cache_creation_tokens"] == 4500

    def test_provider_reporting_no_cache_fields_still_aggregates(self) -> None:
        """``add_llm_usage`` is duck-typed: a partial reporter contributes what it has."""
        total = AgentUsage()
        total.add_llm_usage(
            SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)
        )
        assert total.total_prompt_cache_creation_tokens == 0
        assert total.total_tokens == 120

    def test_carries_no_cost_fields(self) -> None:
        """Pricing is out of scope — see AgentUsage's docstring."""
        assert not [name for name in AgentUsage.model_fields if name.endswith("cost")]


# ── How many completion tokens were reasoning ─────────────────────────


def _chat_completion(usage: dict[str, Any]) -> ChatCompletion:
    """A chat completion as DeepSeek's official API returns it, cache counters included."""
    return ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hi", "reasoning_content": "r"},
                }
            ],
            "usage": usage,
        }
    )


_DEEPSEEK_USAGE = {
    "prompt_tokens": 12,
    "completion_tokens": 40,
    "total_tokens": 52,
    "prompt_tokens_details": {"cached_tokens": 0},
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 12,
}


class TestReasoningTokenCounts:
    """``completion_reasoning_tokens`` is the share of ``completion_tokens`` the response states.

    ``None`` means the response stated none, which is not zero: a provider
    that bills reasoning without a split must not read as "no reasoning".
    """

    @pytest.mark.parametrize("chat", [ChatOpenAI, ChatDeepSeek])
    def test_chat_completions_read_the_details(self, chat: Any) -> None:
        response = _chat_completion(
            {**_DEEPSEEK_USAGE, "completion_tokens_details": {"reasoning_tokens": 31}}
        )
        usage = chat(model="deepseek-flash").get_usage(response)
        assert usage is not None
        assert (usage.completion_tokens, usage.completion_reasoning_tokens) == (40, 31)

    def test_chat_completions_without_details_report_none(self) -> None:
        usage = ChatDeepSeek(model="deepseek-flash").get_usage(_chat_completion(_DEEPSEEK_USAGE))
        assert usage is not None
        assert usage.completion_reasoning_tokens is None

    def test_a_reported_zero_stays_zero(self) -> None:
        response = _chat_completion(
            {**_DEEPSEEK_USAGE, "completion_tokens_details": {"reasoning_tokens": 0}}
        )
        usage = ChatOpenAI(model="gpt-4o").get_usage(response)
        assert usage is not None
        assert usage.completion_reasoning_tokens == 0

    def test_the_responses_api_reads_its_output_details(self) -> None:
        response = Response.model_construct(
            usage=ResponseUsage(
                input_tokens=10,
                input_tokens_details=InputTokensDetails(cached_tokens=0),
                output_tokens=30,
                output_tokens_details=OutputTokensDetails(reasoning_tokens=22),
                total_tokens=40,
            )
        )
        usage = ChatOpenAI(model="gpt-5").get_usage_from_responses(response)
        assert usage is not None
        assert (usage.completion_tokens, usage.completion_reasoning_tokens) == (30, 22)

    def test_gemini_counts_its_thoughts_inside_the_completion(self) -> None:
        """Thoughts are added to ``completion_tokens``, so the count is a share of it."""
        response = genai_types.GenerateContentResponse(
            usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10,
                candidates_token_count=8,
                thoughts_token_count=550,
                total_token_count=568,
            )
        )
        usage = ChatGoogle(model="gemini-3.5-flash", api_key="k")._get_usage(response)
        assert usage is not None
        assert (usage.completion_tokens, usage.completion_reasoning_tokens) == (558, 550)

    def test_gemini_without_thoughts_reports_none(self) -> None:
        response = genai_types.GenerateContentResponse(
            usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10, candidates_token_count=8, total_token_count=18
            )
        )
        usage = ChatGoogle(model="gemini-2.0-flash", api_key="k")._get_usage(response)
        assert usage is not None
        assert usage.completion_reasoning_tokens is None

    @pytest.mark.parametrize(("details", "expected"), [(5, 5), (None, None)])
    def test_oci_reads_its_completion_details(self, details: int | None, expected: Any) -> None:
        chat = ChatOCI(
            model_id="ocid1.generativeaimodel.oc1..stub",
            service_endpoint="https://oci.test",
            compartment_id="ocid1.compartment.oc1..stub",
        )
        reported = oci_models.Usage(prompt_tokens=3, completion_tokens=9, total_tokens=12)
        if details is not None:
            reported.completion_tokens_details = oci_models.CompletionTokensDetails(
                reasoning_tokens=details
            )
        response = SimpleNamespace(
            data=SimpleNamespace(chat_response=SimpleNamespace(usage=reported))
        )
        usage = chat._extract_usage(response)
        assert usage is not None
        assert usage.completion_reasoning_tokens == expected

    def test_anthropic_states_no_split(self) -> None:
        usage = ChatAnthropic(model="claude-sonnet-4-5")._get_usage(
            _anthropic_response(uncached=10, cache_read=0, cache_write=0, output=300)
        )
        assert usage.completion_reasoning_tokens is None

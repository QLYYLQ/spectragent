"""Token accounting must be faithful — the framework records tokens, never money.

The bug these guard: Anthropic reports three *disjoint* input counters, and
reading only one or two of them makes a run look cheaper the better its cache
worked. Nothing asserted on this seam, so a cached step reported a
``total_tokens`` smaller than its own ``prompt_tokens`` for as long as caching
had been on.
"""

from __future__ import annotations

from types import SimpleNamespace

from anthropic.types import Message, Usage

from spectragent.core.llm.providers.anthropic import ChatAnthropic
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

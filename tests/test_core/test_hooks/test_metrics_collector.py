"""Tests for MetricsCollector hook — M11.

The collector is an *enricher*: it fills gaps in whatever the Loop
measured and never overwrites it.
"""

import pytest

from spectragent.core.hooks._registrar import HookBase, HookRegistrar
from spectragent.core.hooks.metrics_collector import MetricsCollector
from spectragent.schemas.agent_result import AgentMetrics, AgentUsage


class _FakeUsage:
    def __init__(self, prompt=10, completion=5, cached=2, total=15):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_cached_tokens = cached
        self.total_tokens = total


class _FakeCompletion:
    def __init__(self, usage=None):
        self.completion = "hello"
        self.usage = usage


class _FakeLLM:
    model = "gpt-4o-mini"


class _FakeResult:
    """Mimics AgentResult with mutable metrics."""

    def __init__(self, metrics=None):
        self.metrics = metrics if metrics is not None else AgentMetrics(
            end_to_end_ms=0, steps=0
        )
        self.agent_metadata = {}


class TestMetricsCollectorRegistration:
    def test_registered_in_conscribe(self):
        cls = HookRegistrar.get("metrics")
        assert cls is MetricsCollector

    def test_registry_key(self):
        assert MetricsCollector.__registry_key__ == "metrics"

    def test_blocking(self):
        mc = MetricsCollector()
        assert mc.blocking is True

    def test_intercepts_ainvoke(self):
        methods = {i.method for i in MetricsCollector.__intercepts__}
        assert "ainvoke" in methods

    def test_enriches_before_writers_persist(self):
        """Priority must place enrichment ahead of serialization."""
        from spectragent.core.hooks.trace_writer import TraceWriter

        assert MetricsCollector.priority < TraceWriter.priority
        assert MetricsCollector.priority == HookBase.PRIORITY_ENRICH


class TestMetricsCollectorAccumulation:
    @pytest.fixture()
    def collector(self) -> MetricsCollector:
        return MetricsCollector()

    async def test_single_call_accumulation(self, collector):
        await collector.on_run_start()
        usage = _FakeUsage(prompt=10, completion=5, cached=2, total=15)
        await collector.after_ainvoke(
            _FakeCompletion(usage=usage), args=(), kwargs={}
        )
        assert collector._usage.total_prompt_tokens == 10
        assert collector._usage.total_completion_tokens == 5
        assert collector._usage.total_prompt_cached_tokens == 2
        assert collector._usage.total_tokens == 15
        assert collector.llm_calls == 1

    async def test_multi_call_accumulation(self, collector):
        await collector.on_run_start()
        for _ in range(3):
            usage = _FakeUsage(prompt=10, completion=5, cached=0, total=15)
            await collector.after_ainvoke(
                _FakeCompletion(usage=usage), args=(), kwargs={}
            )
        assert collector._usage.total_prompt_tokens == 30
        assert collector._usage.total_completion_tokens == 15
        assert collector._usage.total_tokens == 45
        assert collector.llm_calls == 3

    async def test_none_usage_handled(self, collector):
        await collector.on_run_start()
        await collector.after_ainvoke(
            _FakeCompletion(usage=None), args=(), kwargs={}
        )
        assert collector._usage.total_tokens == 0
        assert collector.llm_calls == 1

    async def test_by_model_filled_from_target(self, collector):
        """The model name lives on the LLM instance, not the call kwargs."""
        await collector.on_run_start()
        await collector.after_ainvoke(
            _FakeCompletion(usage=_FakeUsage(total=15)),
            args=(),
            kwargs={},
            target=_FakeLLM(),
            role="llm",
            method="ainvoke",
        )
        assert collector._usage.by_model is not None
        assert collector._usage.by_model["gpt-4o-mini"]["total_tokens"] == 15
        assert collector._usage.by_model["gpt-4o-mini"]["calls"] == 1

    async def test_on_run_end_adopts_metrics_when_unmeasured(self, collector):
        await collector.on_run_start()
        usage = _FakeUsage(prompt=20, completion=10, cached=5, total=30)
        await collector.after_ainvoke(
            _FakeCompletion(usage=usage), args=(), kwargs={}
        )

        result = _FakeResult()
        await collector.on_run_end(result=result, env=None)

        assert result.metrics.usage is not None
        assert result.metrics.usage.total_prompt_tokens == 20
        assert result.metrics.usage.total_completion_tokens == 10
        assert result.metrics.usage.total_tokens == 30
        assert result.metrics.llm_calls == 1

    async def test_on_run_end_never_overwrites_loop_step_count(self, collector):
        """steps (agent steps) and llm_calls are different quantities."""
        await collector.on_run_start()
        for _ in range(4):
            await collector.after_ainvoke(
                _FakeCompletion(usage=_FakeUsage(total=5)), args=(), kwargs={}
            )

        loop_metrics = AgentMetrics(
            end_to_end_ms=1234,
            steps=2,
            usage=AgentUsage(total_prompt_tokens=7, total_tokens=7),
        )
        result = _FakeResult(metrics=loop_metrics)
        await collector.on_run_end(result=result, env=None)

        assert result.metrics.steps == 2  # not 4
        assert result.metrics.llm_calls == 4
        assert result.metrics.end_to_end_ms == 1234
        assert result.metrics.usage is not None
        assert result.metrics.usage.total_tokens == 7  # loop's number stands

    async def test_on_run_end_leaves_usage_none_when_no_calls(self, collector):
        """No LLM call at all must not fabricate a zero-filled usage."""
        await collector.on_run_start()
        result = _FakeResult()
        await collector.on_run_end(result=result, env=None)
        assert result.metrics.usage is None

    async def test_reset_on_run_start(self, collector):
        """on_run_start resets all counters."""
        await collector.on_run_start()
        await collector.after_ainvoke(
            _FakeCompletion(usage=_FakeUsage()), args=(), kwargs={}
        )
        assert collector.llm_calls == 1

        # Second run should reset
        await collector.on_run_start()
        assert collector.llm_calls == 0
        assert collector._usage.total_tokens == 0


class TestACallWhoseReplyWasRefused:
    """A schema-rejected reply costs tokens and the run continues.

    ``ainvoke`` raises, so no ``after`` advice fires — but the Loop catches
    ``StructuredOutputValidationError``, hands the model its own text back
    and counts the call in the run's own total. Uncounted here, the
    collector and the result would disagree by one call and its tokens,
    and the gap would widen with every correction a run needed.
    """

    @pytest.fixture()
    def collector(self) -> MetricsCollector:
        return MetricsCollector()

    @staticmethod
    def _raiser(exc: Exception):
        async def proceed():
            raise exc

        return proceed

    def _rejected(self, usage=None):
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        return StructuredOutputValidationError(
            "schema mismatch",
            raw_output='{"action": [{}]}',
            model="gpt-4o-mini",
            usage=usage,
        )

    async def test_the_tokens_it_cost_are_counted(self, collector):
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        await collector.on_run_start()
        exc = self._rejected(_FakeUsage(prompt=10, completion=5, cached=0, total=15))
        with pytest.raises(StructuredOutputValidationError):
            await collector.around_ainvoke(
                proceed=self._raiser(exc), args=(), kwargs={}, target=_FakeLLM()
            )
        assert collector.llm_calls == 1
        assert collector._usage.total_tokens == 15
        assert collector._usage.by_model is not None

    async def test_a_transport_failure_is_not_counted(self, collector):
        """No reply, no tokens, no call — the honest answer for a 429."""
        await collector.on_run_start()
        with pytest.raises(TimeoutError):
            await collector.around_ainvoke(
                proceed=self._raiser(TimeoutError("gateway gone")), args=(), kwargs={}
            )
        assert collector.llm_calls == 0
        assert collector._usage.total_tokens == 0

    async def test_a_successful_call_is_counted_once(self, collector):
        """``around`` wraps ``after``; the success path stays ``after``'s."""
        await collector.on_run_start()

        async def proceed():
            completion = _FakeCompletion(usage=_FakeUsage(total=15))
            await collector.after_ainvoke(completion, args=(), kwargs={})
            return completion

        await collector.around_ainvoke(proceed=proceed, args=(), kwargs={})
        assert collector.llm_calls == 1
        assert collector._usage.total_tokens == 15

    async def test_the_exception_still_reaches_the_caller(self, collector):
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        await collector.on_run_start()
        with pytest.raises(StructuredOutputValidationError):
            await collector.around_ainvoke(
                proceed=self._raiser(self._rejected()), args=(), kwargs={}
            )

"""Tests for MetricsCollector hook — M11."""

import pytest

from spectragent.core.hooks._registrar import HookRegistrar
from spectragent.core.hooks.metrics_collector import MetricsCollector


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


class _FakeResult:
    """Mimics AgentResult with mutable metrics."""

    def __init__(self):
        self.metrics = _FakeMetrics()
        self.agent_metadata = {}


class _FakeMetrics:
    def __init__(self):
        self.usage = None
        self.end_to_end_ms = 0
        self.steps = 0


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


class TestMetricsCollectorAccumulation:
    @pytest.fixture()
    def collector(self) -> MetricsCollector:
        return MetricsCollector()

    @pytest.mark.asyncio
    async def test_single_call_accumulation(self, collector):
        await collector.on_run_start()
        usage = _FakeUsage(prompt=10, completion=5, cached=2, total=15)
        await collector.after_ainvoke(
            _FakeCompletion(usage=usage), args=(), kwargs={}
        )
        assert collector._total_prompt_tokens == 10
        assert collector._total_completion_tokens == 5
        assert collector._total_cached_tokens == 2
        assert collector._total_tokens == 15
        assert collector._entry_count == 1

    @pytest.mark.asyncio
    async def test_multi_call_accumulation(self, collector):
        await collector.on_run_start()
        for _ in range(3):
            usage = _FakeUsage(prompt=10, completion=5, cached=0, total=15)
            await collector.after_ainvoke(
                _FakeCompletion(usage=usage), args=(), kwargs={}
            )
        assert collector._total_prompt_tokens == 30
        assert collector._total_completion_tokens == 15
        assert collector._total_tokens == 45
        assert collector._entry_count == 3

    @pytest.mark.asyncio
    async def test_none_usage_handled(self, collector):
        await collector.on_run_start()
        await collector.after_ainvoke(
            _FakeCompletion(usage=None), args=(), kwargs={}
        )
        assert collector._total_tokens == 0
        assert collector._entry_count == 1

    @pytest.mark.asyncio
    async def test_on_run_end_attaches_metrics(self, collector):
        await collector.on_run_start()
        usage = _FakeUsage(prompt=20, completion=10, cached=5, total=30)
        await collector.after_ainvoke(
            _FakeCompletion(usage=usage), args=(), kwargs={}
        )

        result = _FakeResult()
        await collector.on_run_end(result=result)

        assert result.metrics.usage is not None
        assert result.metrics.usage.total_prompt_tokens == 20
        assert result.metrics.usage.total_completion_tokens == 10
        assert result.metrics.usage.total_tokens == 30
        assert result.metrics.steps == 1

    @pytest.mark.asyncio
    async def test_reset_on_run_start(self, collector):
        """on_run_start resets all counters."""
        await collector.on_run_start()
        await collector.after_ainvoke(
            _FakeCompletion(usage=_FakeUsage()), args=(), kwargs={}
        )
        assert collector._entry_count == 1

        # Second run should reset
        await collector.on_run_start()
        assert collector._entry_count == 0
        assert collector._total_tokens == 0

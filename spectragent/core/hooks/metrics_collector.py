"""MetricsCollector — RuntimeHook for token usage and cost aggregation.

Intercepts llm.ainvoke via AOP to extract token counts from
ChatInvokeCompletion.usage, accumulating per-model statistics.

Registered via conscribe as hook_id="metrics".

See docs/execution_dag.md M11 for design details.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase

logger = logging.getLogger(__name__)


class MetricsCollector(HookBase):
    """Token/cost metrics aggregation hook.

    Accumulates token usage from each LLM call and attaches the
    aggregated AgentMetrics to the AgentResult at run end.

    blocking=True but actual work is just in-memory accumulation,
    overhead is negligible.
    """

    __registry_key__ = "metrics"
    __intercepts__ = [
        Intercept("ainvoke", role="llm", advice="after"),
    ]

    # ── internal state ────────────────────────────────────────
    _start_time: float = 0.0
    _step_count: int = 0
    _total_prompt_tokens: int = 0
    _total_completion_tokens: int = 0
    _total_cached_tokens: int = 0
    _total_tokens: int = 0
    _entry_count: int = 0
    _by_model: dict[str, dict[str, int]]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._by_model = {}

    @property
    def blocking(self) -> bool:
        return True

    # ── lifecycle hooks ───────────────────────────────────────

    async def on_run_start(self, **kwargs: Any) -> None:
        self._start_time = time.monotonic()
        self._step_count = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cached_tokens = 0
        self._total_tokens = 0
        self._entry_count = 0
        self._by_model = {}

    async def on_run_end(self, *, result: Any, **kwargs: Any) -> None:
        """Attach aggregated metrics to the result."""
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)

        # Build AgentUsage
        from spectragent.schemas.agent_result import AgentMetrics, AgentUsage

        usage = AgentUsage(
            total_prompt_tokens=self._total_prompt_tokens,
            total_completion_tokens=self._total_completion_tokens,
            total_prompt_cached_tokens=self._total_cached_tokens,
            total_tokens=self._total_tokens,
            entry_count=self._entry_count,
            by_model=self._by_model if self._by_model else None,
        )

        # Mutate result.metrics if it has the right shape
        if hasattr(result, "metrics") and result.metrics is not None:
            result.metrics.usage = usage
            result.metrics.end_to_end_ms = elapsed_ms
            result.metrics.steps = self._step_count
        else:
            # Fallback: attach as agent_metadata
            metrics = AgentMetrics(
                end_to_end_ms=elapsed_ms,
                steps=self._step_count,
                usage=usage,
            )
            if hasattr(result, "agent_metadata") and isinstance(
                result.agent_metadata, dict
            ):
                result.agent_metadata["metrics"] = metrics.model_dump()

        logger.info(
            "MetricsCollector: %d calls, %d tokens (prompt=%d, completion=%d, cached=%d)",
            self._entry_count,
            self._total_tokens,
            self._total_prompt_tokens,
            self._total_completion_tokens,
            self._total_cached_tokens,
        )

    # ── AOP intercept handler ─────────────────────────────────

    async def after_ainvoke(
        self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any
    ) -> None:
        """Extract token usage from ChatInvokeCompletion."""
        self._entry_count += 1
        self._step_count += 1

        usage = getattr(result, "usage", None)
        if usage is None:
            return

        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        cached = getattr(usage, "prompt_cached_tokens", 0) or 0
        total = getattr(usage, "total_tokens", 0) or 0

        self._total_prompt_tokens += prompt
        self._total_completion_tokens += completion
        self._total_cached_tokens += cached
        self._total_tokens += total

        # Per-model accumulation
        model = _extract_model_name(args, kwargs)
        if model:
            if model not in self._by_model:
                self._by_model[model] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "total_tokens": 0,
                    "calls": 0,
                }
            entry = self._by_model[model]
            entry["prompt_tokens"] += prompt
            entry["completion_tokens"] += completion
            entry["cached_tokens"] += cached
            entry["total_tokens"] += total
            entry["calls"] += 1


def _extract_model_name(args: tuple, kwargs: dict) -> str:
    """Try to extract model name from the LLM call context.

    The LLM object is typically `self` (first element of the original
    method's bound args), accessible via the proxy's target.
    """
    # kwargs might contain model info in some scenarios
    if "model" in kwargs:
        return str(kwargs["model"])
    return ""

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
    """Token/cost metrics **enricher**.

    Accumulates token usage per LLM call and fills in what the run's own
    measurements are missing. Deliberately *not* an authority: the Loop
    counts its own steps and its own tokens, and this hook must never
    overwrite them. It used to assign ``result.metrics.steps =
    <llm call count>``, silently replacing "steps the agent took" with a
    different quantity that only coincides for strictly 1-call-per-step
    loops.

    blocking=True but actual work is just in-memory accumulation,
    overhead is negligible.
    """

    __registry_key__ = "metrics"
    __intercepts__ = [
        Intercept("ainvoke", role="llm", advice="after"),
    ]

    # Enrich before anything serializes the result.
    priority = HookBase.PRIORITY_ENRICH

    # ── internal state ────────────────────────────────────────
    _start_time: float = 0.0
    _llm_calls: int = 0
    _usage: Any = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        from spectragent.schemas.agent_result import AgentUsage

        self._usage = AgentUsage()

    @property
    def blocking(self) -> bool:
        return True

    @property
    def llm_calls(self) -> int:
        return self._llm_calls

    # ── lifecycle hooks ───────────────────────────────────────

    async def on_run_start(self, **kwargs: Any) -> None:
        from spectragent.schemas.agent_result import AgentUsage

        self._start_time = time.monotonic()
        self._llm_calls = 0
        self._usage = AgentUsage()

    async def on_run_end(self, *, result: Any = None, env: Any = None, **kwargs: Any) -> None:
        """Fill the gaps in the result's metrics; overwrite nothing."""
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)

        from spectragent.schemas.agent_result import AgentMetrics

        metrics = getattr(result, "metrics", None)
        if metrics is None:
            # Fallback: the result has no metrics slot at all.
            fallback = AgentMetrics(
                end_to_end_ms=elapsed_ms,
                steps=0,
                llm_calls=self._llm_calls,
                usage=self._usage if self._llm_calls else None,
            )
            if hasattr(result, "agent_metadata") and isinstance(
                result.agent_metadata, dict
            ):
                result.agent_metadata["metrics"] = fallback.model_dump()
        else:
            if metrics.usage is None:
                # Nothing measured the run — adopt what we counted.
                metrics.usage = self._usage if self._llm_calls else None
            else:
                _merge_missing(metrics.usage, self._usage)
            if metrics.llm_calls is None:
                metrics.llm_calls = self._llm_calls
            if not metrics.end_to_end_ms:
                metrics.end_to_end_ms = elapsed_ms

        logger.info(
            "MetricsCollector: %d calls, %d tokens (prompt=%d, completion=%d, cached=%d)",
            self._llm_calls,
            self._usage.total_tokens,
            self._usage.total_prompt_tokens,
            self._usage.total_completion_tokens,
            self._usage.total_prompt_cached_tokens,
        )

    # ── AOP intercept handler ─────────────────────────────────

    async def after_ainvoke(
        self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any
    ) -> None:
        """Extract token usage from ChatInvokeCompletion."""
        self._llm_calls += 1
        self._usage.add_llm_usage(
            getattr(result, "usage", None),
            model=_extract_model_name(kwargs, ctx),
        )


def _merge_missing(target: Any, collected: Any) -> None:
    """Fill only the fields the authoritative usage left unmeasured."""
    if target.by_model is None and collected.by_model:
        target.by_model = collected.by_model
    if target.total_tokens == 0 and collected.total_tokens:
        target.total_prompt_tokens = collected.total_prompt_tokens
        target.total_completion_tokens = collected.total_completion_tokens
        target.total_prompt_cached_tokens = collected.total_prompt_cached_tokens
        target.total_tokens = collected.total_tokens
    if target.entry_count == 0:
        target.entry_count = collected.entry_count


def _extract_model_name(kwargs: dict, ctx: dict) -> str:
    """Resolve which model produced this completion.

    The name lives on the LLM instance, not in the call's kwargs — which
    is why ``AgentUsage.by_model`` was permanently ``None`` until the AOP
    layer started passing ``target`` to handlers. An explicit ``model=``
    kwarg still wins when a caller passes one.
    """
    if kwargs.get("model"):
        return str(kwargs["model"])
    target = ctx.get("target")
    for attr in ("model", "model_name", "name"):
        value = getattr(target, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""

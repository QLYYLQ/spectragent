"""TraceView — the read-side vocabulary every evaluator shares.

``RunTrace`` is a faithful, low-level reader: manifests, step files,
observation blobs. Evaluators do not want that. They want four things —
what was asked, what the agent answered, what it did, and what it saw —
and every grader that was ported into this package reimplemented those
four with slightly different fallbacks. Centralising them here means a
change to the on-disk step format updates one file, not four graders.

Everything is derived from files on disk. Nothing here touches a live
environment, an agent instance, or a network.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator.errors import TraceIncompleteError
from spectragent.schemas._types import UTCDatetime
from spectragent.schemas.agent_result import AgentMetrics, AgentUsage
from spectragent.schemas.eval_result import AgentResultRef


class TraceView:
    """Evaluator-facing projection of one persisted run."""

    def __init__(self, trace: RunTrace) -> None:
        self._trace = trace

    @property
    def trace(self) -> RunTrace:
        return self._trace

    @property
    def trace_dir(self) -> Path:
        return self._trace.trace_dir

    @property
    def run_id(self) -> str:
        return self._trace.run_id

    # ── Identity ────────────────────────────────────────────────────

    @cached_property
    def _result(self) -> dict[str, Any]:
        result = self._trace.result
        if result is None:
            raise TraceIncompleteError(
                f"{self._trace.trace_dir} has no result.json. The run never "
                f"reached a terminal state through AgentRuntime, so there is "
                f"nothing to score. Check the trace's manifest status."
            )
        return result

    @property
    def has_result(self) -> bool:
        return self._trace.result is not None

    @cached_property
    def task_id(self) -> str:
        """Dataset identity, from the config snapshot then the result.

        Without this an evaluator cannot join the trace back to its
        ``EvalReference`` at all — which is why ``TraceConfig`` carries
        ``(benchmark, subset, task_id)``.
        """
        config_id = self._trace.config.task_id
        if config_id:
            return config_id
        result_id = str(self._result.get("task_id") or "")
        if result_id:
            return result_id
        raise TraceIncompleteError(
            f"{self._trace.trace_dir} records no task_id in config.json or "
            f"result.json. It cannot be matched to any benchmark reference."
        )

    @cached_property
    def benchmark(self) -> str:
        return self._trace.config.benchmark

    @cached_property
    def subset(self) -> str:
        return self._trace.config.subset

    @cached_property
    def task_text(self) -> str:
        """The instruction the agent was given."""
        return self._trace.config.task or str(self._result.get("task") or "")

    # ── Outcome ─────────────────────────────────────────────────────

    @cached_property
    def answer(self) -> str:
        """The agent's final output.

        Valid on every termination path — a run that produced an answer
        and then hit ``max_steps`` must not lose it.
        """
        return str(self._result.get("answer") or "")

    @cached_property
    def agent_done(self) -> str:
        return str(self._result.get("agent_done") or "")

    @cached_property
    def error(self) -> str | None:
        error = self._result.get("error")
        return str(error) if error else None

    @cached_property
    def model_id(self) -> str:
        return str(self._result.get("model_id") or self._trace.config.llm_model or "")

    @cached_property
    def browser_id(self) -> str:
        return str(self._result.get("browser_id") or "")

    @cached_property
    def timestamp(self) -> UTCDatetime | None:
        raw = self._result.get("timestamp")
        if not raw:
            return None
        return _coerce_datetime(raw)

    # ── Trajectory ──────────────────────────────────────────────────

    @cached_property
    def action_history(self) -> list[str]:
        """Flat ``name(params)`` strings, one per executed action.

        Rebuilt from the WAL step records when they carry structured
        ``actions``, because those are written per step and survive a
        crash; ``result.json``'s pre-flattened list only exists if the run
        finished. Falls back to it when the steps are empty.
        """
        history: list[str] = []
        for record in self._trace.step_records:
            for action in record.get("actions") or []:
                if isinstance(action, dict):
                    name = action.get("name", "")
                    params = action.get("params", {})
                    history.append(f"{name}({params})")
                else:
                    history.append(str(action))
        if history:
            return history
        return [str(item) for item in (self._result.get("action_history") or [])]

    @cached_property
    def screenshots(self) -> list[Path]:
        """On-disk PNG paths, in step order.

        Real files, not base64 buried in JSON: ``ScreenshotObs`` persists
        ``image_bytes`` through ``trace_binary()`` as ``NNN_screenshot.png``
        with a metadata sidecar. An image judge that finds this list empty
        must say so rather than silently switching to a text-only prompt.
        """
        return list(self._trace.screenshots)

    @cached_property
    def step_count(self) -> int:
        metrics = self._result.get("metrics")
        if isinstance(metrics, dict) and metrics.get("steps") is not None:
            return int(metrics["steps"])
        return self._trace.step_count

    # ── Metrics ─────────────────────────────────────────────────────

    @cached_property
    def metrics(self) -> AgentMetrics | None:
        """Agent-side metrics, nested rather than flat-merged.

        Flat-merging these into the eval details is what used to let the
        agent's ``usage`` silently overwrite the evaluator's.
        """
        raw = self._result.get("metrics")
        if not isinstance(raw, dict):
            return None
        usage_raw = raw.get("usage")
        usage = (
            AgentUsage(**usage_raw)
            if isinstance(usage_raw, dict) and usage_raw
            else None
        )
        return AgentMetrics(
            ttft_ms=raw.get("ttft_ms"),
            end_to_end_ms=int(raw.get("end_to_end_ms") or 0),
            steps=int(raw.get("steps") or 0),
            llm_calls=raw.get("llm_calls"),
            usage=usage,
        )

    @cached_property
    def end_to_end_ms(self) -> int:
        metrics = self.metrics
        return metrics.end_to_end_ms if metrics else 0

    def result_ref(self) -> AgentResultRef:
        """Back-pointer from a verdict to the run that produced it."""
        return AgentResultRef(
            task_id=self.task_id,
            timestamp=self.timestamp or _utc_now(),
            result_dir=str(self.trace_dir),
            model_id=self.model_id,
            browser_id=self.browser_id,
        )


def _utc_now() -> UTCDatetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _coerce_datetime(raw: Any) -> UTCDatetime | None:
    from datetime import UTC, datetime

    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

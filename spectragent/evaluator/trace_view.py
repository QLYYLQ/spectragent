"""TraceView — the read-side vocabulary every evaluator shares.

``RunTrace`` is a faithful, low-level reader: a manifest, a config
snapshot, an ``AgentResult``, a list of ``StepRecord``. Evaluators do not
want that shape. They want four things — what was asked, what the agent
answered, what it did, and what it saw — each of which is spread across
two or three of those files, and every grader that was ported into this
package reimplemented the joins with slightly different fallbacks.
Centralising them here means a change to the on-disk step format updates
one file, not four graders.

Everything is derived from files on disk. Nothing here touches a live
environment, an agent instance, or a network.

A trace's steps have three origins (``StepRecord.origin``,
``docs/architecture_design.md`` §4.11): ``policy`` steps are what this run's
agent decided, ``restored`` steps are a branch run's re-execution of its
source's prefix, and a ``terminal`` step is the look at the world after the
run ended. The trajectory accessors keep them apart.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator.errors import TraceIncompleteError
from spectragent.schemas._types import UTCDatetime
from spectragent.schemas.agent_result import AgentMetrics, AgentResult
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
    def _result(self) -> AgentResult:
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
        if self._result.task_id:
            return self._result.task_id
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
        return self._trace.config.task or self._result.task

    # ── Outcome ─────────────────────────────────────────────────────

    @cached_property
    def answer(self) -> str:
        """The agent's final output.

        Valid on every termination path — a run that produced an answer
        and then hit ``max_steps`` must not lose it.
        """
        return self._result.answer

    @cached_property
    def agent_done(self) -> str:
        return self._result.agent_done.value

    @cached_property
    def error(self) -> str | None:
        return self._result.error or None

    @cached_property
    def model_id(self) -> str:
        return self._result.model_id or self._trace.config.llm_model

    @cached_property
    def browser_id(self) -> str:
        return self._result.browser_id

    @cached_property
    def timestamp(self) -> UTCDatetime:
        """When the run ended. Required on ``AgentResult``, so never absent."""
        return self._result.timestamp

    # ── Trajectory ──────────────────────────────────────────────────

    @cached_property
    def action_history(self) -> list[str]:
        """Flat ``name(params)`` strings of the run's own agent, one per action.

        Policy steps only: a branch run's restored prefix replays its source
        run's actions, which this run's agent did not decide
        (:attr:`restored_action_history` lists them apart). Rebuilt from the
        WAL step records when they carry structured ``actions``, because
        those are written per step and survive a crash; ``result.json``'s
        pre-flattened list only exists if the run finished. Falls back to it
        when the policy steps carry none.
        """
        history: list[str] = []
        for record in self._trace.step_records:
            if record.origin == "policy":
                history.extend(f"{a.name}({a.params})" for a in record.actions)
        if history:
            return history
        return list(self._result.action_history)

    @cached_property
    def restored_action_history(self) -> list[str]:
        """A branch run's restored prefix, each action labelled ``[restored]``.

        The actions actually executed after retargeting during this run, in
        step order. Empty for every trace that is not a branch, so a judge
        that prepends it to :attr:`action_history` sends exactly what it
        sent before for those.
        """
        history: list[str] = []
        for record in self._trace.step_records:
            if record.origin != "restored":
                continue
            if record.restoration is None:
                history.extend(f"[restored] {a.name}({a.params})" for a in record.actions)
                continue
            for attempt in record.restoration.actions:
                action = attempt.executed
                if action is None:
                    continue
                text = f"[restored] {action.name}({action.params})"
                if attempt.live_error is not None:
                    text += f" [raised: {attempt.live_error}]"
                elif attempt.result is not None and not attempt.result.success:
                    text += f" [failed: {attempt.result.error or 'unsuccessful result'}]"
                history.append(text)
        return history

    @cached_property
    def screenshots(self) -> list[Path]:
        """On-disk screenshot paths of the committed steps, in step order.

        Real files, not base64 buried in JSON: ``ScreenshotObs`` persists
        ``image_bytes`` through ``trace_binary()`` as
        ``NNN_browser.screenshot.png`` (``.jpg`` for a JPEG run — the suffix
        comes from the image header) — named by the fully-qualified fragment
        key, so the path says which capability took the frame. Resolved
        through the manifest's fragment index, so a frame written for a step
        that never committed is not one of them. An image judge that finds
        this list empty must say so rather than silently switching to a
        text-only prompt.

        Every origin is included: a branch run's restored steps and a
        terminal observation (``ReactLoop.terminal_observation``) — whose
        frame is the state after the last action — are frames of this run.
        """
        return list(self._trace.screenshots)

    @cached_property
    def step_count(self) -> int:
        """Policy steps, as ``AgentMetrics.steps`` counts them."""
        return self._result.metrics.steps

    # ── Metrics ─────────────────────────────────────────────────────

    @cached_property
    def metrics(self) -> AgentMetrics:
        """Agent-side metrics, nested rather than flat-merged.

        Flat-merging these into the eval details is what used to let the
        agent's ``usage`` silently overwrite the evaluator's.

        A deep copy, because ``EvaluatorBase.build_result`` hands this
        straight to a mutable ``EvalDetails``: an evaluator that adds to
        ``metrics.usage`` would otherwise be editing the trace's own
        cached result, and every later reader of the same ``RunTrace``
        would see the doctored numbers.
        """
        return self._result.metrics.model_copy(deep=True)

    @cached_property
    def execution_ms(self) -> int:
        return self.metrics.execution_ms

    def result_ref(self) -> AgentResultRef:
        """Back-pointer from a verdict to the run that produced it."""
        return AgentResultRef(
            task_id=self.task_id,
            timestamp=self.timestamp,
            result_dir=str(self.trace_dir),
            model_id=self.model_id,
            browser_id=self.browser_id,
        )

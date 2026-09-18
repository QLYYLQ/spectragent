"""EvaluationRunner — score a directory of traces, offline.

This is the "Evaluate Many" half of the record-once contract, and it is
deliberately a separate driver from :class:`BenchmarkRunner`. The runner
that produces traces needs a browser, API keys and hours; this one needs
a directory. You can point it at traces recorded last month, on another
machine, by a stack that no longer exists — which is the only reason the
WAL is worth writing.

Each verdict lands in the trace's own ``evals/`` subdirectory, which the
WAL has always created and never used. Keeping the verdict next to the
evidence means a trace directory is self-contained: ``ls traces/foo/``
tells you what ran, what it did, and how it scored.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator.base import BaseEvaluator
from spectragent.evaluator.summary import summarize
from spectragent.evaluator.trace_view import TraceView
from spectragent.schemas.eval_result import (
    AgentResultRef,
    EvalDetails,
    EvalResult,
)
from spectragent.schemas.eval_summary import EvalSummary

logger = logging.getLogger(__name__)


def discover_traces(base_dir: Path) -> list[Path]:
    """Every committed trace directory under *base_dir*.

    A directory without ``manifest.json`` is not a trace — it is a
    half-written one, or something else entirely — and is skipped rather
    than crashing the sweep.
    """
    if not base_dir.is_dir():
        return []
    return sorted(
        child
        for child in base_dir.iterdir()
        if child.is_dir() and (child / "manifest.json").is_file()
    )


@dataclass(frozen=True)
class EvaluationOutcome:
    """What happened to one trace."""

    task_id: str
    trace_dir: Path | None = None
    result: EvalResult | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.error is None


@dataclass
class EvaluationReport:
    """Verdicts for one sweep, plus the ones that could not be produced."""

    evaluator_id: str
    benchmark: str = ""
    subset: str = ""
    outcomes: list[EvaluationOutcome] = field(default_factory=list)
    total_tasks: int = 0

    @property
    def results(self) -> list[EvalResult]:
        return [o.result for o in self.outcomes if o.result is not None]

    @property
    def graded(self) -> list[EvalResult]:
        """Only the verdicts a grader actually produced.

        :attr:`results` also contains the coverage-policy placeholders, so
        anything asking "what did the judge decide?" — as opposed to "what
        does the benchmark score?" — wants this list.
        """
        return [r for r in self.results if r.verdict_source == "graded"]

    @property
    def errors(self) -> list[EvaluationOutcome]:
        return [o for o in self.outcomes if o.error is not None]

    def summary(self) -> EvalSummary:
        return summarize(
            self.results,
            total_tasks=self.total_tasks or len(self.results),
            evaluation_config={
                "evaluator_id": self.evaluator_id,
                "benchmark": self.benchmark,
                "subset": self.subset,
                "unevaluable_traces": len(self.errors),
            },
        )


class EvaluationRunner:
    """Drive one evaluator across many traces.

    Args:
        evaluator: Any object satisfying :class:`BaseEvaluator`.
        eval_loader: Source of ``EvalReference`` rows, keyed by task id —
            i.e. any registered eval data loader.
        evals_dir: Optional directory for a flat copy of every verdict
            plus ``summary.json``. Verdicts are *always* written into
            each trace's own ``evals/`` regardless.
        write_into_trace: Turn off to evaluate read-only.
    """

    def __init__(
        self,
        *,
        evaluator: BaseEvaluator,
        eval_loader: Any,
        evals_dir: Path | None = None,
        write_into_trace: bool = True,
    ) -> None:
        self._evaluator = evaluator
        self._loader = eval_loader
        self._evals_dir = evals_dir
        self._write_into_trace = write_into_trace

    @property
    def evaluator_id(self) -> str:
        return self._evaluator.evaluator_id

    @property
    def verdict_slug(self) -> str:
        """Filename stem for this evaluator's verdicts.

        ``evaluator_id`` alone is not a filename: re-scoring a trace with
        the same evaluator under a different threshold, normalization or
        judge model would land on the same path and silently destroy the
        earlier verdict. :class:`EvaluatorBase` derives a config digest;
        an evaluator that only satisfies the Protocol has no such notion,
        and falls back to its bare id — it simply does not get the
        disambiguation.
        """
        slug = getattr(self._evaluator, "verdict_slug", None)
        return str(slug) if slug else self.evaluator_id

    async def evaluate_trace(self, trace_dir: Path) -> EvalResult:
        """Score one trace. Raises on anything that makes it unscorable."""
        trace = RunTrace(trace_dir)
        task_id = TraceView(trace).task_id
        reference = self._loader.get(task_id)
        if reference is None:
            raise KeyError(
                f"Trace {trace_dir} ran task_id {task_id!r}, which is not in "
                f"eval loader {getattr(self._loader, 'benchmark_name', '?')!r} "
                f"(subset {getattr(self._loader, 'subset', '?')!r}). The trace "
                f"and the reference set disagree about which split ran."
            )
        result = await self._evaluator.evaluate(trace, reference)
        self._persist(trace_dir, result)
        return result

    async def run(
        self,
        trace_dirs: Sequence[Path],
        *,
        expected_task_ids: Iterable[str] | None = None,
        benchmark: str = "",
        subset: str = "",
    ) -> EvaluationReport:
        """Score every trace, then apply the coverage policy.

        *expected_task_ids* is the split that was supposed to run. Any
        task in it with no verdict is recorded as a **failure**, not
        omitted — a task the agent crashed on is a task the agent did not
        complete, and dropping it from the denominator inflates the score
        of exactly the runs that went worst.

        That argument is about the *agent* failing, and it does not carry
        over to the *evaluator* failing. A judge that returned 429 says
        nothing about the agent's answer, so a trace this runner could not
        score is recorded with ``verdict_source="evaluation_error"`` and
        kept out of ``evaluated_tasks``: the placeholder still occupies
        the denominator (the run is not finished until it is scored), but
        it never masquerades as a graded wrong answer. Re-run the scoring
        pass and it turns into a real verdict — the trace is untouched.
        """
        outcomes: list[EvaluationOutcome] = []
        failures: dict[str, str] = {}
        for trace_dir in trace_dirs:
            try:
                result = await self.evaluate_trace(trace_dir)
                outcomes.append(
                    EvaluationOutcome(task_id=result.task_id, trace_dir=trace_dir, result=result)
                )
            except Exception as exc:
                logger.exception("Evaluation failed for %s", trace_dir)
                task_id = _task_id_or_dirname(trace_dir)
                message = f"{type(exc).__name__}: {exc}"
                failures[task_id] = message
                outcomes.append(
                    EvaluationOutcome(task_id=task_id, trace_dir=trace_dir, error=message)
                )

        # Which tasks owe a verdict and have none. With a declared split
        # that is the split; without one it is the traces we were handed,
        # so an unscorable trace cannot sit in the denominator as a task
        # with no record at all.
        expected = list(expected_task_ids or [])
        scored = {o.task_id for o in outcomes if o.result is not None}
        owed = (
            [task_id for task_id in expected if task_id not in scored]
            if expected
            else [o.task_id for o in outcomes if o.result is None]
        )
        for task_id in owed:
            outcomes.append(
                EvaluationOutcome(
                    task_id=task_id,
                    result=self._unscored(task_id, benchmark, subset, reason=failures.get(task_id)),
                )
            )

        report = EvaluationReport(
            evaluator_id=self.evaluator_id,
            benchmark=benchmark,
            subset=subset,
            outcomes=outcomes,
            total_tasks=len(expected),
        )
        self._persist_summary(report)
        return report

    # ── internals ───────────────────────────────────────────────────

    def _unscored(
        self, task_id: str, benchmark: str, subset: str, *, reason: str | None
    ) -> EvalResult:
        """The coverage-policy record for a task with no verdict.

        *reason* is the evaluator-side failure, when there was one. It is
        the whole distinction: with a reason, scoring broke and the
        ``predicted_label=0`` below is a placeholder; without one, no
        trace ever arrived and the 0 is a statement about the agent.
        """
        from datetime import UTC, datetime

        source = "evaluation_error" if reason else "not_evaluated"
        details: dict[str, Any] = {
            "is_synthetic_failure": True,
            "missing_reason": source,
        }
        if reason:
            details["evaluation_error"] = reason
        now = datetime.now(UTC)
        return EvalResult(
            task_id=task_id,
            evaluator_id=self.evaluator_id,
            evaluator_config=_config_signature(self._evaluator),
            benchmark=benchmark,
            subset=subset,
            timestamp=now,
            agent_result_ref=AgentResultRef(task_id=task_id, timestamp=now, result_dir=""),
            predicted_label=0,
            verdict_source=source,
            failure_category=source,
            evaluation_details=EvalDetails(
                reasoning=(
                    f"Scoring this trace failed ({reason}). No grader ever "
                    f"judged the answer, so this label is a placeholder held "
                    f"by the coverage policy, not a measurement — re-run the "
                    f"evaluation pass before quoting the score."
                    if reason
                    else "No trace produced a verdict for this task; counted "
                    "as failed by coverage policy."
                ),
                benchmark_details=details,
            ),
        )

    def _persist(self, trace_dir: Path, result: EvalResult) -> None:
        payload = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)
        slug = self.verdict_slug
        if self._write_into_trace:
            evals_dir = trace_dir / "evals"
            evals_dir.mkdir(parents=True, exist_ok=True)
            (evals_dir / f"{slug}.json").write_text(payload, "utf-8")
        if self._evals_dir is not None:
            self._evals_dir.mkdir(parents=True, exist_ok=True)
            name = f"{result.run_id or result.task_id}__{slug}.json"
            (self._evals_dir / _safe_name(name)).write_text(payload, "utf-8")

    def _persist_summary(self, report: EvaluationReport) -> None:
        if self._evals_dir is None:
            return
        self._evals_dir.mkdir(parents=True, exist_ok=True)
        (self._evals_dir / f"{self.verdict_slug}_summary.json").write_text(
            json.dumps(report.summary().model_dump(mode="json"), ensure_ascii=False, indent=2),
            "utf-8",
        )


def _task_id_or_dirname(trace_dir: Path) -> str:
    try:
        return TraceView(RunTrace(trace_dir)).task_id
    except Exception:
        return trace_dir.name


def _config_signature(evaluator: BaseEvaluator) -> dict[str, Any]:
    """The evaluator's settings, when it is one that can report them.

    A synthetic record should name the configuration that failed to score
    the trace, for the same reason a real verdict does. Protocol-only
    evaluators have no signature to give.
    """
    reader = getattr(evaluator, "config_signature", None)
    if not callable(reader):
        return {}
    try:
        signature = reader()
    except Exception:  # pragma: no cover — a broken evaluator, not our verdict
        return {}
    return signature if isinstance(signature, dict) else {}


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in name)

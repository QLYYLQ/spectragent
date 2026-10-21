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

from spectragent.core.trace.fsio import atomic_write
from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator.base import BaseEvaluator, EvaluatorLifecycle, needs_reference
from spectragent.evaluator.errors import NoReferenceError
from spectragent.evaluator.summary import summarize
from spectragent.evaluator.trace_view import TraceView
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.eval_result import (
    AgentResultRef,
    EvalDetails,
    EvalResult,
)
from spectragent.schemas.eval_summary import EvalSummary
from spectragent.schemas.restoration import BranchProvenance, BranchTaskVariant

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
    details: EvalDetails | None = None

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
            plus ``summary.json``. Writing into each trace's own
            ``evals/`` is controlled separately by ``write_into_trace``.
        write_into_trace: Turn off to evaluate read-only.
    """

    def __init__(
        self,
        *,
        evaluator: BaseEvaluator,
        eval_loader: Any,
        evals_dir: Path | None = None,
        write_into_trace: bool = True,
        verify_reference_identity: bool = False,
    ) -> None:
        self._evaluator = evaluator
        self._loader = eval_loader
        self._evals_dir = evals_dir
        self._write_into_trace = write_into_trace
        self._verify_reference_identity = verify_reference_identity

    @property
    def evaluator_id(self) -> str:
        return self._evaluator.evaluator_id

    @property
    def verdict_slug(self) -> str:
        """Filename stem for this evaluator's verdicts.

        ``evaluator_id`` alone is not a filename: re-scoring a trace with
        the same evaluator under a different threshold, normalization or
        judge model would land on the same path and silently destroy the
        earlier verdict. Every evaluator declares its own stable identity.
        """
        slug = self._evaluator.verdict_slug
        if not isinstance(slug, str) or not slug:
            raise ValueError(f"{self.evaluator_id} must provide a nonempty verdict_slug")
        return slug

    async def evaluate_trace(self, trace_dir: Path) -> EvalResult:
        """Score one trace as one independent judge execution."""
        try:
            return await self._evaluate_trace(trace_dir)
        finally:
            await self.aclose()

    async def aclose(self, *, timeout: float = 5.0) -> None:
        """Release the resource-owning evaluator through its public contract."""
        if isinstance(self._evaluator, EvaluatorLifecycle):
            # Judge evaluators delegate to close_models, which isolates cleanup
            # diagnostics from verdicts and preserves external cancellation.
            try:
                await self._evaluator.aclose(timeout=timeout)
            except Exception:
                logger.exception("Evaluator cleanup failed for %s", self.evaluator_id)

    async def _evaluate_trace(self, trace_dir: Path) -> EvalResult:
        """Score one trace. Raises on anything that makes it unscorable."""
        trace = RunTrace(trace_dir)
        view = TraceView(trace)
        branch = trace.config.branch
        if branch is not None and branch.task_variant is not None:
            return await self._evaluate_task_variant(trace_dir, trace, view, branch)
        task_id = branch.source_task_id if branch is not None else view.task_id
        if branch is not None:
            for field, expected in (
                ("benchmark_name", branch.source_benchmark),
                ("subset", branch.source_subset),
            ):
                actual = getattr(self._loader, field, None)
                if not expected or actual != expected:
                    raise ValueError(
                        f"Branch reference {field}={expected!r} does not match "
                        f"eval loader {actual!r}"
                    )
        elif self._verify_reference_identity:
            for field, expected in (("benchmark_name", view.benchmark), ("subset", view.subset)):
                actual = getattr(self._loader, field, None)
                if not expected or actual != expected:
                    raise ValueError(
                        f"Trace reference {field}={expected!r} does not match eval loader {actual!r}"
                    )
        reference = self._loader.get(task_id)
        if reference is None:
            raise KeyError(
                f"Trace {trace_dir} ran task_id {task_id!r}, which is not in "
                f"eval loader {getattr(self._loader, 'benchmark_name', '?')!r} "
                f"(subset {getattr(self._loader, 'subset', '?')!r}). The trace "
                f"and the reference set disagree about which split ran."
            )
        result = await self._evaluator.evaluate(trace, reference)
        if branch is not None:
            result.evaluation_details.benchmark_details["source_reference"] = {
                "benchmark": branch.source_benchmark,
                "subset": branch.source_subset,
                "task_id": branch.source_task_id,
            }
        self._persist(trace_dir, result)
        return result

    async def _evaluate_task_variant(
        self, trace_dir: Path, trace: RunTrace, view: TraceView, branch: BranchProvenance
    ) -> EvalResult:
        """Score a branch that ran a task variant: a task no benchmark reference describes.

        Its source task's reference belongs to another task, so it is never
        looked up. An evaluator that grades against a reference cannot score
        the trace (:class:`NoReferenceError`, an evaluation error); one that
        declares ``__needs_reference__ = False`` judges the recorded task —
        the variant's — and receives a reference naming the run's own task
        with no evaluation data.
        """
        variant = branch.task_variant
        assert variant is not None
        if needs_reference(self._evaluator):
            source = "/".join(
                part or "-"
                for part in (branch.source_benchmark, branch.source_subset, branch.source_task_id)
            )
            raise NoReferenceError(
                f"task variant {variant.variant_id!r}: no reference for this task. The branch "
                f"ran a variant of source task {source}, whose reference describes the "
                f"source's task, not this one; {self.evaluator_id!r} grades against a "
                f"benchmark reference, so this trace is not evaluated by it"
            )
        reference = EvalReference(task_id=view.task_id, benchmark=view.benchmark)
        result = await self._evaluator.evaluate(trace, reference)
        result.evaluation_details.benchmark_details["task_variant"] = _variant_record(
            variant, branch
        )
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
        try:
            outcomes: list[EvaluationOutcome] = []
            failures: dict[str, str] = {}
            failure_details: dict[str, EvalDetails] = {}
            for trace_dir in trace_dirs:
                try:
                    result = await self._evaluate_trace(trace_dir)
                    outcomes.append(
                        EvaluationOutcome(
                            task_id=result.task_id, trace_dir=trace_dir, result=result
                        )
                    )
                except Exception as exc:
                    logger.exception("Evaluation failed for %s", trace_dir)
                    task_id = _task_id_or_dirname(trace_dir)
                    message = f"{type(exc).__name__}: {exc}"
                    failures[task_id] = message
                    details = getattr(exc, "details", None)
                    if isinstance(details, EvalDetails):
                        failure_details[task_id] = details
                    else:
                        details = None
                    outcomes.append(
                        EvaluationOutcome(
                            task_id=task_id, trace_dir=trace_dir, error=message, details=details
                        )
                    )

            # Which tasks owe a verdict and have none. With a declared split
            # that is the split; without one it is the traces we were handed,
            # so an unscorable trace cannot sit in the denominator as a task
            # with no record at all.
            expected = list(expected_task_ids or [])
            scored = {o.task_id for o in outcomes if o.result is not None}
            owed = (
                [
                    (task_id, failures.get(task_id), failure_details.get(task_id))
                    for task_id in expected
                    if task_id not in scored
                ]
                if expected
                else [(o.task_id, o.error, o.details) for o in outcomes if o.result is None]
            )
            for task_id, reason, evidence in owed:
                outcomes.append(
                    EvaluationOutcome(
                        task_id=task_id,
                        result=self._unscored(
                            task_id,
                            benchmark,
                            subset,
                            reason=reason,
                            evidence=evidence,
                        ),
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

        finally:
            await self.aclose()

    # ── internals ───────────────────────────────────────────────────

    def _unscored(
        self,
        task_id: str,
        benchmark: str,
        subset: str,
        *,
        reason: str | None,
        evidence: EvalDetails | None = None,
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
            evaluator_config=self._evaluator.config_signature(),
            benchmark=benchmark,
            subset=subset,
            timestamp=now,
            agent_result_ref=AgentResultRef(task_id=task_id, timestamp=now, result_dir=""),
            predicted_label=0,
            verdict_source=source,
            failure_category=source,
            evaluation_details=EvalDetails(
                **(
                    evidence.model_dump(exclude={"reasoning", "benchmark_details"})
                    if evidence is not None
                    else {}
                ),
                reasoning=(
                    f"Scoring this trace failed ({reason}). No grader ever "
                    f"judged the answer, so this label is a placeholder held "
                    f"by the coverage policy, not a measurement — re-run the "
                    f"evaluation pass before quoting the score."
                    if reason
                    else "No trace produced a verdict for this task; counted "
                    "as failed by coverage policy."
                ),
                benchmark_details={
                    **(evidence.benchmark_details if evidence is not None else {}),
                    **details,
                },
            ),
        )

    def _persist(self, trace_dir: Path, result: EvalResult) -> None:
        """Write the verdict next to the evidence, and to the flat copy if asked.

        Through :func:`~spectragent.core.trace.fsio.atomic_write`, like every
        other file in a trace: a verdict torn by a crash or a full disk would
        be read back by the next pass as a corrupt evaluation rather than a
        missing one, and a re-score that failed half way through used to
        truncate the verdict it was replacing. Either the old file or the new
        one is on disk, never part of one.
        """
        payload = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)
        data = payload.encode("utf-8")
        slug = self.verdict_slug
        if self._write_into_trace:
            atomic_write(trace_dir / "evals" / f"{slug}.json", data)
        if self._evals_dir is not None:
            name = f"{result.run_id or result.task_id}__{slug}.json"
            atomic_write(self._evals_dir / _safe_name(name), data)

    def _persist_summary(self, report: EvaluationReport) -> None:
        if self._evals_dir is None:
            return
        summary = json.dumps(report.summary().model_dump(mode="json"), ensure_ascii=False, indent=2)
        atomic_write(self._evals_dir / f"{self.verdict_slug}_summary.json", summary.encode("utf-8"))


def _variant_record(variant: BranchTaskVariant, branch: BranchProvenance) -> dict[str, Any]:
    """What a verdict on a task-variant branch says about its task: the variant, no reference."""
    return {
        **variant.model_dump(mode="json"),
        "source_task": {
            "benchmark": branch.source_benchmark,
            "subset": branch.source_subset,
            "task_id": branch.source_task_id,
        },
        "reference": None,
    }


def _task_id_or_dirname(trace_dir: Path) -> str:
    try:
        return TraceView(RunTrace(trace_dir)).task_id
    except Exception:
        return trace_dir.name


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in name)

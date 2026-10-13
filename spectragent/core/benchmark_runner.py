"""BenchmarkRunner — drive a whole benchmark, one task at a time.

This is the piece that turns "an agent ran once" into "a benchmark was
evaluated". Before it existed, ``AgentRuntime.execute`` took a free-text
task string and nothing in ``spectragent/`` ever called it: the only
drivers were demo scripts that hardcoded a task, never touched a
``TaskLoader``, and therefore produced traces with no dataset identity.
A trace you cannot join back to its ``EvalReference`` cannot be scored by
anything, which is what made "Record Once, Evaluate Many" false in
practice.

The runner owns exactly four things:

1. **Pre-flight** — ``AgentRuntime.prepare()`` once, for the first task,
   before anything runs: a configuration that cannot run fails the whole
   sweep up front (:class:`PreflightError`, exit code 2), not once per task
   after each one opened a trace.
2. **Iteration** — which tasks, in which order, with which subset.
3. **Isolation** — a fresh :class:`ComposedRun` per task by default, so
   task N's browser, memory and hook state cannot leak into task N+1.
4. **Identity** — every run carries ``(benchmark, subset, task_id)`` and
   a stable ``run_id`` into the trace.

The summary counts three ledgers per task — how the agent ended
(``result.json``), whether the environment held up, whether the trace is a
complete record (its manifest) — and names every infrastructure failure: an
environment that failed, a model unavailable after the provider's retries,
a trace that is incomplete or never closed. A guard's veto stops the sweep,
is shown, and has its own exit code (:data:`VETO_EXIT_CODE`).

Everything else — env setup/teardown ordering, lifecycle dispatch,
timeout, result construction — belongs to :class:`AgentRuntime` and is
deliberately *not* duplicated here.

Scoring stays a separate pass, driven by
:class:`~spectragent.evaluator.runner.EvaluationRunner` over the trace
directories once every task has finished. The runner never hands an
Evaluator a live agent or environment — an Evaluator reads persisted
traces, which is what lets the same sweep be re-scored later, by a
different judge, on a machine with no browser. Wiring it in here only
saves you from typing the second command.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from spectragent.core.composer import ComposedRun
from spectragent.core.llm.exceptions import ModelOutputTruncatedError
from spectragent.core.loops.errors import RunAbort
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.encoding import MANIFEST_FILE
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus

logger = logging.getLogger(__name__)

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")

#: ``spectragent-bench``'s exit code when the pre-flight check refused the
#: configuration: no task ran and no trace was opened (0 and 1 are a sweep
#: that ran, with no infrastructure failure / with one).
PREFLIGHT_EXIT_CODE = 2
#: ``spectragent-bench``'s exit code when a guard vetoed the sweep: the sweep
#: stopped at the vetoed task, whose trace and the finished tasks' traces stay
#: (the flywheel chain stops on it, arch §4.16).
VETO_EXIT_CODE = 3

#: ``StepRecord.error_phase`` of a step whose model call failed (``ReactLoop``).
_MODEL_CALL_PHASE = "llm"


def _safe_run_id(task_id: str, *, prefix: str = "") -> str:
    """Filesystem-safe, collision-resistant trace directory name.

    Keeps the task id legible — the whole point is that ``ls traces/``
    answers "which task is this?" — and suffixes a short random tag so
    re-running a task does not clobber the previous attempt.
    """
    safe = _UNSAFE_ID.sub("-", task_id).strip("-")[:64] or "task"
    tag = uuid.uuid4().hex[:8]
    return f"{prefix}{safe}_{tag}"


def _trace_dir_of(run: ComposedRun) -> Path | None:
    """Locate the trace a run wrote, via any hook exposing ``run_dir``.

    Duck-typed on purpose: the runner must not depend on TraceWriter, and
    a project swapping in its own persistence hook keeps this working.
    """
    for hook in run.hooks:
        run_dir = getattr(hook, "run_dir", None)
        if isinstance(run_dir, Path):
            return run_dir
    return None


class PreflightError(RuntimeError):
    """The sweep's configuration was refused before any task ran (:meth:`BenchmarkRunner.preflight`).

    Chained from the refusal itself, which names both parties
    (``IncompatibleError``, a negotiation error, ``BrowserCooperationError``
    …). Nothing was set up and no trace was opened.

    Attributes:
        task_id: The task the configuration was checked for (the sweep's first).
        error: The refusal.
    """

    def __init__(self, task_id: str, error: BaseException) -> None:
        self.task_id = task_id
        self.error = error
        super().__init__(
            f"the pre-flight check refused the configuration for task {task_id!r}, so no task "
            f"was run: {type(error).__name__}: {error}"
        )


@dataclass(frozen=True)
class TaskOutcome:
    """What happened to one task — the agent's ledger, the environment's, the trace's.

    ``result`` says how the agent ended (``agent_done``) and whether the
    environment held up (``env_status``); the trace's own ledger — whether
    the trace is a complete record of the run — is read back from its
    manifest once the run is over (``trace_status``, ``trace_note``).
    """

    task_id: str
    result: Any = None
    trace_dir: Path | None = None
    error: str | None = None
    elapsed_ms: int = 0
    #: The guard's veto (a ``RunAbort``), when one stopped this task — and
    #: with it the sweep.
    vetoed: str | None = None
    #: The trace's status as its manifest says once the run is over:
    #: ``complete``, ``incomplete`` or ``writing`` (never closed out), or
    #: ``unreadable``; ``None`` when no trace was written.
    trace_status: str | None = None
    #: Why the trace is incomplete (its ``incomplete_reason``), or why it
    #: does not read back.
    trace_note: str | None = None
    #: The model call that ended the run by failing after the provider's own
    #: retries (its error class and message), when that is how the run ended.
    model_unavailable: str | None = None

    @property
    def agent_done(self) -> str:
        if self.vetoed is not None:
            return str(AgentDoneStatus.VETOED)
        done = getattr(self.result, "agent_done", None)
        return str(done) if done is not None else "not_started"

    @property
    def env_status(self) -> str:
        status = getattr(self.result, "env_status", None)
        return str(status) if status is not None else ""

    @property
    def infrastructure_error(self) -> str | None:
        """What stopped this task from being a real, fully recorded attempt, if anything.

        ``error`` alone is not that answer. The sweep calls
        ``execute(raise_on_error=False)``, so a browser that never
        connected comes back as an ordinary ``AgentResult`` — no
        exception, ``error=None`` — carrying ``env_status=failed`` and
        the real message inside ``result.error``. Reading only the
        outcome's own ``error`` reports 300 unstarted browsers as 300
        completed tasks and exits 0.

        So each ledger is consulted for what it alone can say: the
        environment failed (``env_status`` exists precisely to separate
        that from an agent that did badly); the model was unavailable — its
        call failed after the provider's own retries, which the agent ledger
        records as an ordinary ``error``; the trace is not a complete record
        (``incomplete``, never closed, unreadable). An agent that ran out of
        steps in a healthy browser, fully recorded, is a completed attempt
        and stays ``ok``. A veto is not an infrastructure failure: it is
        reported on its own (``vetoed``).
        """
        if self.error is not None:
            return self.error
        if self.result is not None and self.env_status == EnvironmentStatus.FAILED:
            return str(getattr(self.result, "error", None) or "environment failed")
        if self.model_unavailable is not None:
            return f"the model call failed after the provider's retries: {self.model_unavailable}"
        if self.trace_status == "incomplete":
            return f"the trace is incomplete: {self.trace_note}"
        if self.trace_status == "writing":
            return "the trace was never closed out (its manifest still says writing)"
        if self.trace_status == "unreadable":
            return f"the trace does not read back: {self.trace_note}"
        return None

    @property
    def ok(self) -> bool:
        """The task ran to a terminal state, fully recorded, with no infrastructure failure or veto."""
        return self.vetoed is None and self.result is not None and self.infrastructure_error is None


@dataclass(frozen=True)
class BenchmarkRunSummary:
    """Aggregate of one benchmark sweep: three counters, the failures, the veto.

    The counters are the three ledgers: how the agents ended
    (:meth:`count_by_done`), whether the environment held up
    (:meth:`count_by_environment`), whether each trace is a complete record
    (:meth:`count_by_trace`). Whether an answer was correct stays an
    Evaluator's business and lives in ``eval_summary``, which is ``None``
    unless the config declared an ``evaluator:`` layer — a sweep is
    perfectly valid without one.
    """

    benchmark: str
    subset: str
    outcomes: list[TaskOutcome] = field(default_factory=list)
    eval_summary: Any = None
    eval_errors: list[str] = field(default_factory=list)

    #: Set when a guard vetoed the sweep (``RunAbort``), naming the task that
    #: did it. ``outcomes`` ends with that task, so without this a short sweep
    #: is indistinguishable from a small one — and a reader would compare a
    #: 3-task partial against a 30-task full one.
    aborted: str | None = None

    @property
    def was_aborted(self) -> bool:
        return self.aborted is not None

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def completed(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failed(self) -> int:
        """Tasks with an infrastructure failure (:attr:`TaskOutcome.infrastructure_error`)."""
        return sum(1 for o in self.outcomes if o.infrastructure_error is not None)

    def count_by_done(self) -> dict[str, int]:
        """How the agents ended: done, max_steps, error, timeout, vetoed (not_started: no result)."""
        return _count(outcome.agent_done for outcome in self.outcomes)

    def count_by_environment(self) -> dict[str, int]:
        """Whether the environment held up: success, failed (``(no result)``: nothing to say)."""
        return _count(outcome.env_status or "(no result)" for outcome in self.outcomes)

    def count_by_trace(self) -> dict[str, int]:
        """Whether each trace is a complete record: complete, incomplete, writing, unreadable."""
        return _count(outcome.trace_status or "(no trace)" for outcome in self.outcomes)

    def format_table(self) -> str:
        header = (
            f"benchmark={self.benchmark} subset={self.subset or '-'} tasks={self.total} "
            f"completed={self.completed} infrastructure_failures={self.failed}"
        )
        if self.aborted is not None:
            header += " vetoed=1"
        lines = [header]
        for label, counts in (
            ("agent outcome", self.count_by_done()),
            ("environment", self.count_by_environment()),
            ("trace", self.count_by_trace()),
        ):
            listed = ", ".join(f"{key}={count}" for key, count in sorted(counts.items()))
            lines.append(f"  {label}: {listed or '-'}")
        for outcome in self.outcomes:
            where = outcome.trace_dir.name if outcome.trace_dir else "-"
            # ``infrastructure_error``, not ``error``: the message for a
            # browser that never started lives on the result, and printing
            # only the bare status left "Environment setup failed: ..."
            # unreachable from the table it belongs in.
            failure = outcome.infrastructure_error
            if outcome.vetoed is not None:
                detail = f" vetoed: {outcome.vetoed}"
            elif failure:
                detail = f" infrastructure failure: {failure}"
            else:
                detail = ""
            lines.append(
                f"  {outcome.task_id}: {outcome.agent_done} "
                f"({outcome.elapsed_ms} ms, trace={where}){detail}"
            )
        if self.aborted is not None:
            lines.append(
                f"vetoed: a guard stopped the sweep at {self.aborted}; no task after it ran"
            )
        if self.eval_summary is not None:
            from spectragent.evaluator.summary import format_summary

            lines.append("evaluation:")
            lines.extend(f"  {line}" for line in format_summary(self.eval_summary).splitlines())
        for message in self.eval_errors:
            lines.append(f"  eval error: {message}")
        return "\n".join(lines)


def _count(keys: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    return counts


class _TraceLedger(NamedTuple):
    """What a finished run's trace says about it (:func:`_trace_ledger`)."""

    status: str | None = None
    note: str | None = None
    model_unavailable: str | None = None
    #: The run's ``result.json`` — what a vetoed run's outcome is read from,
    #: since ``execute`` raises the veto instead of returning a result.
    result: Any = None


def _trace_ledger(trace_dir: Path | None, result: Any) -> _TraceLedger:
    """The trace's own ledger and the model call a run in error ended on, read back from the trace.

    Read once the run is over, from what was committed — the manifest's
    status and ``incomplete_reason``; and, for a run that ended in error on
    a model call, that call's recorded failure (*result*, or the trace's own
    ``result.json`` when there is no *result*, says how the run ended). A
    truncated reply is the configured token ceiling, not the model's
    availability, so it is not counted as one. No trace (none written, or
    its manifest never made it to disk) is the empty ledger; one that does
    not read back is ``unreadable`` with the reason.
    """
    if trace_dir is None or not (trace_dir / MANIFEST_FILE).exists():
        return _TraceLedger()
    try:
        trace = RunTrace(trace_dir)
        recorded = result if result is not None else trace.result
        model = _model_call_that_ended(trace, recorded)
    except TraceSchemaError as exc:
        return _TraceLedger(status="unreadable", note=str(exc))
    return _TraceLedger(
        status=trace.manifest.status.value,
        note=trace.manifest.incomplete_reason,
        model_unavailable=model,
        result=recorded,
    )


def _model_call_that_ended(trace: RunTrace, result: Any) -> str | None:
    """The failed model call a run in error ended on — ``"<error class>: <detail>"`` — or ``None``."""
    if getattr(result, "agent_done", None) != AgentDoneStatus.ERROR:
        return None
    policy = [record for record in trace.step_records if record.origin == "policy"]
    if not policy or policy[-1].error_phase != _MODEL_CALL_PHASE:
        return None
    failed = [
        call.failure
        for call in trace.llm_messages(policy[-1].step)
        if call.status == "failed" and call.failure is not None
    ]
    if not failed or failed[-1].error == ModelOutputTruncatedError.__name__:
        return None
    return f"{failed[-1].error}: {failed[-1].detail}"


class BenchmarkRunner:
    """Run every task of a benchmark through a composed agent stack.

    Args:
        loader: A ``TaskLoader`` — the source of task ids, task text and
            per-task environment context.
        compose: Zero-arg factory returning a fresh :class:`ComposedRun`.
            Called once per task (the first task's call happens in the
            pre-flight, whose stack that task then runs on). Pass
            ``lambda: existing_run`` to reuse one stack across tasks;
            ``AgentRuntime.execute`` clears memory either way, but a fresh
            compose is the only thing that also resets capability and hook
            state.
        agent_id: Which agent instance to drive. Defaults to the run's
            only agent.
        env_id: Which environment to run against. Required only when a
            run holds more than one.
        timeout: Per-task wall-clock budget.
        benchmark / subset: Recorded identity. Default to the loader's own.
        run_id_prefix: Prepended to every generated trace directory name.
        evaluator: Optional ``BaseEvaluator``. When given, every trace the
            sweep produced is scored after the last task finishes.
        eval_loader: Source of ``EvalReference`` rows for that evaluator.
            Required whenever *evaluator* is set — a verdict needs ground
            truth, and the task loader does not carry it.
        evals_dir: Optional flat output directory for verdicts and the
            summary. Verdicts always also land in each trace's ``evals/``.
    """

    def __init__(
        self,
        *,
        loader: Any,
        compose: Callable[[], ComposedRun],
        agent_id: str | None = None,
        env_id: str | None = None,
        timeout: float = 300.0,
        benchmark: str | None = None,
        subset: str | None = None,
        run_id_prefix: str = "",
        evaluator: Any = None,
        eval_loader: Any = None,
        evals_dir: Path | None = None,
    ) -> None:
        self._loader = loader
        self._compose = compose
        self._agent_id = agent_id
        self._env_id = env_id
        self._timeout = timeout
        self._benchmark = benchmark or getattr(loader, "benchmark_name", "")
        self._subset = subset if subset is not None else getattr(loader, "subset", "")
        self._run_id_prefix = run_id_prefix
        if evaluator is not None and eval_loader is None:
            raise ValueError(
                "BenchmarkRunner was given an evaluator but no eval_loader. "
                "Scoring needs reference data; pass the benchmark's eval "
                "loader or drop the evaluator."
            )
        self._evaluator = evaluator
        self._eval_loader = eval_loader
        self._evals_dir = evals_dir

    @property
    def benchmark(self) -> str:
        return self._benchmark

    @property
    def subset(self) -> str:
        return self._subset

    def select_task_ids(
        self,
        *,
        task_ids: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[str]:
        selected = list(task_ids) if task_ids is not None else list(self._loader.task_ids())
        if limit is not None:
            selected = selected[:limit]
        return selected

    async def run(
        self,
        *,
        task_ids: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> BenchmarkRunSummary:
        """Run the selected tasks sequentially and summarise.

        The configuration is checked first, once, for the first selected
        task (:meth:`preflight`): a configuration that cannot run fails the
        sweep before any task runs or any trace is opened.

        Raises:
            PreflightError: The pre-flight check refused the configuration.
        """
        selected = self.select_task_ids(task_ids=task_ids, limit=limit)
        logger.info(
            "BenchmarkRunner: %s/%s — %d task(s)",
            self._benchmark,
            self._subset or "-",
            len(selected),
        )
        # The first task runs on the stack the pre-flight assembled, so the
        # sweep still composes exactly one stack per task.
        prepared = self.preflight(selected[0]) if selected else None
        outcomes: list[TaskOutcome] = []
        aborted: str | None = None
        for task_id in selected:
            run, prepared = prepared, None
            outcome = await self.run_task(task_id, run=run)
            outcomes.append(outcome)
            if outcome.vetoed is not None:
                # Stop the sweep, but keep what it produced. The guard said
                # "stop", not "destroy the evidence": the finished tasks and
                # the vetoed one have closed traces, and under Record Once,
                # Evaluate Many those are still worth scoring. Losing them
                # silently would make a veto more destructive than a crash.
                aborted = f"{task_id}: {outcome.vetoed}"
                logger.warning(
                    "BenchmarkRunner: sweep vetoed at %s after %d/%d task(s)",
                    task_id,
                    len(outcomes),
                    len(selected),
                )
                break
        eval_summary, eval_errors = await self._evaluate(
            outcomes, selected[: len(outcomes)] if aborted else selected
        )
        return BenchmarkRunSummary(
            benchmark=self._benchmark,
            subset=self._subset,
            outcomes=outcomes,
            eval_summary=eval_summary,
            eval_errors=eval_errors,
            aborted=aborted,
        )

    async def _evaluate(
        self, outcomes: Sequence[TaskOutcome], selected: Sequence[str]
    ) -> tuple[Any, list[str]]:
        """Score the traces this sweep produced, if an evaluator is wired.

        Runs *after* every task, over trace directories rather than live
        objects — so an evaluation crash cannot destroy a sweep's worth of
        compute, and the same traces can be re-scored later without
        re-running anything.
        """
        if self._evaluator is None:
            return None, []

        from spectragent.evaluator.runner import EvaluationRunner

        runner = EvaluationRunner(
            evaluator=self._evaluator,
            eval_loader=self._eval_loader,
            evals_dir=self._evals_dir,
        )
        trace_dirs = [o.trace_dir for o in outcomes if o.trace_dir is not None]
        try:
            report = await runner.run(
                trace_dirs,
                expected_task_ids=selected,
                benchmark=self._benchmark,
                subset=self._subset,
            )
        except Exception as exc:
            logger.exception("BenchmarkRunner: evaluation pass failed")
            return None, [f"{type(exc).__name__}: {exc}"]
        return report.summary(), [f"{o.task_id}: {o.error}" for o in report.errors if o.error]

    def preflight(self, task_id: str) -> ComposedRun:
        """Check the configuration once for *task_id*, before the sweep starts; return its stack.

        Composes the task's stack and runs ``AgentRuntime.prepare()`` on it
        with the task's loop inputs and environment setup context: slot
        validation, the Loop's inputs, the negotiations and the
        environment's setup check (``docs/architecture_design.md`` §4.2) —
        everything that would refuse every task of the sweep alike. Nothing
        is set up, no hook is called and no trace is opened. The returned
        stack is the one this task then runs on (``run_task(run=...)``).

        Raises:
            PreflightError: Naming the task and the refusal, chained from it
                — including a stack that cannot be composed and a task
                context that cannot be read.
        """
        try:
            context = self._loader.get_task_context(task_id)
            run = self._compose()
            agent_id = self._agent_id or run.ids("agent")[0]
            runtime = AgentRuntime.for_agent(run, agent_id, env_id=self._env_id)
            runtime.prepare(task_id=task_id, loop_inputs=context.loop, env_context=context.env)
        except Exception as exc:
            raise PreflightError(task_id, exc) from exc
        return run

    async def run_task(self, task_id: str, *, run: ComposedRun | None = None) -> TaskOutcome:
        """Run one task end to end. Never raises — a failure is data.

        A benchmark sweep that dies on task 3 of 300 has destroyed the
        other 297 runs' worth of compute, so infrastructure failures are
        captured per task and recorded. A guard's veto (``RunAbort``) is
        recorded too — the outcome's ``vetoed`` — and :meth:`run` stops the
        sweep on it. Once the run is over, the trace's own ledger is read
        back from its manifest (``trace_status``), and so is the model call a
        run in error ended on, when it failed after the provider's retries.

        Args:
            task_id: The task.
            run: The stack to run it on — the one :meth:`preflight` assembled
                for the sweep's first task; composed fresh when omitted.
        """
        started = time.monotonic()
        try:
            task = self._loader[task_id]
        except Exception as exc:
            logger.exception("BenchmarkRunner: cannot load task %s", task_id)
            return TaskOutcome(
                task_id=task_id,
                error=f"task load failed: {type(exc).__name__}: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        try:
            run = run if run is not None else self._compose()
            agent_id = self._agent_id or run.ids("agent")[0]
            runtime = AgentRuntime.for_agent(run, agent_id, env_id=self._env_id)
        except Exception as exc:
            logger.exception("BenchmarkRunner: compose failed for %s", task_id)
            return TaskOutcome(
                task_id=task_id,
                error=f"compose failed: {type(exc).__name__}: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        # The link the framework never had: dataset setup → capability.
        # For SWE-bench this is the container image and checkout commit;
        # for a browser benchmark, the start URL. The task's context has
        # three parts with three consumers, and each goes to its own
        # parameter: ``env`` to the capabilities' setup, ``loop`` to the
        # Loop (a branch task's checkpoint), ``branch`` to the trace.
        context = self._loader.get_task_context(task_id)

        try:
            result = await runtime.execute(
                task=getattr(task, "task_text", ""),
                task_id=task_id,
                benchmark=self._benchmark,
                subset=self._subset,
                run_id=_safe_run_id(task_id, prefix=self._run_id_prefix),
                env_context=context.env,
                loop_inputs=context.loop,
                branch=context.branch,
                timeout=self._timeout,
                raise_on_error=False,
                config=_task_snapshot(task, self._benchmark, self._subset),
            )
            error = vetoed = None
        except RunAbort as exc:
            # First: this handler exists so one bad task does not stop the
            # sweep, and a veto is precisely the case where it should.
            # ``execute`` has already closed the trace out, so the veto and
            # its evidence survive — and a close-out that failed under the
            # veto (a full disk), or the run's own timeout expiring while the
            # vetoed step was being recorded, arrives as a note on it, never
            # in its place. ``run`` stops the sweep on the outcome's
            # ``vetoed``.
            logger.warning("BenchmarkRunner: task %s vetoed the sweep: %s", task_id, exc)
            result = error = None
            vetoed = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            # With ``raise_on_error=False`` what reaches here never got a
            # clean close-out: ``prepare`` or ``on_run_start`` failed, or a
            # blocking hook raised while closing out a run that was not
            # vetoed — ``execute`` closes a run out once and does not retry
            # it.
            logger.exception("BenchmarkRunner: task %s failed", task_id)
            result = vetoed = None
            error = f"{type(exc).__name__}: {exc}"

        trace_dir = _trace_dir_of(run)
        ledger = _trace_ledger(trace_dir, result)
        return TaskOutcome(
            task_id=task_id,
            # A veto leaves the run's result only in its trace.
            result=ledger.result if vetoed is not None else result,
            trace_dir=trace_dir,
            error=error,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            vetoed=vetoed,
            trace_status=ledger.status,
            trace_note=ledger.note,
            model_unavailable=ledger.model_unavailable,
        )


def _task_snapshot(task: Any, benchmark: str, subset: str) -> dict[str, Any]:
    """Small, secret-free context recorded into the trace config.

    Deliberately not the whole composed YAML: that carries credentials.
    Difficulty/domain/tags are here because they are what you slice
    evaluation results by.
    """
    return {
        "benchmark": benchmark,
        "subset": subset,
        "env_type": getattr(task, "env_type", ""),
        "difficulty": getattr(task, "difficulty", None),
        "domain": getattr(task, "domain", None),
        "tags": list(getattr(task, "tags", []) or []),
    }


# ── CLI ────────────────────────────────────────────────────────────


def build_composer() -> Any:
    """Build a ConfigComposer over every registered layer."""
    from spectragent.core.composer import ConfigComposer
    from spectragent.scripts.generate_all import _collect_registrars

    return ConfigComposer(registrars=_collect_registrars())


def _resolve_loader(run: ComposedRun, benchmark: str | None, subset: str | None) -> Any:
    """Prefer the loader the config wired; fall back to explicit flags."""
    loader = run.optional_single("task_loader")
    if loader is not None:
        return loader
    if not benchmark:
        raise SystemExit(
            "No task_loader in the config and no --benchmark given: "
            "nothing says which dataset to run."
        )
    from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar

    loader_cls = TaskLoaderRegistrar.get(benchmark)
    return loader_cls(subset=subset) if subset else loader_cls()


def _resolve_evaluator(run: ComposedRun, evaluator_id: str | None) -> Any:
    """Pick the evaluator instance the config built, if any.

    Evaluators are resolved from the composed run rather than constructed
    from the registry, because an LLM-judge evaluator's judge model is a
    wired dependency: building one straight off ``EvaluatorRegistrar.get``
    would leave ``self.llm`` uninjected and fail at the first call. Declare
    the evaluator in YAML and it arrives with its judge attached.
    """
    instances = run.instances("evaluator")
    if not instances:
        if evaluator_id:
            raise SystemExit(
                f"--evaluator {evaluator_id!r} was requested but the config "
                f"declares no 'evaluator:' layer. Add one so its judge model "
                f"is wired from YAML like every other component."
            )
        return None
    if evaluator_id is None:
        if len(instances) > 1:
            raise SystemExit(
                f"The config declares {len(instances)} evaluators "
                f"({sorted(run.ids('evaluator'))}). Pass --evaluator to say "
                f"which one should score this sweep."
            )
        return instances[0]
    matches = [e for e in instances if e.evaluator_id == evaluator_id]
    if not matches:
        raise SystemExit(
            f"No evaluator with evaluator_id={evaluator_id!r} in the config "
            f"(found: {sorted({e.evaluator_id for e in instances})})."
        )
    return matches[0]


def _resolve_eval_loader(run: ComposedRun, benchmark: str | None, subset: str | None) -> Any:
    """Reference data for scoring: config first, then the registry.

    Falls back to ``EvalLoaderRegistrar.get(benchmark)`` because the eval
    loader carries no secrets and no wiring — the benchmark name is enough
    to construct it, and requiring it in YAML would be ceremony. Called only
    when an evaluator is configured: a sweep that scores nothing never asks.

    A benchmark with no registered eval loader stops the sweep before
    anything runs, naming it — ``trace_branches`` has none in v1 (how a
    branch result becomes a training signal is open, §4.10), and a sweep
    must not run for hours to fail at its scoring pass.
    """
    loader = run.optional_single("eval_loader")
    if loader is not None:
        return loader
    if not benchmark:
        raise SystemExit(
            "An evaluator is configured but nothing says which reference "
            "data to score against: add an 'eval_loader:' block or pass "
            "--benchmark."
        )
    from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar

    loader_cls = EvalLoaderRegistrar.get_or_none(benchmark)
    if loader_cls is None:
        raise SystemExit(
            f"An evaluator is configured, but benchmark {benchmark!r} has no registered "
            f"eval loader to score against (a 'trace_branches' sweep has none): add an "
            f"'eval_loader:' block, drop the evaluator, or pass --no-eval."
        )
    return loader_cls(subset=subset) if subset else loader_cls()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectragent-bench",
        description="Run a benchmark's tasks through a composed agent stack.",
    )
    parser.add_argument("--config", required=True, type=Path, help="YAML run config.")
    parser.add_argument("--benchmark", help="Task loader key (if not in the config).")
    parser.add_argument("--subset", help="Benchmark subset/split.")
    parser.add_argument("--task-id", action="append", dest="task_ids", help="Repeatable.")
    parser.add_argument("--limit", type=int, help="Run at most N tasks.")
    parser.add_argument("--timeout", type=float, default=300.0, help="Per-task seconds.")
    parser.add_argument("--agent-id", help="Agent instance id to drive.")
    parser.add_argument("--env-id", help="Environment instance id to run against.")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the selected task ids and exit without running.",
    )
    parser.add_argument(
        "--evaluator",
        help=("evaluator_id to score with. Only needed when the config declares more than one."),
    )
    parser.add_argument(
        "--evals-dir",
        type=Path,
        default=Path("evals"),
        help=(
            "Flat output directory for EvalResults and the EvalSummary. "
            "Verdicts also always land in each trace's own evals/."
        ),
    )
    parser.add_argument(
        "--no-eval",
        action="store_true",
        help="Record traces only; skip the scoring pass.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import yaml

    raw = yaml.safe_load(args.config.read_text("utf-8"))
    composer = build_composer()
    probe = composer.compose(raw)
    loader = _resolve_loader(probe, args.benchmark, args.subset)

    evaluator = None if args.no_eval else _resolve_evaluator(probe, args.evaluator)
    benchmark = args.benchmark or getattr(loader, "benchmark_name", "")
    subset = args.subset if args.subset is not None else getattr(loader, "subset", "")
    eval_loader = _resolve_eval_loader(probe, benchmark, subset) if evaluator is not None else None

    runner = BenchmarkRunner(
        loader=loader,
        compose=lambda: composer.compose(raw),
        agent_id=args.agent_id,
        env_id=args.env_id,
        timeout=args.timeout,
        benchmark=args.benchmark,
        subset=args.subset,
        evaluator=evaluator,
        eval_loader=eval_loader,
        evals_dir=args.evals_dir if evaluator is not None else None,
    )

    selected = runner.select_task_ids(task_ids=args.task_ids, limit=args.limit)
    if args.list:
        for task_id in selected:
            print(task_id)
        return 0

    try:
        summary = asyncio.run(runner.run(task_ids=selected))
    except PreflightError as exc:
        # Nothing ran and no trace exists: exit 2, never 0 or 1 (the codes of
        # a sweep that ran), so a script or the flywheel chain stops here.
        print(f"spectragent-bench: {exc}", file=sys.stderr)
        return PREFLIGHT_EXIT_CODE
    print(summary.format_table())
    if summary.was_aborted:
        # A guard said stop: its own code, so a script or the flywheel chain
        # stops here too instead of feeding the vetoed stage's runs onwards.
        print(f"spectragent-bench: the sweep was vetoed at {summary.aborted}", file=sys.stderr)
        return VETO_EXIT_CODE
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

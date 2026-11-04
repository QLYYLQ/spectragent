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

**The token limit** (``token_limit``, ``--token-limit``; the user's ruling of
2026-10-14, ``docs/architecture_design.md`` §4.2). ``unlimited``, the
default, reads no usage and changes nothing. With a number, each finished
task's tokens are read from its trace's call records
(``core/trace/usage.py``; no metrics hook needed) and added to the sweep's
total; once the total reaches the limit, the task that reached it has
finished, no task after it starts, and the sweep has its own exit code
(:data:`TOKEN_LIMIT_EXIT_CODE`). Tasks run one at a time, so a task is never
cut off by the limit: a sweep overshoots it by at most one task. A call whose
provider reported no usage adds no tokens and is named in the summary.

Everything else — env setup/teardown ordering, lifecycle dispatch,
timeout, result construction — belongs to :class:`AgentRuntime` and is
deliberately *not* duplicated here.

The benchmark's offline verifiers judge the sweep in a separate pass, driven
by :class:`~spectragent.verifiers.runner.VerificationRunner` over the trace
directories once every task has finished (``docs/architecture_design.md``
§4.10.1 "只有两个挂接点"). The runner never hands a verifier a live agent or
environment — an offline verifier reads committed traces, which is what lets
the same sweep be judged again later, by a different judge, on a machine with
no browser; each verdict goes into its run's own verdict store. Running the
pass here only saves you from typing ``spectragent eval``.

**A branch task belongs to its source run's benchmark** (§4.10.1 "分叉 sweep 是一种任务来源"):
a branch sweep's task source (``trace_branches``) is no benchmark of its own,
and each task names its source's (``TaskContext.benchmark``). The runner
composes such a task's stack, judges its trace and reads its primary verdict
as that benchmark's (:class:`TaskBenchmark`), records the source's
``benchmark_spec``, and checks the configuration once for the first task of
each benchmark the sweep's tasks belong to; a sweep over runs of several
benchmarks does each per task.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import re
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from spectragent.benchmarks import Benchmark
from spectragent.benchmarks.selection import read_benchmark, read_spec, run_root, spec_of
from spectragent.core.benchmark_session import SessionLedger, TaskProgress
from spectragent.core.composer import ComposedRun, ConfigComposer
from spectragent.core.data_loader.eval.base import BaseEvalLoader
from spectragent.core.experiment import ExperimentScope
from spectragent.core.llm.exceptions import ModelOutputTruncatedError
from spectragent.core.loops.errors import RunAbort
from spectragent.core.runtime import VERIFY_TIMEOUT_S, AgentRuntime
from spectragent.core.trace.encoding import MANIFEST_FILE
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.usage import RunUsage, recorded_usage
from spectragent.schemas._types import (
    UNLIMITED,
    AgentDoneStatus,
    EnvironmentStatus,
    Unlimited,
    limit_value,
)
from spectragent.schemas.benchmark_spec import BenchmarkSpec
from spectragent.schemas.config_source import ConfigSource
from spectragent.schemas.task import TaskContext
from spectragent.schemas.verdict import Verdict
from spectragent.verifiers.base import OfflineVerifier
from spectragent.verifiers.report import VerifierTally, tallies
from spectragent.verifiers.runner import VerificationRunner

logger = logging.getLogger(__name__)

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")

#: ``spectragent bench``'s exit code when the pre-flight check refused the
#: configuration: no task ran and no trace was opened (0 and 1 are a sweep
#: that ran, with no infrastructure failure / with one).
PREFLIGHT_EXIT_CODE = 2
#: ``spectragent bench``'s exit code when a guard vetoed the sweep: the sweep
#: stopped at the vetoed task, whose trace and the finished tasks' traces stay
#: (the flywheel chain stops on it, arch §4.16).
VETO_EXIT_CODE = 3
#: ``spectragent bench``'s exit code when the tokens the sweep's traces recorded
#: reached its ``--token-limit``: the task that reached it finished, no task
#: after it started (the flywheel chain stops on it, arch §4.16).
TOKEN_LIMIT_EXIT_CODE = 4

#: ``StepRecord.error_phase`` of a step whose model call failed (``ReactLoop``),
#: and of one whose prompt failed, as when a helper model the prompt builder
#: calls fails (M3A's step summary).
_MODEL_CALL_PHASES = ("llm", "prompt")


def safe_run_id(task_id: str, *, prefix: str = "") -> str:
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


def _records_usage(run: ComposedRun) -> bool:
    """Whether a recording hook declares that it persists usage facts."""
    return any(
        hasattr(hook, "run_dir") and getattr(getattr(hook, "recording", None), "metrics", False)
        for hook in run.hooks
    )


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
    #: The tokens the task's trace recorded (``core/trace/usage.py``), read only
    #: when the sweep has a token limit. ``None`` without a limit, when no trace
    #: was written (then no model call was recorded either), or when the trace
    #: does not read back (``usage_note`` says why).
    usage: RunUsage | None = None
    #: Why the task's tokens could not be counted: its trace, or one of its
    #: recorded calls, does not read back.
    usage_note: str | None = None
    #: The benchmark a branch task inherited from its source run
    #: (``TaskContext.benchmark``), which composed, judged and gives the primary
    #: verdict of its run; ``None``: the sweep's own benchmark did.
    inherited: BenchmarkSpec | None = None
    #: The verifier kind whose verdict is the brief result of the benchmark the
    #: task belongs to; ``None`` when it declares none.
    primary_verifier: str | None = None
    #: That benchmark's primary verdict on the run (§4.10.1 "主判定"), read once
    #: the sweep's verifiers have judged it; ``None`` when there is none.
    primary: Verdict | None = None
    #: How far the task succeeded by it, from 0 to 1 (the benchmark's success
    #: rule); ``None`` when no primary verdict judged the run.
    success: float | None = None
    #: Why the run's verdicts do not read back.
    primary_note: str | None = None

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
class TokenLimitReached:
    """The sweep's token total reached its limit: the task that reached it finished, none after it started."""

    #: The task whose tokens brought the total to the limit.
    task_id: str
    #: The sweep's total then: every finished task's recorded tokens.
    tokens: int
    limit: int
    #: Selected tasks that never started.
    not_started: int


@dataclass(frozen=True)
class PrimaryTally:
    """One primary verifier kind over the sweep's tasks whose benchmark declares it (§4.10.1 "主判定").

    A sweep of one benchmark has at most one; a branch sweep over runs of
    several benchmarks has one per kind they declare, since a success rate
    across different judges would mean nothing.
    """

    verifier_id: str
    #: The tasks whose benchmark declares this kind its primary verdict.
    tasks: int
    #: Those a primary verdict of this kind judged.
    judged: int
    #: Their mean success; ``None`` when it judged none.
    success_rate: float | None

    def describe(self) -> str:
        rate = f"{self.success_rate:.1%}" if self.success_rate is not None else "-"
        return (
            f"primary verdict ({self.verifier_id}): success rate {rate} over the "
            f"{self.judged} of {self.tasks} task(s) it judged"
        )


@dataclass(frozen=True)
class BenchmarkRunSummary:
    """Aggregate of one benchmark sweep: three counters, the failures, the veto.

    The counters are the three ledgers: how the agents ended
    (:meth:`count_by_done`), whether the environment held up
    (:meth:`count_by_environment`), whether each trace is a complete record
    (:meth:`count_by_trace`). Whether an answer was correct is the
    verifiers' business: ``verification`` holds each offline verifier's
    counts over the sweep, and is empty when the benchmark configures none
    (or with ``--no-eval``) — a sweep is perfectly valid without one.
    """

    benchmark: str
    subset: str
    outcomes: list[TaskOutcome] = field(default_factory=list)
    #: Each offline verifier's counts over the sweep's traces.
    verification: list[VerifierTally] = field(default_factory=list)
    #: ``<task> (<slug>): <why>`` for each trace a verifier could not judge,
    #: or the pass's own failure.
    verification_errors: list[str] = field(default_factory=list)

    #: Set when a guard vetoed the sweep (``RunAbort``), naming the task that
    #: did it. ``outcomes`` ends with that task, so without this a short sweep
    #: is indistinguishable from a small one — and a reader would compare a
    #: 3-task partial against a 30-task full one.
    aborted: str | None = None
    #: The sweep's token limit; ``None`` is ``unlimited``: no usage was read,
    #: and the table is the one a sweep without a limit prints.
    token_limit: int | None = None
    #: Set when the sweep stopped because its token total reached the limit.
    token_limit_reached: TokenLimitReached | None = None

    @property
    def was_aborted(self) -> bool:
        return self.aborted is not None

    @property
    def tokens(self) -> int:
        """Every task's recorded tokens (only counted with a token limit)."""
        return sum(
            o.usage.tokens
            for o in self.outcomes
            if o.usage is not None and o.usage.tokens is not None
        )

    @property
    def calls(self) -> int:
        """Every task's recorded model calls (only counted with a token limit)."""
        return sum(
            o.usage.calls
            for o in self.outcomes
            if o.usage is not None and o.usage.calls is not None
        )

    @property
    def calls_without_usage(self) -> list[str]:
        """``<task>: step S call K (status)`` for every recorded call whose provider reported no usage."""
        return [
            f"{o.task_id}: {call.describe()}"
            for o in self.outcomes
            if o.usage is not None
            for call in o.usage.without_usage
        ]

    @property
    def uncounted(self) -> list[str]:
        """``<task>: <why>`` for every task whose tokens could not be read."""
        return [f"{o.task_id}: {o.usage_note}" for o in self.outcomes if o.usage_note is not None]

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def primaries(self) -> list[PrimaryTally]:
        """Each primary verifier kind the sweep's tasks' benchmarks declare, in the order of the tasks.

        A task with no judged primary verdict (no trace, a verifier that
        could not judge it, a live verdict that timed out) is not counted as
        a failure: it is left out of the rate, and ``judged`` against
        ``tasks`` says how much the rate covers.
        """
        kinds: dict[str, list[TaskOutcome]] = {}
        for outcome in self.outcomes:
            if outcome.primary_verifier is not None:
                kinds.setdefault(outcome.primary_verifier, []).append(outcome)
        counted: list[PrimaryTally] = []
        for kind, outcomes in kinds.items():
            values = [o.success for o in outcomes if o.success is not None]
            counted.append(
                PrimaryTally(
                    verifier_id=kind,
                    tasks=len(outcomes),
                    judged=len(values),
                    success_rate=sum(values) / len(values) if values else None,
                )
            )
        return counted

    @property
    def completed(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failed(self) -> int:
        """Tasks with an infrastructure failure (:attr:`TaskOutcome.infrastructure_error`)."""
        return sum(1 for o in self.outcomes if o.infrastructure_error is not None)

    def count_by_done(self) -> dict[str, int]:
        """How the agents ended: done, max_steps, episode_over, error, timeout, vetoed (not_started: no result)."""
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
        if self.token_limit_reached is not None:
            header += " token_limit_reached=1"
        lines = [header]
        for label, counts in (
            ("agent outcome", self.count_by_done()),
            ("environment", self.count_by_environment()),
            ("trace", self.count_by_trace()),
        ):
            listed = ", ".join(f"{key}={count}" for key, count in sorted(counts.items()))
            lines.append(f"  {label}: {listed or '-'}")
        lines += [f"  {tally.describe()}" for tally in self.primaries]
        if self.token_limit is not None:
            lines.append(
                f"  tokens: {self.tokens} recorded by {self.calls} call(s); "
                f"limit {self.token_limit}"
            )
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
            tokens = (
                f", {outcome.usage.tokens} tokens"
                if outcome.usage is not None and outcome.usage.tokens is not None
                else ""
            )
            primary = f", {_primary_words(outcome)}" if outcome.primary_verifier is not None else ""
            lines.append(
                f"  {outcome.task_id}: {outcome.agent_done} "
                f"({outcome.elapsed_ms} ms, trace={where}{tokens}{primary}){detail}"
            )
        lines += [f"  no usage reported (not counted): {call}" for call in self.calls_without_usage]
        lines += [f"  tokens not counted: {task}" for task in self.uncounted]
        if self.aborted is not None:
            lines.append(
                f"vetoed: a guard stopped the sweep at {self.aborted}; no task after it ran"
            )
        reached = self.token_limit_reached
        if reached is not None:
            lines.append(
                f"token limit: the sweep's runs recorded {reached.tokens} tokens, reaching its "
                f"limit of {reached.limit} after {reached.task_id}; {reached.not_started} task(s) "
                f"not started"
            )
        if self.verification:
            lines.append("verifiers:")
            lines.extend(f"  {tally.describe()}" for tally in self.verification)
        for message in self.verification_errors:
            lines.append(f"  could not judge: {message}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        """Machine-readable summary; preserve the independent result ledgers."""
        return {
            "benchmark": self.benchmark,
            "subset": self.subset,
            "tasks": self.total,
            "completed": self.completed,
            "infrastructure_failures": self.failed,
            "agent_outcomes": self.count_by_done(),
            "environment_statuses": self.count_by_environment(),
            "trace_statuses": self.count_by_trace(),
            "outcomes": [_outcome_dict(outcome) for outcome in self.outcomes],
            "aborted": self.aborted,
            "token_limit": self.token_limit,
            "tokens": self.tokens if self.token_limit is not None else None,
            "not_started": self.token_limit_reached.not_started
            if self.token_limit_reached
            else None,
            "verification": [
                {
                    "slug": tally.verifier.slug,
                    "verifier_id": tally.verifier.verifier_id,
                    "provided": tally.provided,
                    "judged": tally.judged,
                    "passed": tally.passed,
                    "failed": tally.failed,
                    "errors": tally.errors,
                    "success_rate": tally.success_rate,
                }
                for tally in self.verification
            ],
            "verification_errors": list(self.verification_errors),
            "primary": [
                {
                    "verifier_id": tally.verifier_id,
                    "tasks": tally.tasks,
                    "judged": tally.judged,
                    "success_rate": tally.success_rate,
                }
                for tally in self.primaries
            ],
        }


def _primary_words(outcome: TaskOutcome) -> str:
    """A task's primary verdict in a few words, for its line of the table."""
    verdict = outcome.primary
    if outcome.primary_note is not None:
        return f"verdicts unreadable: {outcome.primary_note}"
    if verdict is None:
        return "no primary verdict"
    if verdict.status != "judged":
        return f"primary not judged ({verdict.reason})"
    found = f"label {verdict.label}" if verdict.label is not None else f"score {verdict.score}"
    if outcome.success is None:
        return f"primary {found}"
    return f"primary {found}, success {outcome.success:g}"


def _outcome_dict(outcome: TaskOutcome) -> dict[str, Any]:
    return {
        "task_id": outcome.task_id,
        "agent_done": outcome.agent_done,
        "env_status": outcome.env_status,
        "trace_status": outcome.trace_status,
        "trace_dir": str(outcome.trace_dir) if outcome.trace_dir is not None else None,
        "elapsed_ms": outcome.elapsed_ms,
        "infrastructure_failure": outcome.infrastructure_error is not None,
        "vetoed": outcome.vetoed is not None,
        "tokens": outcome.usage.tokens if outcome.usage is not None else None,
        "primary": outcome.primary.slug if outcome.primary is not None else None,
        "success": outcome.success,
    }


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
    #: The tokens the trace's calls recorded, when they were asked for.
    usage: RunUsage | None = None
    #: Why they could not be counted.
    usage_note: str | None = None


def _trace_ledger(
    trace_dir: Path | None, result: Any, *, count_tokens: bool = False
) -> _TraceLedger:
    """The trace's own ledger and the model call a run in error ended on, read back from the trace.

    Read once the run is over, from what was committed — the manifest's
    status and ``incomplete_reason``; and, for a run that ended in error on
    a model call, that call's recorded failure (*result*, or the trace's own
    ``result.json`` when there is no *result*, says how the run ended). A
    truncated reply is the configured token ceiling, not the model's
    availability, so it is not counted as one. No trace (none written, or
    its manifest never made it to disk) is the empty ledger; one that does
    not read back is ``unreadable`` with the reason.

    With *count_tokens* (a sweep with a token limit), the tokens the trace's
    calls recorded too (``core/trace/usage.py``) — or why they could not be
    counted.
    """
    if trace_dir is None or not (trace_dir / MANIFEST_FILE).exists():
        return _TraceLedger()
    try:
        trace = RunTrace(trace_dir)
        recorded = result if result is not None else trace.result
        model = _model_call_that_ended(trace, recorded)
    except TraceSchemaError as exc:
        uncounted = f"the trace does not read back: {exc}" if count_tokens else None
        return _TraceLedger(status="unreadable", note=str(exc), usage_note=uncounted)
    usage = usage_note = None
    if count_tokens:
        try:
            usage = recorded_usage(trace)
            if not usage.measured:
                usage_note = "the recording plan did not select metrics"
            elif not usage.recording_complete:
                usage_note = "recording coverage is incomplete; tokens are a known subtotal"
                if usage.missing_steps:
                    usage_note += f" (missing steps: {', '.join(map(str, usage.missing_steps))})"
        except TraceSchemaError as exc:
            usage_note = f"a recorded model call does not read back: {exc}"
    return _TraceLedger(
        status=trace.manifest.status.value,
        note=trace.manifest.incomplete_reason,
        model_unavailable=model,
        result=recorded,
        usage=usage,
        usage_note=usage_note,
    )


def _model_call_that_ended(trace: RunTrace, result: Any) -> str | None:
    """The failed model call a run in error ended on — ``"<error class>: <detail>"`` — or ``None``."""
    if getattr(result, "agent_done", None) != AgentDoneStatus.ERROR:
        return None
    policy = [record for record in trace.step_records if record.origin == "policy"]
    if not policy or policy[-1].error_phase not in _MODEL_CALL_PHASES:
        return None
    if any(step > policy[-1].step for step in trace.manifest.missing_steps):
        return None
    failed = [
        call
        for call in trace.call_metrics(policy[-1].step)
        if call.status == "failed" and call.error is not None
    ]
    if not failed or failed[-1].error == ModelOutputTruncatedError.__name__:
        return None
    return f"{failed[-1].error}: {failed[-1].detail}"


@dataclass(frozen=True)
class TaskBenchmark:
    """A benchmark some of a sweep's tasks belong to, as the sweep composes and judges them.

    The sweep's own (the one ``--benchmark`` selected, whose task source gives
    the tasks), or the one a branch task inherits from its source run
    (``TaskContext.benchmark``, ``docs/architecture_design.md`` §4.10.1
    "分叉 sweep 是一种任务来源").
    """

    #: A fresh stack for one of its tasks.
    compose: Callable[[], ComposedRun]
    #: Its offline verifiers: each judges its tasks' traces after the sweep.
    verifiers: Sequence[OfflineVerifier] = ()
    #: Its reference rows, for a verifier that judges against one.
    reference_loader: BaseEvalLoader | None = None
    #: Which verdict is its tasks' brief result and how it counts; ``None``: none.
    primary_rule: Benchmark | None = None


class BenchmarkRunner:
    """Run every task of a benchmark through a composed agent stack.

    Args:
        loader: A ``TaskLoader`` — the source of task ids, task text and
            per-task environment context, and of the identity every run
            records (its ``benchmark_name`` and ``subset``).
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
        verify_timeout: Seconds each live verifier may take to judge a task
            after its run (``AgentRuntime.execute``'s ``verify_timeout``).
        run_id_prefix: Prepended to every generated trace directory name.
        verifiers: The benchmark's offline verifiers. Every trace the sweep
            produced is judged by each of them after the last task finishes,
            each verdict into its run's verdict store.
        reference_loader: The benchmark's reference rows, for a verifier
            that judges against one (``Benchmark.reference_loader()``).
        primary_rule: The benchmark that states which verdict is a run's
            brief result and how it counts (its ``primary_verdict`` and
            ``success``); the summary reports it once the verifiers have
            judged the sweep. ``None``: no primary verdict is reported.
        token_limit: The most tokens the sweep's runs may record: a number
            of at least 1, or ``"unlimited"`` (the default), which reads no
            usage at all. With a number, each finished task's tokens are
            read from its trace and added up; once the total reaches the
            limit, no further task starts (module docstring).
        benchmark_spec: How the selected benchmark was selected, recorded by
            the run of each task its task source gives (``None``: none is
            recorded). A branch task records its source run's instead.
        inherited: How to compose and judge a branch task, which belongs to
            the benchmark its source run did (``TaskContext.benchmark``),
            given that benchmark's record; called once per benchmark. ``None``:
            such a task cannot be composed, and fails as one whose stack
            could not be. *compose*, *verifiers*, *reference_loader* and
            *primary_rule* are the selected benchmark's, and serve every
            other task (a branch of a run that belonged to no benchmark too).
    """

    def __init__(
        self,
        *,
        loader: Any,
        compose: Callable[[], ComposedRun],
        agent_id: str | None = None,
        env_id: str | None = None,
        timeout: float = 300.0,
        verify_timeout: float = VERIFY_TIMEOUT_S,
        run_id_prefix: str = "",
        verifiers: Sequence[OfflineVerifier] = (),
        reference_loader: BaseEvalLoader | None = None,
        primary_rule: Benchmark | None = None,
        token_limit: int | Unlimited = UNLIMITED,
        progress: TaskProgress | None = None,
        session: SessionLedger | None = None,
        config_source: ConfigSource | None = None,
        benchmark_spec: BenchmarkSpec | None = None,
        inherited: Callable[[BenchmarkSpec], TaskBenchmark] | None = None,
    ) -> None:
        if token_limit != UNLIMITED and (
            isinstance(token_limit, bool) or not isinstance(token_limit, int) or token_limit < 1
        ):
            raise ValueError(
                f"token_limit must be a number of at least 1 or 'unlimited', not {token_limit!r}"
            )
        self._token_limit = limit_value(token_limit)
        self._loader = loader
        self._agent_id = agent_id
        self._env_id = env_id
        self._timeout = timeout
        self._verify_timeout = verify_timeout
        self._benchmark = getattr(loader, "benchmark_name", "")
        self._subset = getattr(loader, "subset", "")
        self._run_id_prefix = run_id_prefix
        self._benchmark_spec = benchmark_spec
        self._inherited = inherited
        own = TaskBenchmark(
            compose=compose,
            verifiers=verifiers,
            reference_loader=reference_loader,
            primary_rule=primary_rule,
        )
        # Each benchmark the sweep's tasks belong to, by the record a branch
        # task names (``None``: the selected one), with its judging pass. Built
        # before any of its tasks runs: a verifier that needs reference rows
        # its benchmark does not have, or one attached live, is refused now,
        # not hours later at the judging pass.
        self._task_benchmarks: dict[BenchmarkSpec | None, TaskBenchmark] = {None: own}
        self._verifications: dict[BenchmarkSpec | None, VerificationRunner | None] = {
            None: _verification_of(own)
        }
        self._progress = progress
        self._session = session
        self._config_source = config_source
        self._experiment_scope: ExperimentScope | None = None

    @property
    def benchmark(self) -> str:
        return self._benchmark

    @property
    def subset(self) -> str:
        return self._subset

    @property
    def token_limit(self) -> int | None:
        """The sweep's token limit; ``None`` is ``unlimited``."""
        return self._token_limit

    def select_task_ids(
        self,
        *,
        task_ids: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[str]:
        return select_task_ids(self._loader, task_ids=task_ids, limit=limit)

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

        With a token limit, each finished task's recorded tokens are added
        to the sweep's total; once it reaches the limit the sweep stops
        there (``token_limit_reached``). Tasks run one at a time, so the task
        that reached it ran to its end: the limit never cuts a task off.

        Raises:
            PreflightError: The pre-flight check refused the configuration.
        """
        selected = self.select_task_ids(task_ids=task_ids, limit=limit)
        if self._session is not None:
            self._session.start(selected)
        if self._progress is not None:
            self._progress.set_tasks(selected)
        try:
            async with ExperimentScope() as scope:
                self._experiment_scope = scope
                try:
                    summary = await self._run_selected(selected)
                finally:
                    self._experiment_scope = None
        except BaseException as exc:
            reason = (
                "configuration_refused"
                if isinstance(exc, PreflightError)
                else "cancelled"
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
                else "execution_failed"
            )
            self._finish_observers(reason, preserve_error=True)
            raise
        reason = (
            "vetoed"
            if summary.was_aborted
            else "token_limit"
            if summary.token_limit_reached is not None
            else "completed_with_failures"
            if summary.failed or summary.verification_errors
            else "completed"
        )
        self._finish_observers(reason)
        return summary

    def _finish_observers(self, reason: str, *, preserve_error: bool = False) -> None:
        for observer in (self._session, self._progress):
            if observer is not None:
                try:
                    observer.finish(reason)
                except Exception:
                    if not preserve_error:
                        raise
                    logger.exception("Could not close the batch observer after %s", reason)

    async def _run_selected(self, selected: list[str]) -> BenchmarkRunSummary:
        logger.info(
            "BenchmarkRunner: %s/%s — %d task(s)",
            self._benchmark,
            self._subset or "-",
            len(selected),
        )
        # Each pre-flight's task runs on the stack the pre-flight assembled,
        # so the sweep still composes exactly one stack per task.
        prepared = {task_id: self.preflight(task_id) for task_id in self._checked_first(selected)}
        outcomes: list[TaskOutcome] = []
        aborted: str | None = None
        reached: TokenLimitReached | None = None
        tokens = 0
        for task_id in selected:
            run = prepared.pop(task_id, None)
            for observer in (self._session, self._progress):
                if observer is not None:
                    observer.start_task(task_id)
            outcome = await self.run_task(task_id, run=run)
            outcomes.append(outcome)
            for observer in (self._session, self._progress):
                if observer is not None:
                    observer.finish_task(task_id, _outcome_dict(outcome))
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
            if self._token_limit is None:
                continue
            if outcome.usage is not None and outcome.usage.tokens is not None:
                tokens += outcome.usage.tokens
            logger.info(
                "BenchmarkRunner: %s recorded %s tokens; the sweep's total is %d of %d",
                task_id,
                outcome.usage.tokens if outcome.usage is not None else "uncounted",
                tokens,
                self._token_limit,
            )
            if tokens >= self._token_limit:
                reached = TokenLimitReached(
                    task_id=task_id,
                    tokens=tokens,
                    limit=self._token_limit,
                    not_started=len(selected) - len(outcomes),
                )
                logger.warning(
                    "BenchmarkRunner: the sweep's token total %d reached its limit of %d after "
                    "%s; %d task(s) not started",
                    tokens,
                    self._token_limit,
                    task_id,
                    reached.not_started,
                )
                break
        verification, verification_errors = await self._verify(outcomes)
        outcomes = [self._with_primary(outcome) for outcome in outcomes]
        return BenchmarkRunSummary(
            benchmark=self._benchmark,
            subset=self._subset,
            outcomes=outcomes,
            verification=verification,
            verification_errors=verification_errors,
            aborted=aborted,
            token_limit=self._token_limit,
            token_limit_reached=reached,
        )

    async def _verify(
        self, outcomes: Sequence[TaskOutcome]
    ) -> tuple[list[VerifierTally], list[str]]:
        """Judge the traces this sweep produced, each with the offline verifiers of its benchmark.

        Runs *after* every task (a vetoed sweep's and one stopped at its token
        limit included: only what ran is judged), over trace directories
        rather than live objects — so a judging crash cannot destroy a
        sweep's worth of compute, and the same traces can be judged again
        later without re-running anything. A branch task's trace is judged by
        its source's benchmark's verifiers (``TaskOutcome.inherited``); each
        benchmark's pass runs once, after the last task, its own failure
        failing only its tallies.
        """
        trace_dirs: dict[BenchmarkSpec | None, list[Path]] = {}
        for outcome in outcomes:
            if outcome.trace_dir is not None:
                trace_dirs.setdefault(outcome.inherited, []).append(outcome.trace_dir)
        counted: list[VerifierTally] = []
        errors: list[str] = []
        for key, verification in self._verifications.items():
            if verification is None:
                continue
            try:
                report = await verification.run(trace_dirs.get(key, []))
            except Exception as exc:
                logger.exception("BenchmarkRunner: the judging pass failed")
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            counted += tallies(report)
            errors += [
                f"{outcome.task_id} ({outcome.slug}): {outcome.error}" for outcome in report.errors
            ]
        return counted, errors

    def _with_primary(self, outcome: TaskOutcome) -> TaskOutcome:
        """*outcome* with its benchmark's primary verdict on its run and how far the task succeeded.

        The benchmark the task belongs to (``TaskOutcome.inherited``: a
        branch task's source's) states both; one this sweep never composed
        (its task did not load) states nothing.
        """
        benchmark = self._task_benchmarks.get(outcome.inherited)
        rule = benchmark.primary_rule if benchmark is not None else None
        if rule is None or rule.primary_verifier is None:
            return outcome
        outcome = dataclasses.replace(outcome, primary_verifier=rule.primary_verifier)
        if outcome.trace_dir is None:
            return outcome
        try:
            trace = RunTrace(outcome.trace_dir)
            verdicts = trace.verdicts()
        except TraceSchemaError as exc:
            return dataclasses.replace(outcome, primary_note=str(exc))
        primary = rule.primary_verdict(verdicts)
        if primary is None:
            return outcome
        result = outcome.result
        if result is None:
            # ``execute`` raised after the close-out (a blocking hook failing
            # in ``on_run_end``): how the run ended is still in its result.json.
            # One that does not read back leaves it unknown; the trace ledger
            # already reports why.
            try:
                result = trace.result
            except TraceSchemaError:
                result = None
        return dataclasses.replace(outcome, primary=primary, success=rule.success(primary, result))

    def preflight(self, task_id: str) -> ComposedRun:
        """Check the configuration once for *task_id*, before the sweep starts; return its stack.

        Composes the task's stack and runs ``AgentRuntime.prepare()`` on it
        with the task's loop inputs and environment setup context: slot
        validation, the Loop's inputs, the negotiations and the
        environment's setup check (``docs/architecture_design.md`` §4.2) —
        everything that would refuse every task of its benchmark alike. A
        branch task's stack is its source's benchmark's (``inherited``), whose
        judging pass is built here too. Nothing is set up, no hook is called
        and no trace is opened. The returned stack is the one this task then
        runs on (``run_task(run=...)``).

        A sweep with a token limit also needs a hook that writes a trace:
        the limit counts what each task's trace recorded, and without one
        it could count nothing. The check costs no token, and the limit
        itself never refuses a sweep up front (it is at least 1, so the
        first task always starts).

        Raises:
            PreflightError: Naming the task and the refusal, chained from it
                — including a stack that cannot be composed and a task
                context that cannot be read.
        """
        try:
            context = self._loader.get_task_context(task_id)
            run = self._benchmark_of(inherited_of(context)).compose()
            if self._experiment_scope is not None:
                self._experiment_scope.bind(run)
            self._attach_progress(run)
            agent_id = self._agent_id or run.ids("agent")[0]
            runtime = AgentRuntime.for_agent(run, agent_id, env_id=self._env_id)
            runtime.prepare(task_id=task_id, loop_inputs=context.loop, env_context=context.env)
            if self._token_limit is not None and not _records_usage(run):
                raise ValueError(
                    "a token limit counts the tokens each task's trace records, and this "
                    "configuration writes no trace with metrics: add a trace_writer hook "
                    "with recording.metrics enabled, or run without a token limit"
                )
        except Exception as exc:
            raise PreflightError(task_id, exc) from exc
        return run

    def _checked_first(self, selected: Sequence[str]) -> list[str]:
        """The tasks the pre-flight checks: the sweep's first, and the first of each other benchmark.

        A sweep's tasks belong to the selected benchmark, except a branch task,
        which belongs to its source run's (``TaskContext.benchmark``): a branch
        sweep over runs of several benchmarks runs under one configuration per
        benchmark, each checked once. A task whose context does not read is
        left to its own run to report, unless it is the first.
        """
        checked = list(selected[:1])
        seen: set[BenchmarkSpec | None] = set()
        for task_id in selected:
            try:
                key = inherited_of(self._loader.get_task_context(task_id))
            except Exception:
                continue
            if key not in seen:
                seen.add(key)
                if task_id not in checked:
                    checked.append(task_id)
        return checked

    def _benchmark_of(self, inherited: BenchmarkSpec | None) -> TaskBenchmark:
        """The benchmark a task belongs to: the selected one, or the one a branch task *inherited*.

        Raises:
            ValueError: This runner cannot compose a task of another benchmark
                (no ``inherited``), or that benchmark's verifiers cannot judge
                its traces (``VerificationRunner``'s refusals).
            Exception: Whatever composing that benchmark raised (its YAML no
                longer reads, its package or its data is not here).
        """
        if inherited is None or inherited in self._task_benchmarks:
            return self._task_benchmarks[inherited]
        if self._inherited is None:
            raise ValueError(
                f"the task is a branch of a run of the benchmark {inherited.benchmark_id!r} "
                f"({inherited.describe()}), and this sweep has no way to compose a task of "
                f"another benchmark"
            )
        benchmark = self._inherited(inherited)
        self._verifications[inherited] = _verification_of(benchmark)
        self._task_benchmarks[inherited] = benchmark
        return benchmark

    def _attach_progress(self, run: ComposedRun) -> None:
        if self._progress is not None:
            from spectragent.monitoring import attach_progress

            attach_progress(run, self._progress)

    async def run_task(self, task_id: str, *, run: ComposedRun | None = None) -> TaskOutcome:
        """Run one task, borrowing the sweep scope or owning a one-task scope."""
        if self._experiment_scope is not None:
            return await self._run_task(task_id, run=run, scope=self._experiment_scope)
        async with ExperimentScope() as scope:
            return await self._run_task(task_id, run=run, scope=scope)

    async def _run_task(
        self, task_id: str, *, run: ComposedRun | None, scope: ExperimentScope
    ) -> TaskOutcome:
        """Run one task end to end. Never raises — a failure is data.

        A benchmark sweep that dies on task 3 of 300 has destroyed the
        other 297 runs' worth of compute, so infrastructure failures are
        captured per task and recorded. A guard's veto (``RunAbort``) is
        recorded too — the outcome's ``vetoed`` — and :meth:`run` stops the
        sweep on it. Once the run is over, the trace's own ledger is read
        back from its manifest (``trace_status``), and so is the model call a
        run in error ended on, when it failed after the provider's retries.

        With a token limit, the tokens the task's trace recorded are read
        back too (``usage``).

        Args:
            task_id: The task.
            run: The stack to run it on — the one :meth:`preflight` assembled
                for it (the first task of its benchmark); composed fresh, as its
                benchmark's, when omitted.
        """
        started = time.monotonic()
        try:
            task = self._loader[task_id]
            context = self._loader.get_task_context(task_id)
        except Exception as exc:
            logger.exception("BenchmarkRunner: cannot load task %s", task_id)
            return TaskOutcome(
                task_id=task_id,
                error=f"task load failed: {type(exc).__name__}: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        inherited = inherited_of(context)
        try:
            run = run if run is not None else self._benchmark_of(inherited).compose()
            scope.bind(run)
            self._attach_progress(run)
            agent_id = self._agent_id or run.ids("agent")[0]
            runtime = AgentRuntime.for_agent(run, agent_id, env_id=self._env_id)
        except Exception as exc:
            logger.exception("BenchmarkRunner: compose failed for %s", task_id)
            return TaskOutcome(
                task_id=task_id,
                error=f"compose failed: {type(exc).__name__}: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                inherited=inherited,
            )

        # The link the framework never had: dataset setup → capability.
        # For SWE-bench this is the container image and checkout commit;
        # for a browser benchmark, the start URL. The task's context parts
        # each go to their own parameter: ``env`` to the capabilities' setup,
        # ``loop`` to the Loop (a branch task's checkpoint), ``branch`` to the
        # trace; a branch task's ``benchmark`` chose its stack above, and the
        # trace records it as the benchmark the run belongs to.
        try:
            result = await runtime.execute(
                task=getattr(task, "task_text", ""),
                task_id=task_id,
                benchmark=self._benchmark,
                subset=self._subset,
                run_id=safe_run_id(task_id, prefix=self._run_id_prefix),
                env_context=context.env,
                loop_inputs=context.loop,
                branch=context.branch,
                benchmark_spec=inherited if context.branch is not None else self._benchmark_spec,
                config_source=self._config_source,
                timeout=self._timeout,
                verify_timeout=self._verify_timeout,
                raise_on_error=False,
                config=task_snapshot(task, self._benchmark, self._subset),
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
        ledger = _trace_ledger(trace_dir, result, count_tokens=self._token_limit is not None)
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
            usage=ledger.usage,
            usage_note=ledger.usage_note,
            inherited=inherited,
        )


def select_task_ids(
    loader: Any, *, task_ids: Sequence[str] | None = None, limit: int | None = None
) -> list[str]:
    """*task_ids*, or every task of *loader*, the first *limit* of them."""
    selected = list(task_ids) if task_ids is not None else list(loader.task_ids())
    return selected[:limit] if limit is not None else selected


def inherited_of(context: TaskContext) -> BenchmarkSpec | None:
    """The benchmark a task inherited: a branch task's source's; ``None`` for every other task.

    A branch of a run that belonged to no benchmark inherits none either, and
    runs as the sweep's own task.
    """
    return context.benchmark if context.branch is not None else None


def _verification_of(benchmark: TaskBenchmark) -> VerificationRunner | None:
    """The judging pass of *benchmark*'s offline verifiers; ``None`` when it has none.

    Raises:
        ValueError: ``VerificationRunner``'s refusals (a verifier attached live,
            one needing reference rows the benchmark does not have).
    """
    if not benchmark.verifiers:
        return None
    return VerificationRunner(
        verifiers=benchmark.verifiers, reference_loader=benchmark.reference_loader
    )


def task_snapshot(task: Any, benchmark: str, subset: str) -> dict[str, Any]:
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


def _reference_loader(
    benchmark: Benchmark, verifiers: Sequence[OfflineVerifier], selection: str
) -> BaseEvalLoader | None:
    """The reference rows the sweep's verifiers judge against; ``None`` when none needs one.

    The benchmark's own (:meth:`BenchmarkBase.reference_loader`). A verifier
    that needs one on a benchmark with none stops the sweep before anything
    runs, naming it: a sweep must not run for hours to fail at its judging
    pass.
    """
    needing = [verifier.verifier_id for verifier in verifiers if "reference" in verifier.needs]
    if not needing:
        return None
    loader = benchmark.reference_loader()
    if loader is None:
        raise SystemExit(
            f"Verifier(s) {', '.join(needing)} of benchmark {selection!r} judge against the "
            f"task's reference row, and the benchmark has no reference data: drop them from its "
            f"verifiers, or pass --no-eval."
        )
    return loader


def compose_benchmark_alone(block: dict[str, Any]) -> Benchmark:
    """The benchmark *block* builds as a run root of its own: its task source, verifiers and rules.

    What gives a sweep its tasks. A branch sweep's own run root may not
    compose: its run configuration may leave to each task's source benchmark
    what that benchmark supplies (an AVD, an app table;
    ``docs/architecture_design.md`` §4.10.1 "分叉 sweep 是一种任务来源").
    """
    root = run_root({}, block)
    return ConfigComposer.for_config(root).compose(root).single("benchmark")


def inherited_benchmark(
    configuration: object,
    task_source: dict[str, Any],
    spec: BenchmarkSpec,
    *,
    evaluate: bool = True,
) -> TaskBenchmark:
    """The benchmark a branch task inherits, by its source run's *spec*, as a sweep composes and judges it.

    The task's run root is the run *configuration*, the branch sweep's
    *task_source* block and the source's benchmark block, read again from how
    the source selected it (``read_spec``): its setup component is hosted, its
    live verifiers judge at the scoring point and its part of the capability
    configuration is merged, as for the source run
    (``docs/architecture_design.md`` §4.10.1 "分叉 sweep 是一种任务来源").
    With *evaluate* (``spectragent bench`` without ``--no-eval``), its offline
    verifiers judge the branch's trace after the sweep, against its reference
    rows when they need them.

    Raises:
        BenchmarkSelectionError: The selection no longer reads, or names
            another benchmark.
        Exception: What composing the root raises (``ComposeError``, a task
            loader's data error).
    """
    root = run_root(configuration, task_source, read_spec(spec))
    composer = ConfigComposer.for_config(root)
    probe = composer.compose(root)
    benchmark = next(
        candidate
        for candidate in probe.instances("benchmark")
        if candidate.benchmark_id == spec.benchmark_id
    )
    verifiers = benchmark.offline_verifiers() if evaluate else []
    needs_reference = any("reference" in verifier.needs for verifier in verifiers)
    return TaskBenchmark(
        compose=lambda: composer.compose(root),
        verifiers=verifiers,
        reference_loader=benchmark.reference_loader() if needs_reference else None,
        primary_rule=benchmark,
    )


def _token_limit_argument(text: str) -> int | Unlimited:
    """``--token-limit``: a number of at least 1, or ``unlimited``."""
    if text == UNLIMITED:
        return UNLIMITED
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is neither a number nor '{UNLIMITED}'"
        ) from None
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"{value} is below 1: a token limit is a number of at least 1, or '{UNLIMITED}'"
        )
    return value


def positive_seconds(text: str) -> float:
    """``--timeout`` / ``--verify-timeout`` (here and in ``spectragent run``): seconds above 0.

    ``value > 0`` refuses NaN as well. A limit of 0, below 0 or NaN expires at
    once: every live verifier would come back not judged, and a verdict judged
    against the live environment cannot be had again once it is torn down.
    """
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not above 0: a time limit is seconds above 0"
        )
    return value


def main(
    argv: list[str] | None = None,
    *,
    progress: Any = None,
    execute: Callable[..., Any] = asyncio.run,
    report: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="spectragent bench",
        description="Run a benchmark's tasks through a composed agent stack.",
        epilog=(
            "Exit codes: 0 every task ran with no infrastructure failure; 1 some task had one; "
            "2 the pre-flight check refused the configuration (no task ran); 3 a guard vetoed "
            "the sweep; 4 the sweep reached its token limit (no task after it started)."
        ),
    )
    parser.add_argument("--config", required=True, type=Path, help="YAML run config.")
    parser.add_argument(
        "--config-source",
        type=Path,
        help="Canonical absolute original YAML path before generated overrides; requires --config-source-sha256.",
    )
    parser.add_argument(
        "--config-source-sha256",
        help="Digest captured from that original YAML's consumed bytes; requires --config-source.",
    )
    parser.add_argument(
        "--benchmark",
        required=True,
        help=(
            "The benchmark whose tasks to run: a shipped alias "
            "(configs/benchmarks/<alias>.yaml) or the path of a benchmark YAML, such as a "
            "branches YAML for a branch sweep. The run configuration never holds one."
        ),
    )
    parser.add_argument(
        "--subset",
        help=(
            "Which subset of the benchmark's tasks to run, written into its task loader before "
            "the run is composed, so every run's configuration and identity record it."
        ),
    )
    parser.add_argument("--task-id", action="append", dest="task_ids", help="Repeatable.")
    parser.add_argument("--limit", type=int, help="Run at most N tasks.")
    parser.add_argument("--timeout", type=positive_seconds, default=300.0, help="Per-task seconds.")
    parser.add_argument(
        "--verify-timeout",
        type=positive_seconds,
        default=VERIFY_TIMEOUT_S,
        help="Seconds each live verifier may take to judge a task after its run.",
    )
    parser.add_argument("--agent-id", help="Agent instance id to drive.")
    parser.add_argument("--env-id", help="Environment instance id to run against.")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--session-dir",
        type=Path,
        help="Batch plan/outcome directory; default: the trace root's .sessions directory.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the selected task ids and exit without running.",
    )
    parser.add_argument(
        "--no-eval",
        action="store_true",
        help=(
            "Record traces only; skip the pass of the benchmark's offline verifiers (each "
            "verdict of that pass goes into its run's verdicts/)."
        ),
    )
    parser.add_argument(
        "--token-limit",
        type=_token_limit_argument,
        default=UNLIMITED,
        help=(
            "The most tokens this sweep's runs may record, counted from the model calls each "
            "task's trace recorded and added up after each task: once the total reaches it, the "
            "task that reached it has finished, no task after it starts, and the exit code is 4. "
            "A call whose provider reported no usage adds nothing and is named in the summary. "
            "A number of at least 1, or 'unlimited' (the default): no limit, and no usage is read."
        ),
    )
    args = parser.parse_args(argv)
    if (args.config_source is None) != (args.config_source_sha256 is None):
        parser.error("--config-source and --config-source-sha256 must be supplied together")

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import yaml

    canonical = args.config.expanduser().resolve()
    source_bytes = canonical.read_bytes()
    configuration = yaml.safe_load(source_bytes)
    task_source = read_benchmark(args.benchmark, subset=args.subset)
    raw = run_root(configuration, task_source)
    config_source = (
        ConfigSource(path=str(args.config_source), sha256=args.config_source_sha256)
        if args.config_source is not None
        else ConfigSource.from_path(canonical, data=source_bytes)
    )
    composer = ConfigComposer.for_config(raw)
    chosen = compose_benchmark_alone(task_source)
    loader = chosen.task_loader

    verifiers = [] if args.no_eval else chosen.offline_verifiers()
    benchmark = loader.benchmark_name
    subset = getattr(loader, "subset", "")
    reference_loader = _reference_loader(chosen, verifiers, args.benchmark)
    # One per benchmark the branch tasks inherit, shared by the probe below
    # and the runner, which composes each task's stack from it.
    built: dict[BenchmarkSpec, TaskBenchmark] = {}

    def inherited(spec: BenchmarkSpec) -> TaskBenchmark:
        if spec not in built:
            built[spec] = inherited_benchmark(
                configuration, task_source, spec, evaluate=not args.no_eval
            )
        return built[spec]

    selected = select_task_ids(loader, task_ids=args.task_ids, limit=args.limit)

    from spectragent.core.benchmark_session import BatchSession

    # Where the traces go, for the session file beside them: the first task's
    # stack, as it runs (a branch task's is its source benchmark's). One that
    # does not compose is the pre-flight's refusal, made before it.
    try:
        spec = inherited_of(loader.get_task_context(selected[0])) if selected else None
        probe = inherited(spec).compose() if spec is not None else composer.compose(raw)
    except Exception as exc:
        if not selected:
            raise
        print(f"spectragent bench: {PreflightError(selected[0], exc)}", file=sys.stderr)
        return PREFLIGHT_EXIT_CODE
    trace_root = next(
        (Path(hook.trace_dir) for hook in probe.hooks if hasattr(hook, "trace_dir")),
        Path("traces"),
    )
    session = BatchSession(
        args.session_dir or trace_root / ".sessions",
        benchmark=benchmark,
        subset=subset,
        config_source=config_source,
    )
    if progress is not None:
        progress.configure(token_limit=args.token_limit)

    runner = BenchmarkRunner(
        loader=loader,
        compose=lambda: composer.compose(raw),
        agent_id=args.agent_id,
        env_id=args.env_id,
        timeout=args.timeout,
        verify_timeout=args.verify_timeout,
        verifiers=verifiers,
        reference_loader=reference_loader,
        primary_rule=chosen,
        token_limit=args.token_limit,
        progress=progress,
        session=session,
        config_source=config_source,
        benchmark_spec=spec_of(args.benchmark, task_source, subset=args.subset),
        inherited=inherited,
    )
    if args.list:
        for task_id in selected:
            print(task_id)
        if report is not None:
            report({"task_ids": selected, "launched": False})
        return 0

    try:
        summary = execute(runner.run(task_ids=selected))
    except PreflightError as exc:
        # Nothing ran and no trace exists: exit 2, never 0 or 1 (the codes of
        # a sweep that ran), so a script or the flywheel chain stops here.
        print(f"spectragent bench: {exc}", file=sys.stderr)
        return PREFLIGHT_EXIT_CODE
    if report is not None:
        report({"summary": summary.as_dict(), "session": str(session.path)})
    print(summary.format_table())
    print(f"session: {session.path}")
    if summary.was_aborted:
        # A guard said stop: its own code, so a script or the flywheel chain
        # stops here too instead of feeding the vetoed stage's runs onwards.
        print(f"spectragent bench: the sweep was vetoed at {summary.aborted}", file=sys.stderr)
        return VETO_EXIT_CODE
    reached = summary.token_limit_reached
    if reached is not None:
        # The budget is spent: its own code, ahead of 1, so a script or the
        # flywheel chain starts nothing more.
        print(
            f"spectragent bench: the sweep reached its token limit after task {reached.task_id} "
            f"({reached.tokens} of {reached.limit} tokens); {reached.not_started} task(s) not "
            f"started",
            file=sys.stderr,
        )
        return TOKEN_LIMIT_EXIT_CODE
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

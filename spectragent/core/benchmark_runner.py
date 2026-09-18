"""BenchmarkRunner — drive a whole benchmark, one task at a time.

This is the piece that turns "an agent ran once" into "a benchmark was
evaluated". Before it existed, ``AgentRuntime.execute`` took a free-text
task string and nothing in ``spectragent/`` ever called it: the only
drivers were demo scripts that hardcoded a task, never touched a
``TaskLoader``, and therefore produced traces with no dataset identity.
A trace you cannot join back to its ``EvalReference`` cannot be scored by
anything, which is what made "Record Once, Evaluate Many" false in
practice.

The runner owns exactly three things:

1. **Iteration** — which tasks, in which order, with which subset.
2. **Isolation** — a fresh :class:`ComposedRun` per task by default, so
   task N's browser, memory and hook state cannot leak into task N+1.
3. **Identity** — every run carries ``(benchmark, subset, task_id)`` and
   a stable ``run_id`` into the trace.

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
from typing import Any

from spectragent.core.composer import ComposedRun
from spectragent.core.loops.errors import RunAbort
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas._types import EnvironmentStatus

logger = logging.getLogger(__name__)

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


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


@dataclass(frozen=True)
class TaskOutcome:
    """What happened to one task."""

    task_id: str
    result: Any = None
    trace_dir: Path | None = None
    error: str | None = None
    elapsed_ms: int = 0

    @property
    def agent_done(self) -> str:
        done = getattr(self.result, "agent_done", None)
        return str(done) if done is not None else "not_started"

    @property
    def env_status(self) -> str:
        status = getattr(self.result, "env_status", None)
        return str(status) if status is not None else ""

    @property
    def infrastructure_error(self) -> str | None:
        """What stopped this task from being a real attempt, if anything.

        ``error`` alone is not that answer. The sweep calls
        ``execute(raise_on_error=False)``, so a browser that never
        connected comes back as an ordinary ``AgentResult`` — no
        exception, ``error=None`` — carrying ``env_status=failed`` and
        the real message inside ``result.error``. Reading only the
        outcome's own ``error`` reports 300 unstarted browsers as 300
        completed tasks and exits 0.

        ``env_status`` is the field that exists precisely to separate "the
        environment broke" from "the agent did badly", so it is the one
        consulted here: an agent that ran out of steps in a healthy
        browser is a completed attempt and stays ``ok``.
        """
        if self.error is not None:
            return self.error
        if self.result is not None and self.env_status == EnvironmentStatus.FAILED:
            return str(getattr(self.result, "error", None) or "environment failed")
        return None

    @property
    def ok(self) -> bool:
        """The task ran to a terminal state without infrastructure failure."""
        return self.result is not None and self.infrastructure_error is None


@dataclass(frozen=True)
class BenchmarkRunSummary:
    """Aggregate of one benchmark sweep.

    ``agent_done`` counts say how runs *terminated*. Whether an answer was
    correct stays an Evaluator's business and lives in ``eval_summary``,
    which is ``None`` unless the config declared an ``evaluator:`` layer —
    a sweep is perfectly valid without one.
    """

    benchmark: str
    subset: str
    outcomes: list[TaskOutcome] = field(default_factory=list)
    eval_summary: Any = None
    eval_errors: list[str] = field(default_factory=list)

    #: Set when a guard vetoed the sweep (``RunAbort``), naming the task that
    #: did it. ``outcomes`` then holds only the tasks that ran before it, so
    #: without this a short sweep is indistinguishable from a small one — and
    #: a reader would compare a 3-task partial against a 30-task full one.
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
        return sum(1 for o in self.outcomes if not o.ok)

    def count_by_done(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.agent_done] = counts.get(outcome.agent_done, 0) + 1
        return counts

    def format_table(self) -> str:
        lines = [
            f"benchmark={self.benchmark} subset={self.subset or '-'} "
            f"tasks={self.total} completed={self.completed} failed={self.failed}",
        ]
        for done, count in sorted(self.count_by_done().items()):
            lines.append(f"  agent_done={done}: {count}")
        for outcome in self.outcomes:
            where = outcome.trace_dir.name if outcome.trace_dir else "-"
            # ``infrastructure_error``, not ``error``: the message for a
            # browser that never started lives on the result, and printing
            # only the bare status left "Environment setup failed: ..."
            # unreachable from the table it belongs in.
            failure = outcome.infrastructure_error
            detail = f" error={failure}" if failure else ""
            lines.append(
                f"  {outcome.task_id}: {outcome.agent_done} "
                f"({outcome.elapsed_ms} ms, trace={where}){detail}"
            )
        if self.eval_summary is not None:
            from spectragent.evaluator.summary import format_summary

            lines.append("evaluation:")
            lines.extend(f"  {line}" for line in format_summary(self.eval_summary).splitlines())
        for message in self.eval_errors:
            lines.append(f"  eval error: {message}")
        return "\n".join(lines)


class BenchmarkRunner:
    """Run every task of a benchmark through a composed agent stack.

    Args:
        loader: A ``TaskLoader`` — the source of task ids, task text and
            per-task environment context.
        compose: Zero-arg factory returning a fresh :class:`ComposedRun`.
            Called once per task. Pass ``lambda: existing_run`` to reuse
            one stack across tasks; ``AgentRuntime.execute`` clears memory
            either way, but a fresh compose is the only thing that also
            resets capability and hook state.
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
        """Run the selected tasks sequentially and summarise."""
        selected = self.select_task_ids(task_ids=task_ids, limit=limit)
        logger.info(
            "BenchmarkRunner: %s/%s — %d task(s)",
            self._benchmark,
            self._subset or "-",
            len(selected),
        )
        outcomes: list[TaskOutcome] = []
        aborted: str | None = None
        for task_id in selected:
            try:
                outcomes.append(await self.run_task(task_id))
            except RunAbort as exc:
                # Stop the sweep, but keep what it produced. The guard said
                # "stop", not "destroy the evidence": the finished tasks have
                # complete traces, and under Record Once, Evaluate Many those
                # are still worth scoring. Losing them silently would make a
                # veto more destructive than a crash.
                aborted = f"{task_id}: {exc}"
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

    async def run_task(self, task_id: str) -> TaskOutcome:
        """Run one task end to end. Never raises — a failure is data.

        A benchmark sweep that dies on task 3 of 300 has destroyed the
        other 297 runs' worth of compute, so infrastructure failures are
        captured per task and recorded.
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
            run = self._compose()
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
        # for a browser benchmark, the start URL.
        env_context = self._loader.get_task_context(task_id)

        try:
            result = await runtime.execute(
                task=getattr(task, "task_text", ""),
                task_id=task_id,
                benchmark=self._benchmark,
                subset=self._subset,
                run_id=_safe_run_id(task_id, prefix=self._run_id_prefix),
                env_context=env_context,
                timeout=self._timeout,
                raise_on_error=False,
                config=_task_snapshot(task, self._benchmark, self._subset),
            )
            error = None
        except RunAbort:
            # First, and re-raised: this handler exists so one bad task does
            # not stop the sweep, and a veto is precisely the case where it
            # should. ``execute`` has already closed the trace out, so the
            # veto and its evidence survive; ``run`` catches it above and
            # summarises the tasks that did finish.
            logger.warning("BenchmarkRunner: task %s vetoed the sweep", task_id)
            raise
        except Exception as exc:  # execute already closed the trace out
            logger.exception("BenchmarkRunner: task %s failed", task_id)
            result = None
            error = f"{type(exc).__name__}: {exc}"

        return TaskOutcome(
            task_id=task_id,
            result=result,
            trace_dir=_trace_dir_of(run),
            error=error,
            elapsed_ms=int((time.monotonic() - started) * 1000),
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
    to construct it, and requiring it in YAML would be ceremony.
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

    loader_cls = EvalLoaderRegistrar.get(benchmark)
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

    summary = asyncio.run(runner.run(task_ids=selected))
    print(summary.format_table())
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

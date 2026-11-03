"""Run one configured agent task from the command line.

A task comes from one of two places (``docs/architecture_design.md``
§4.10.1 "单个任务照样简单"):

- a benchmark's task source: ``--benchmark <alias|path.yaml> --task-id <id>``
  gives the task's text and its three context parts (environment, Loop,
  branch), exactly as ``spectragent bench`` runs it;
- the command line: ``--task``, with the browser's ``--start-url`` and
  ``--replay-archive``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from spectragent.benchmarks.selection import read_benchmark, run_root
from spectragent.core.benchmark_runner import positive_seconds, safe_run_id, task_snapshot
from spectragent.core.composer import ConfigComposer
from spectragent.core.experiment import ExperimentScope
from spectragent.core.runtime import ORACLE_TIMEOUT_S, AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.config_source import ConfigSource
from spectragent.schemas.restoration import BranchProvenance
from spectragent.schemas.task import REPLAY_ARCHIVE_KEY


def task_context(
    *, start_url: str | None = None, replay_archive: str | None = None
) -> dict[str, str] | None:
    """The task context the flags name, routed to capabilities by key (§4.10); ``None`` if empty.

    ``replay_archive`` is the source run directory an ``archive`` network
    source replays, passed as given: ``AgentRuntime`` resolves a relative
    one against the working directory before ``prepare()`` checks it, the
    trace records it (``task_setup``) and setup opens it, for every caller
    alike.
    """
    context = {
        key: value
        for key, value in (("start_url", start_url), (REPLAY_ARCHIVE_KEY, replay_archive))
        if value
    }
    return context or None


def _trace_summary(path: str | None) -> dict[str, Any]:
    if path is None:
        return {"status": "not_recorded", "complete_through": None, "reason": None}
    try:
        manifest = RunTrace(Path(path)).manifest
        return {
            "status": str(manifest.status),
            "complete_through": manifest.complete_through,
            "reason": manifest.incomplete_reason,
        }
    except Exception as exc:
        return {"status": "unreadable", "complete_through": None, "reason": str(exc)}


@dataclass(frozen=True)
class _Task:
    """What one task hands the run: its text, its context parts and the identity it is recorded with.

    The identity defaults are ``AgentRuntime.execute``'s: a task written on the
    command line belongs to no benchmark.
    """

    text: str
    env: Mapping[str, Any] | None = None
    loop: Mapping[str, Any] | None = None
    branch: BranchProvenance | None = None
    task_id: str = "unknown"
    benchmark: str = ""
    subset: str = ""
    run_id: str | None = None
    config: dict[str, Any] | None = None


async def _run(
    **kwargs: Any,
) -> int:
    async with ExperimentScope() as scope:
        return await _run_in_experiment(scope=scope, **kwargs)


async def _run_in_experiment(
    *,
    scope: ExperimentScope,
    config_path: Path,
    task: str,
    timeout: float,
    dry_run: bool,
    start_url: str | None = None,
    replay_archive: str | None = None,
    benchmark: str | None = None,
    task_id: str | None = None,
    oracle_timeout: float = ORACLE_TIMEOUT_S,
    progress: Any = None,
    report: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    canonical = config_path.expanduser().resolve()
    source_bytes = canonical.read_bytes()
    raw = run_root(
        yaml.safe_load(source_bytes), *([read_benchmark(benchmark)] if benchmark else [])
    )
    context = task_context(start_url=start_url, replay_archive=replay_archive)
    config_source = ConfigSource.from_path(canonical, data=source_bytes)
    run = scope.bind(ConfigComposer.for_config(raw).compose(raw))
    if task_id is not None:
        loader = run.single("benchmark").task_loader
        definition = loader.get(task_id)
        if definition is None:
            raise ValueError(
                f"--task-id {task_id}: benchmark {benchmark!r} has no such task "
                f"({len(loader)} tasks; spectragent bench --benchmark {benchmark} --list lists them)"
            )
        parts = loader.get_task_context(task_id)
        selected = _Task(
            text=definition.task_text,
            env=parts.env,
            loop=parts.loop,
            branch=parts.branch,
            task_id=task_id,
            benchmark=loader.benchmark_name,
            subset=loader.subset,
            run_id=safe_run_id(task_id),
            config=task_snapshot(definition, loader.benchmark_name, loader.subset),
        )
    else:
        selected = _Task(text=task, env=context)
    label = selected.task_id
    if progress is not None:
        from spectragent.monitoring import attach_progress

        if not dry_run:
            progress.set_tasks([label])
            progress.start_task(label)
        attach_progress(run, progress)
    agent_id = run.ids("agent")[0]
    runtime = AgentRuntime.for_agent(run, agent_id)

    agent = runtime.agent
    env = runtime.env
    capabilities = ",".join(
        f"{cap.capability_id}:{getattr(cap.__dict__.get('backend'), 'provider_id', '-')}"
        for cap in run.instances("capability")
    )
    print(
        f"agent={agent.agent_id} loop={agent.agent_loop.loop_id} "
        f"model={getattr(agent.llm, 'model', '?')} "
        f"environment={getattr(env, 'env_type', '?')} "
        f"capabilities={capabilities or '-'}"
    )

    if dry_run:
        # The same assembly a run starts with, setup check included: with a
        # replay archive the replay's cooperation rows and its consistency
        # with the source run are checked too. Nothing is launched.
        runtime.prepare(
            task_id=selected.task_id, env_context=selected.env, loop_inputs=selected.loop
        )
        print("Configuration and agent/environment negotiation succeeded.")
        if report is not None:
            report({"checked": "configuration_and_prepare", "launched": False})
        if progress is not None:
            progress.finish("checked")
        return 0

    try:
        result = await runtime.execute(
            task=selected.text,
            timeout=timeout,
            oracle_timeout=oracle_timeout,
            task_id=selected.task_id,
            benchmark=selected.benchmark,
            subset=selected.subset,
            run_id=selected.run_id,
            env_context=selected.env,
            loop_inputs=selected.loop,
            branch=selected.branch,
            config_source=config_source,
            config=selected.config,
        )
    except BaseException as exc:
        if progress is not None:
            progress.finish("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
        raise
    print(f"agent_done={result.agent_done} env_status={result.env_status}")
    if result.answer:
        print(f"answer: {result.answer}")
    if result.error:
        print(f"error: {result.error}")
    trace_path = None
    for hook in run.hooks:
        trace_dir = getattr(hook, "run_dir", None)
        if trace_dir is not None:
            trace_path = str(trace_dir)
            print(f"trace: {trace_dir}")
            break
    trace = _trace_summary(trace_path)
    print(f"recording={trace['status']} complete_through={trace['complete_through']}")
    if trace["reason"]:
        print(f"recording detail: {trace['reason']}")
    print("evaluation=not_evaluated")
    if report is not None:
        report(
            {
                "result": result.model_dump(mode="json"),
                "trace_dir": trace_path,
                "recording": trace,
                "evaluation": "not_evaluated",
            }
        )
    if progress is not None:
        if trace_path is not None:
            progress.set_trace(trace_path)
        progress.finish_task(
            label,
            {
                "agent_done": str(result.agent_done),
                "env_status": str(result.env_status),
                "trace_dir": trace_path,
                "error": result.error,
                "trace_status": trace["status"],
            },
        )
        progress.finish(str(result.agent_done))
    return 1 if result.error or trace["status"] in ("incomplete", "writing", "unreadable") else 0


def main(
    argv: list[str] | None = None,
    *,
    progress: Any = None,
    execute: Callable[..., Any] = asyncio.run,
    report: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="spectragent run",
        description=(
            "Run one task with a composed agent and environment: a task of a benchmark "
            "(--benchmark and --task-id), or one written here (--task, --start-url)."
        ),
    )
    parser.add_argument("--config", required=True, type=Path, help="YAML run config")
    parser.add_argument(
        "--task", default="", help="Task text (required unless --dry-run or --task-id)"
    )
    parser.add_argument(
        "--start-url",
        help="Browser page to open before the agent starts: one complete http:// or https:// URL",
    )
    parser.add_argument(
        "--replay-archive",
        help="Recorded run directory to replay the browser's network from "
        "(needs network_source: archive)",
    )
    parser.add_argument(
        "--benchmark",
        help=(
            "The benchmark whose task --task-id runs: a shipped alias "
            "(configs/benchmarks/<alias>.yaml) or the path of a benchmark YAML"
        ),
    )
    parser.add_argument(
        "--task-id",
        help="Run this task of --benchmark: its text and where it starts come from the benchmark",
    )
    parser.add_argument(
        "--timeout", type=positive_seconds, default=300.0, help="Run timeout in seconds"
    )
    parser.add_argument(
        "--oracle-timeout",
        type=positive_seconds,
        default=ORACLE_TIMEOUT_S,
        help="Seconds each task oracle may take to score the task after the run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose, negotiate and check what setup would refuse; launch nothing",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    start = [
        flag
        for flag, value in (
            ("--start-url", args.start_url),
            ("--replay-archive", args.replay_archive),
        )
        if value
    ]
    if (args.benchmark is None) != (args.task_id is None):
        parser.error("--benchmark and --task-id go together: they select one task of a benchmark")
    if args.task_id is not None:
        given = [*(["--task"] if args.task else []), *start]
        if given:
            parser.error(
                f"{', '.join(given)}: a benchmark task's text and start come from its benchmark; "
                f"leave them out with --task-id"
            )
    elif not args.dry_run and not args.task:
        parser.error("--task is required unless --dry-run or --task-id is set")

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    return execute(
        _run(
            config_path=args.config,
            task=args.task,
            start_url=args.start_url,
            replay_archive=args.replay_archive,
            benchmark=args.benchmark,
            task_id=args.task_id,
            timeout=args.timeout,
            oracle_timeout=args.oracle_timeout,
            dry_run=args.dry_run,
            progress=progress,
            report=report,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

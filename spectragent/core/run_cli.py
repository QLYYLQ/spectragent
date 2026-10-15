"""Run one configured agent task from the command line."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from spectragent.core.composer import ConfigComposer
from spectragent.core.experiment import ExperimentScope
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.config_source import ConfigSource
from spectragent.schemas.task import REPLAY_ARCHIVE_KEY
from spectragent.scripts.generate_all import _collect_registrars


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
    start_url: str | None,
    timeout: float,
    dry_run: bool,
    replay_archive: str | None = None,
    progress: Any = None,
    report: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    canonical = config_path.expanduser().resolve()
    source_bytes = canonical.read_bytes()
    raw = yaml.safe_load(source_bytes)
    config_source = ConfigSource.from_path(canonical, data=source_bytes)
    composer = ConfigComposer(registrars=_collect_registrars())
    run = scope.bind(composer.compose(raw))
    if progress is not None:
        from spectragent.monitoring import attach_progress

        if not dry_run:
            progress.set_tasks(["unknown"])
            progress.start_task("unknown")
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

    context = task_context(start_url=start_url, replay_archive=replay_archive)
    if dry_run:
        # The same assembly a run starts with, setup check included: with
        # --replay-archive the replay's cooperation rows and its consistency
        # with the source run are checked too. Nothing is launched.
        runtime.prepare(env_context=context)
        print("Configuration and agent/environment negotiation succeeded.")
        if report is not None:
            report({"checked": "configuration_and_prepare", "launched": False})
        if progress is not None:
            progress.finish("checked")
        return 0

    try:
        result = await runtime.execute(
            task=task, timeout=timeout, env_context=context, config_source=config_source
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
            "unknown",
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
        description="Run one task with a composed agent and environment.",
    )
    parser.add_argument("--config", required=True, type=Path, help="YAML run config")
    parser.add_argument("--task", default="", help="Task text (required unless --dry-run)")
    parser.add_argument(
        "--start-url",
        help="Browser page to open before the agent starts: one complete http:// or https:// URL",
    )
    parser.add_argument(
        "--replay-archive",
        help="Recorded run directory to replay the browser's network from "
        "(needs network_source: archive)",
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="Run timeout in seconds")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose, negotiate and check what setup would refuse; launch nothing",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    if not args.dry_run and not args.task:
        parser.error("--task is required unless --dry-run is set")

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
            timeout=args.timeout,
            dry_run=args.dry_run,
            replay_archive=args.replay_archive,
            progress=progress,
            report=report,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

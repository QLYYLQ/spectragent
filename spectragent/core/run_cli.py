"""Run one configured agent task from the command line."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from spectragent.core.composer import ConfigComposer
from spectragent.core.runtime import AgentRuntime
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


async def _run(
    *,
    config_path: Path,
    task: str,
    start_url: str | None,
    timeout: float,
    dry_run: bool,
    replay_archive: str | None = None,
) -> int:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    composer = ConfigComposer(registrars=_collect_registrars())
    run = composer.compose(raw)
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
        return 0

    result = await runtime.execute(task=task, timeout=timeout, env_context=context)
    print(f"agent_done={result.agent_done} env_status={result.env_status}")
    if result.answer:
        print(f"answer: {result.answer}")
    if result.error:
        print(f"error: {result.error}")
    for hook in run.hooks:
        trace_dir = getattr(hook, "run_dir", None)
        if trace_dir is not None:
            print(f"trace: {trace_dir}")
            break
    return 1 if result.error else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectragent-run",
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
    return asyncio.run(
        _run(
            config_path=args.config,
            task=args.task,
            start_url=args.start_url,
            timeout=args.timeout,
            dry_run=args.dry_run,
            replay_archive=args.replay_archive,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

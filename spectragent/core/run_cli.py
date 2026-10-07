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
from spectragent.scripts.generate_all import _collect_registrars


async def _run(
    *,
    config_path: Path,
    task: str,
    start_url: str | None,
    timeout: float,
    dry_run: bool,
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

    if dry_run:
        runtime.prepare()
        print("Configuration and agent/environment negotiation succeeded.")
        return 0

    result = await runtime.execute(
        task=task,
        timeout=timeout,
        env_context={"start_url": start_url} if start_url else None,
    )
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
    parser.add_argument("--start-url", help="Browser page to open before the agent starts")
    parser.add_argument("--timeout", type=float, default=300.0, help="Run timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Compose and negotiate only")
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
        )
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

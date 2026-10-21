"""``python -m lookahead <command>``: the offline lookahead-data pipeline over recorded runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from lookahead.judge import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    ChatTransport,
    Judge,
    JudgeConfigurationError,
    JudgeRouteError,
    OpenAITransport,
)
from lookahead.queues import default_queue_id
from lookahead.runs import LookaheadInputError, RunSet, open_runs
from lookahead.stability import DEFAULT_MIN_CHANGE_AREA

TransportFactory = Callable[[], ChatTransport]


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root", type=Path, required=True, help="Directory holding the recorded runs."
    )
    parser.add_argument(
        "--viewer-root",
        type=Path,
        default=None,
        help="Root the trace viewer will be started with; run keys are relative to it "
        "(default: --root).",
    )


def _queue(parser: argparse.ArgumentParser, prefix: str) -> None:
    parser.add_argument(
        "--annotations",
        type=Path,
        default=None,
        help="Annotation directory the viewer reads; the queue goes to its queues/ "
        "(default: --out).",
    )
    parser.add_argument(
        "--queue-id", default=None, help=f"Queue id (default: {prefix}-<today's date>)."
    )


def _judge_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Judge model id.")
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
        help="reasoning_effort sent with every request.",
    )
    parser.add_argument(
        "--max-completion-tokens",
        type=int,
        default=DEFAULT_MAX_COMPLETION_TOKENS,
        help=f"max_completion_tokens of every request (default {DEFAULT_MAX_COMPLETION_TOKENS}).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Requests in flight at once (default 4)."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lookahead",
        description="Build lookahead-observation data and review queues from recorded runs.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    extract = commands.add_parser("extract", help="One JSON line per step that has an observation.")
    _common(extract)
    extract.add_argument("--out", type=Path, required=True, help="JSON-lines file to write.")

    stability = commands.add_parser(
        "stability", help="Whether each observed screen stayed the same while the agent thought."
    )
    _common(stability)
    stability.add_argument("--out", type=Path, required=True, help="Output directory.")
    _queue(stability, "stability")
    stability.add_argument(
        "--min-change-area",
        type=float,
        default=DEFAULT_MIN_CHANGE_AREA,
        help="Share of the image one changed region must cover for a frame to count as "
        "changed (default 0.005, that is 0.5%%).",
    )
    stability.add_argument(
        "--origins",
        default="policy",
        help="Comma-separated step origins to include: policy, restored (default: policy; "
        "a restored step has no think time).",
    )
    stability.add_argument(
        "--queue-size", type=int, default=60, help="Items in the review queue (default 60)."
    )
    stability.add_argument("--seed", type=int, default=0, help="Sampling seed (default 0).")

    variants = commands.add_parser(
        "variants", help="Ask the judge model for alternative tasks starting from recorded screens."
    )
    _common(variants)
    variants.add_argument(
        "--steps-per-run", type=int, required=True, help="Checkpoints chosen per recorded run."
    )
    variants.add_argument(
        "--per-step", type=int, required=True, help="Alternative tasks asked for per checkpoint."
    )
    variants.add_argument(
        "--out", type=Path, required=True, help="task-variants-1 JSON file to write."
    )
    _judge_options(variants)

    forks = commands.add_parser(
        "judge-forks", help="Judge every branch's decision at its checkpoint, by rules and model."
    )
    _common(forks)
    forks.add_argument("--out", type=Path, required=True, help="Output directory.")
    _queue(forks, "fork-judge")
    _judge_options(forks)
    forks.add_argument(
        "--rules-only",
        action="store_true",
        help="Apply the rules only; ask no model and write no queue.",
    )

    gold = commands.add_parser(
        "gold-queue", help="A review queue for human labels of where the needed information is."
    )
    _common(gold)
    gold.add_argument("--n", type=int, default=100, help="Items in the queue (default 100).")
    gold.add_argument("--out", type=Path, required=True, help="Output directory.")
    _queue(gold, "gold")
    gold.add_argument("--seed", type=int, default=0, help="Sampling seed (default 0).")
    return parser


def _print_skipped(runs: RunSet) -> None:
    for key, why in sorted(runs.skipped.items()):
        print(f"skipped {key}: {why}", file=sys.stderr)


async def _with_judge(
    args: argparse.Namespace,
    transport_factory: TransportFactory,
    log_path: Path,
    work: Callable[[Judge], Any],
) -> tuple[Any, Judge]:
    transport = transport_factory()
    judge = Judge(
        transport,
        model=args.model,
        reasoning_effort=args.reasoning_effort or None,
        max_completion_tokens=args.max_completion_tokens,
        concurrency=args.concurrency,
        log_path=log_path,
    )
    try:
        return await work(judge), judge
    finally:
        await transport.close()


def run(
    argv: Sequence[str] | None = None, *, transport_factory: TransportFactory | None = None
) -> int:
    """Run one command; the exit status. *transport_factory* replaces the gateway (tests)."""
    args = build_parser().parse_args(argv)
    factory = transport_factory or OpenAITransport.from_environment
    try:
        runs = open_runs(args.root, args.viewer_root)
    except LookaheadInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        code = _dispatch(args, runs, factory)
    except (LookaheadInputError, JudgeConfigurationError, JudgeRouteError, ValueError) as exc:
        _print_skipped(runs)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_skipped(runs)
    return code


def _dispatch(args: argparse.Namespace, runs: RunSet, factory: TransportFactory) -> int:
    if args.command == "extract":
        from lookahead.extract import write_rows

        count = write_rows(runs, args.out)
        print(f"{count} row(s) from {len(runs.runs)} run(s) written to {args.out}")
        return 0

    if args.command == "stability":
        from lookahead.stability import run_stability

        report, queue = run_stability(
            runs,
            args.out,
            annotations=args.annotations or args.out,
            queue_id=args.queue_id or default_queue_id("stability"),
            queue_size=args.queue_size,
            seed=args.seed,
            min_change_area=args.min_change_area,
            origins=frozenset(
                origin.strip() for origin in args.origins.split(",") if origin.strip()
            ),
        )
        print(f"{len(report.observations)} observation(s): {json.dumps(report.summary)}")
        print(f"report: {args.out / 'report.md'}; queue: {queue}")
        return 0

    if args.command == "variants":
        from lookahead.variants import calls_path, generate

        summary, judge = asyncio.run(
            _with_judge(
                args,
                factory,
                calls_path(args.out),
                lambda judge: generate(
                    runs,
                    judge,
                    args.out,
                    steps_per_run=args.steps_per_run,
                    per_step=args.per_step,
                ),
            )
        )
        print(
            f"{summary['variants']} variant(s) from {summary['checkpoints']} checkpoint(s) "
            f"written to {args.out}"
        )
        print(judge.totals.summary())
        return 0

    if args.command == "judge-forks":
        from lookahead.forks import judge_forks

        annotations = args.annotations or args.out
        queue_id = args.queue_id or default_queue_id("fork-judge")
        if args.rules_only:
            records, _ = asyncio.run(
                judge_forks(runs, None, args.out, annotations=annotations, queue_id=queue_id)
            )
            print(f"{len(records)} fork(s) judged by rules; report: {args.out / 'report.md'}")
            return 0
        (records, queue), judge = asyncio.run(
            _with_judge(
                args,
                factory,
                args.out / "calls.jsonl",
                lambda judge: judge_forks(
                    runs, judge, args.out, annotations=annotations, queue_id=queue_id
                ),
            )
        )
        print(f"{len(records)} fork(s); report: {args.out / 'report.md'}; queue: {queue}")
        print(judge.totals.summary())
        return 0

    if args.command == "gold-queue":
        from lookahead.gold import gold_queue

        summary, queue = gold_queue(
            runs,
            args.out,
            annotations=args.annotations or args.out,
            queue_id=args.queue_id or default_queue_id("gold"),
            count=args.n,
            seed=args.seed,
        )
        print(f"{summary['items']} item(s) of {summary['candidates']} candidate(s); queue: {queue}")
        return 0

    raise AssertionError(f"unhandled command {args.command!r}")


def main() -> None:
    sys.exit(run())

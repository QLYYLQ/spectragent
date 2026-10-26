"""``python -m lookahead <command>``: the offline lookahead-data pipeline over recorded runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from lookahead.judge import Judge, JudgeModel, JudgeRouteError, load_judge_model
from lookahead.queues import default_queue_id
from lookahead.runs import LookaheadInputError, RunSet, open_runs
from lookahead.stability import DEFAULT_MIN_CHANGE_AREA
from spectragent.core.llm.lifecycle import model_session

JudgeLoader = Callable[[Path], JudgeModel]


def _common(parser: argparse.ArgumentParser, *, exclusions: bool = True) -> None:
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
    if exclusions:
        parser.add_argument(
            "--exclude-runs",
            type=Path,
            default=None,
            help="A run-exclusions file (run-exclusions-1, as health writes it): the runs it "
            "lists and the branch runs from them are left out.",
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


JUDGE_CONFIG_HELP = (
    "YAML file holding one llm layer: the judge model (e.g. configs/lookahead/judge_deepseek.yaml)."
)


def _concurrency(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Requests in flight at once (default 4)."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lookahead",
        description="Build lookahead-observation data and review queues from recorded runs.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    health = commands.add_parser(
        "health",
        help="Whether each recording is usable as data; writes the exclusions file of the others.",
    )
    _common(health, exclusions=False)
    health.add_argument("--out", type=Path, required=True, help="Output directory.")

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
    variants.add_argument("--judge-config", type=Path, required=True, help=JUDGE_CONFIG_HELP)
    _concurrency(variants)

    forks = commands.add_parser(
        "judge-forks", help="Judge every branch's decision at its checkpoint, by rules and model."
    )
    _common(forks)
    forks.add_argument("--out", type=Path, required=True, help="Output directory.")
    _queue(forks, "fork-judge")
    _concurrency(forks)
    # Exactly one: ask the model, apply the rules only, or reuse earlier answers.
    reuse = forks.add_mutually_exclusive_group(required=True)
    reuse.add_argument("--judge-config", type=Path, default=None, help=JUDGE_CONFIG_HELP)
    reuse.add_argument(
        "--rules-only",
        action="store_true",
        help="Apply the rules only; ask no model and write no queue.",
    )
    reuse.add_argument(
        "--answers-from",
        type=Path,
        default=None,
        help="judgments.jsonl of an earlier judge-forks run: reuse its model answers and write "
        "the queue with them, asking no model.",
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
    for key, why in sorted(runs.excluded.items()):
        print(f"excluded {key}: {why}", file=sys.stderr)
    for key, why in sorted(runs.skipped.items()):
        print(f"skipped {key}: {why}", file=sys.stderr)


async def _with_judge(
    args: argparse.Namespace,
    judge_model: JudgeModel | None,
    log_path: Path,
    work: Callable[[Judge], Any],
) -> tuple[Any, Judge]:
    assert judge_model is not None, "argparse requires --judge-config for this command"
    async with model_session(judge_model.llm):
        judge = Judge(judge_model, concurrency=args.concurrency, log_path=log_path)
        return await work(judge), judge


def run(argv: Sequence[str] | None = None, *, load_judge: JudgeLoader = load_judge_model) -> int:
    """Run one command; the exit status. *load_judge* replaces the model's composition (tests)."""
    args = build_parser().parse_args(argv)
    judge_config: Path | None = getattr(args, "judge_config", None)
    try:
        # Before the runs are read, so a bad file fails at once. Composing opens
        # no connection; the client opens at the first request, inside the
        # model session that releases it.
        judge_model = load_judge(judge_config) if judge_config is not None else None
        runs = open_runs(args.root, args.viewer_root, getattr(args, "exclude_runs", None))
    except LookaheadInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        code = _dispatch(args, runs, judge_model)
    except (LookaheadInputError, JudgeRouteError, ValueError) as exc:
        _print_skipped(runs)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_skipped(runs)
    return code


def _dispatch(args: argparse.Namespace, runs: RunSet, judge_model: JudgeModel | None) -> int:
    if args.command == "health":
        from lookahead.health import EXCLUSIONS_NAME, run_health

        report = run_health(runs, args.out)
        for health in sorted(report.runs, key=lambda health: health.run):
            why = "; ".join(reason.code for reason in health.reasons)
            print(f"{health.verdict:8} {health.run}" + (f" ({why})" if why else ""))
        print(
            f"{report.counts['unusable']} of {len(report.runs)} recording(s) unusable; "
            f"report: {args.out / 'report.md'}; exclusions: {args.out / EXCLUSIONS_NAME}"
        )
        return 0

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
                judge_model,
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
        if args.answers_from is not None:
            from lookahead.forks import earlier_answers

            records, queue = asyncio.run(
                judge_forks(
                    runs,
                    None,
                    args.out,
                    annotations=annotations,
                    queue_id=queue_id,
                    earlier=earlier_answers(args.answers_from),
                )
            )
            reused = sum(1 for record in records if record["answers_from"])
            print(
                f"{len(records)} fork(s); {reused} with model answers reused from "
                f"{args.answers_from}; report: {args.out / 'report.md'}; queue: {queue}"
            )
            return 0
        (records, queue), judge = asyncio.run(
            _with_judge(
                args,
                judge_model,
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

"""``spectragent-flywheel``: run the metrics chain, or recompute its report (arch §4.16).

::

    spectragent-flywheel run --spec chain.yaml [--stages record,same_path,report]
    spectragent-flywheel report --spec chain.yaml [--out DIR]
    spectragent-flywheel report --root traces/chains/first-live --evidence live|rehearsal [--out DIR]

``run`` runs the spec's stages in order (``chain.py``); ``report`` reads
the chain root only — committed runs and published datasets — and can be
repeated on any machine: the same inputs give the same bytes. With
``--root`` there is no spec, so no planted credential is searched for (the
credential-shape scan still runs). Neither loads ``.env``: export keys in
the shell first (``set -a; . ./.env; set +a``).

Exit codes: 0 done; 1 a stage failed — for ``report``, the report could not be
computed (a published dataset does not read back as this build's format) or
written (``--out`` names a directory that cannot be created or written), and
the message names which; 2 the spec or the arguments are wrong; 4 (``run``)
the chain stopped at its token limit: no later run or export stage started,
and the report, when selected, was still computed from what exists — the
same code ``spectragent-bench`` exits with when its sweep reaches its limit.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from spectragent.flywheel.chain import (
    ChainConfigError,
    ChainSpec,
    ChainStageError,
    build_spec_report,
    run_chain,
)
from spectragent.flywheel.metrics import ChainLayout, DatasetFormatError
from spectragent.flywheel.report import FlywheelReport, build_report, write_report

#: ``spectragent-flywheel run``'s exit code when the chain stopped at its token limit.
TOKEN_LIMIT_EXIT_CODE = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spectragent-flywheel",
        description="The data flywheel's metrics gate (docs/architecture_design.md §4.16).",
        epilog=(
            "Exit codes: 0 done; 1 a stage failed, or the report could not be computed or "
            "written; 2 the spec or the arguments are wrong; 4 the chain stopped at its token "
            "limit (the report, when selected, still computed from what exists)."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run the chain's stages, then the report.")
    run.add_argument("--spec", required=True, type=Path, help="The chain spec YAML.")
    run.add_argument(
        "--stages",
        help="Comma-separated stages to run (in chain order); default: every configured one.",
    )
    report = commands.add_parser("report", help="Compute the report from a chain root.")
    source = report.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", type=Path, help="The chain spec YAML (root, evidence, planted).")
    source.add_argument("--root", type=Path, help="A chain root, without a spec.")
    report.add_argument(
        "--evidence", choices=("live", "rehearsal"), help="The evidence basis (with --root)."
    )
    report.add_argument("--name", help="The chain's name in the report (with --root).")
    report.add_argument(
        "--out", type=Path, help="Where to write the report (default <root>/report)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "run":
            spec = ChainSpec.load(args.spec)
            stages = (
                [s.strip() for s in args.stages.split(",") if s.strip()] if args.stages else None
            )
            result = run_chain(spec, stages=stages, log_level=args.log_level)
            for stage, outcome in result.stages.items():
                tokens = (
                    f", {outcome.token_use.tokens} tokens" if outcome.token_use is not None else ""
                )
                print(
                    f"{stage}: exit {outcome.exit_codes or '-'}, {len(outcome.runs)} run(s){tokens}"
                )
            if result.report is not None:
                print(f"report: {result.layout.report_dir}")
            if result.token_limit_stop is not None:
                print(
                    f"spectragent-flywheel: stopped at the token limit: "
                    f"{result.token_limit_stop.describe()}",
                    file=sys.stderr,
                )
                return TOKEN_LIMIT_EXIT_CODE
            return 0
        if args.spec is not None:
            if args.evidence is not None or args.name is not None:
                parser.error("--evidence and --name come from the spec; give them only with --root")
            spec = ChainSpec.load(args.spec)
            layout = ChainLayout(spec.root.expanduser().resolve())
            report = _build(lambda: build_spec_report(spec))
        else:
            if args.evidence is None:
                parser.error("--root needs --evidence live|rehearsal")
            layout = ChainLayout(args.root.expanduser().resolve())
            report = _build(lambda: build_report(layout, evidence=args.evidence, name=args.name))
        out = args.out or layout.report_dir
        try:
            json_path, md_path = write_report(report, out)
        except OSError as exc:
            raise ChainStageError("report", f"cannot write the report to {out}: {exc}") from exc
        print(f"report: {json_path}\nreport: {md_path}")
        return 0
    except ChainConfigError as exc:
        print(f"spectragent-flywheel: {exc}", file=sys.stderr)
        return 2
    except ChainStageError as exc:
        print(f"spectragent-flywheel: {exc}", file=sys.stderr)
        return 1


def _build(compute: Callable[[], FlywheelReport]) -> FlywheelReport:
    """The report, or the report stage's failure naming the dataset that does not read back."""
    try:
        return compute()
    except DatasetFormatError as exc:
        raise ChainStageError("report", str(exc)) from exc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

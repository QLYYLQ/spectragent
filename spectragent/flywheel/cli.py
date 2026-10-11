"""``spectragent-flywheel``: run the metrics chain, or recompute its report (arch §4.16).

::

    spectragent-flywheel run --spec chain.yaml [--stages record,same_path,report]
    spectragent-flywheel report --spec chain.yaml [--out DIR]
    spectragent-flywheel report --root runs/m33 --evidence live|rehearsal [--out DIR]

``run`` runs the spec's stages in order (``chain.py``); ``report`` reads
the chain root only — committed runs and published datasets — and can be
repeated on any machine: the same inputs give the same bytes. With
``--root`` there is no spec, so no planted credential is searched for (the
credential-shape scan still runs). Neither loads ``.env``: export keys in
the shell first (``set -a; . ./.env; set +a``).

Exit codes: 0 done; 1 a stage failed; 2 the spec or the arguments are wrong.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from spectragent.flywheel.chain import (
    ChainConfigError,
    ChainSpec,
    ChainStageError,
    resolve_planted,
    run_chain,
)
from spectragent.flywheel.metrics import ChainLayout
from spectragent.flywheel.report import build_report, write_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spectragent-flywheel",
        description="The data flywheel's metrics gate (docs/architecture_design.md §4.16).",
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
                print(f"{stage}: exit {outcome.exit_codes or '-'}, {len(outcome.runs)} run(s)")
            if result.report is not None:
                print(f"report: {result.layout.report_dir}")
            return 0
        if args.spec is not None:
            if args.evidence is not None or args.name is not None:
                parser.error("--evidence and --name come from the spec; give them only with --root")
            spec = ChainSpec.load(args.spec)
            layout = ChainLayout(spec.root.expanduser().resolve())
            report = build_report(
                layout,
                evidence=spec.evidence,
                planted=resolve_planted(spec.report.planted),
                name=spec.name,
            )
        else:
            if args.evidence is None:
                parser.error("--root needs --evidence live|rehearsal")
            layout = ChainLayout(args.root.expanduser().resolve())
            report = build_report(layout, evidence=args.evidence, name=args.name)
        json_path, md_path = write_report(report, args.out or layout.report_dir)
        print(f"report: {json_path}\nreport: {md_path}")
        return 0
    except ChainConfigError as exc:
        print(f"spectragent-flywheel: {exc}", file=sys.stderr)
        return 2
    except ChainStageError as exc:
        print(f"spectragent-flywheel: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

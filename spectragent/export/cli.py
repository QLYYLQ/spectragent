"""``spectragent export --config <yaml>``: export recorded runs as a training dataset (arch §4.15).

The YAML is an ordinary run-root document holding one ``exporter:`` block
(``configs/flywheel/export_sft.yaml``) and, optionally, the ``eval_loader:`` /
``task_loader:`` blocks decontamination and the leak scan read benchmark data
through (``benchmarks.py``), composed by the same
``ConfigComposer(registrars=collect_registrars())`` as every other entry
point; it must yield exactly one exporter. Prints, per split, how many samples
were written and, per reason, how many steps were not, and — under
``wait_actions: drop_when_capped`` — how many thought rewrites came from the
cache and how many from a model call; exits non-zero on any failure — in
which case nothing was written.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from spectragent.core.composer import ConfigComposer
from spectragent.core.registries import collect_registrars
from spectragent.export.errors import ExportConfigError
from spectragent.export.runner import ExportReport, ExportRunner

logger = logging.getLogger(__name__)


def export(config_path: Path) -> ExportReport:
    """Compose *config_path*, run its one exporter, and return what was written.

    Raises:
        ExportConfigError: The YAML is not a mapping, or holds no exporter or
            more than one.
        ExportError: Whatever the export refused (nothing was written).
    """
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ExportConfigError(f"{config_path} is not a YAML mapping")
    composer = ConfigComposer(registrars=collect_registrars())
    run = composer.compose(raw)
    return ExportRunner.from_run(run, composer=composer, origin=str(config_path)).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectragent export",
        description=(
            "Export recorded runs as a training dataset rendered by a student agent "
            "(docs/architecture_design.md §4.15)."
        ),
    )
    parser.add_argument("--config", required=True, type=Path, help="YAML with one exporter block")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        report = export(args.config)
    except Exception as exc:  # every failure is reported, none leaves a dataset behind
        logger.debug("export failed", exc_info=True)
        print(f"export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"dataset: {report.output_dir}")
    for split, count in report.samples.items():
        print(f"  {split}: {count} sample(s)")
    for reason, count in report.drops.items():
        print(f"  not exported ({reason}): {count} step(s)")
    filtered = report.manifest.steps.filtered
    if filtered:
        print(f"  filtered out (each in manifest.json steps.filtered): {len(filtered)} step(s)")
    if report.thought_rewrites:
        print(
            f"  thought rewrites: {report.thought_rewrites['cache_hits']} from the cache, "
            f"{report.thought_rewrites['model_calls']} model call(s)"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

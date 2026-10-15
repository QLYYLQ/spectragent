"""Shared pieces of the training-export tests: student configs, one export, JSON lines.

An export is composed exactly as ``spectragent export`` composes it — one
``exporter:`` entry through the real ``ConfigComposer`` — and run by the real
``ExportRunner``; nothing here reimplements a pipeline stage.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml

from spectragent.core.composer import ConfigComposer
from spectragent.core.registries import collect_registrars
from spectragent.export.runner import ExportReport, ExportRunner

REPO = Path(__file__).resolve().parents[2]
#: The shipped student (v1: UI-TARS on a 0-1000 grid, pinned DPR 1).
STUDENT_CONFIG = REPO / "configs" / "flywheel" / "ui_tars_student.yaml"


def new_composer() -> ConfigComposer:
    return ConfigComposer(registrars=collect_registrars())


def student_root(**agent_overrides: Any) -> dict[str, Any]:
    """The shipped student config as a dict, the agent entry updated with *agent_overrides*."""
    root = yaml.safe_load(STUDENT_CONFIG.read_text())
    root["agent"][0].update(copy.deepcopy(agent_overrides))
    return root


def write_student(path: Path, root: dict[str, Any] | None = None) -> Path:
    """Write a student config (default: the shipped one) to *path* and return it."""
    path.write_text(yaml.safe_dump(root if root is not None else student_root()))
    return path


def exporter_entry(sources_root: Path, output_dir: Path, **overrides: Any) -> dict[str, Any]:
    """An ``exporter:`` entry over *sources_root*: identity, no decontamination, no replay filter."""
    entry: dict[str, Any] = {
        "exporter_id": "openai_messages",
        "sources_root": str(sources_root),
        "sources": ["*"],
        "filters": {"max_document_misses": "unlimited"},
        "decontamination": {"against": []},
        "translator": {"translator_id": "identity"},
        "student_run_config": str(STUDENT_CONFIG),
        "output_dir": str(output_dir),
    }
    entry.update(overrides)
    return entry


def run_export(
    composer: ConfigComposer, entry: dict[str, Any], **layers: list[dict[str, Any]]
) -> ExportReport:
    """Compose one ``exporter:`` entry (plus any other top-level *layers*) as the CLI does; run it.

    *layers* are further blocks of the same document — ``eval_loader=[...]``,
    ``task_loader=[...]`` — read the way the CLI reads them
    (``ExportRunner.from_run``).
    """
    document = {"exporter": [copy.deepcopy(entry)], **copy.deepcopy(layers)}
    run = composer.compose(document)
    return ExportRunner.from_run(run, composer=composer, origin="the test document").run()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every JSON line of *path*."""
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def write_benchmark(
    data_root: Path, benchmark: str, records: list[dict[str, Any]], *, subset: str = "test"
) -> Path:
    """A benchmark data directory the real loaders read: ``<root>/browser/<benchmark>/``.

    Point the loaders at it with ``SPECTRAGENT_BENCHMARK_DATA=<data_root>``,
    or at the returned directory with a loader's ``data_dir``. Each record is
    a raw task (``task_id``, ``task_text``, ``start_url``, ``expected_answer``
    …) — the task and the eval loader read the same file. Another call with
    another *subset* adds that subset beside the first.
    """
    directory = data_root / "browser" / benchmark
    directory.mkdir(parents=True, exist_ok=True)
    info = directory / "data_info.json"
    split = json.loads(info.read_text())["split"] if info.is_file() else {}
    split[subset] = f"{subset}.json"
    info.write_text(json.dumps({"split": split}))
    (directory / f"{subset}.json").write_text(
        json.dumps([{"benchmark": benchmark, "env_type": "browser", **r} for r in records])
    )
    return directory

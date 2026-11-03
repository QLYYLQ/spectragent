"""A benchmark YAML over tasks in a data directory of its own (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml


def online_mind2web_benchmark(
    directory: Path, subsets: Mapping[str, Sequence[Mapping[str, str]]]
) -> Path:
    """An ``online_mind2web`` benchmark YAML whose task loader reads *subsets* (name → task records).

    The task loader's ``subset`` is left out, so it is the loader's default (``All``).
    """
    data = directory / "data"
    data.mkdir(parents=True)
    split = {name: f"{name}.json" for name in subsets}
    (data / "data_info.json").write_text(json.dumps({"split": split}))
    for name, tasks in subsets.items():
        (data / split[name]).write_text(json.dumps(list(tasks)))
    loader = {"benchmark_name": "online_mind2web", "data_dir": str(data)}
    path = directory / "benchmark.yaml"
    path.write_text(
        yaml.safe_dump({"benchmark": [{"benchmark_id": "online_mind2web", "task_loader": loader}]})
    )
    return path

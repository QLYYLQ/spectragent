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


#: The ``open_app`` table :func:`androidworld_data` writes, in short.
ANDROIDWORLD_APPS = {
    "clock": "com.google.android.deskclock/com.android.deskclock.DeskClock",
    "markor": "net.gsantner.markor/net.gsantner.markor.activity.MainActivity",
}


def androidworld_data(directory: Path) -> Path:
    """AndroidWorld's data in *directory*, as ``task_list.py`` writes it: one task, and the app table.

    The task is ``SystemBrightnessMax`` (suite seed 42, index 0) of the pinned
    commit, in the subset ``All``; ``open_app.json`` holds :data:`ANDROIDWORLD_APPS`.
    """
    from spectragent.benchmarks.androidworld.open_app import OPEN_APP_FILE, OPEN_APP_FORMAT
    from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
    from spectragent.benchmarks.androidworld.tasks import TASK_LIST_FORMAT, instance_seed

    directory.mkdir(parents=True, exist_ok=True)
    task = {
        "commit": PINNED_COMMIT,
        "family": "android",
        "task": "SystemBrightnessMax",
        "index": 0,
        "suite_seed": 42,
        "params": {"max_or_min": "max", "seed": instance_seed(42, "SystemBrightnessMax", 0)},
        "goal": "Turn brightness to the max value.",
        "goal_at_initialize": "unchanged",
        "complexity": 1.0,
        "start_on_home_screen": True,
    }
    (directory / "All.json").write_text(json.dumps({"format": TASK_LIST_FORMAT, "tasks": [task]}))
    (directory / "data_info.json").write_text(json.dumps({"split": {"All": "All.json"}}))
    table = {"format": OPEN_APP_FORMAT, "commit": PINNED_COMMIT, "apps": ANDROIDWORLD_APPS}
    (directory / OPEN_APP_FILE).write_text(json.dumps(table))
    return directory

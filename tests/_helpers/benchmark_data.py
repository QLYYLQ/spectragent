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


def androidworld_task(
    task: str = "SystemBrightnessMax",
    *,
    family: str = "android",
    goal: str = "Turn brightness to the max value.",
    goal_at_initialize: str = "unchanged",
    params: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """One AndroidWorld task record of the pinned commit, suite seed 42, index 0, as the list holds it."""
    from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
    from spectragent.benchmarks.androidworld.tasks import instance_seed

    return {
        "commit": PINNED_COMMIT,
        "family": family,
        "task": task,
        "index": 0,
        "suite_seed": 42,
        "params": {**(params or {}), "seed": instance_seed(42, task, 0)},
        "goal": goal,
        "goal_at_initialize": goal_at_initialize,
        "complexity": 1.0,
        "start_on_home_screen": family != "miniwob",
    }


def androidworld_data(directory: Path, tasks: Sequence[Mapping[str, object]] = ()) -> Path:
    """AndroidWorld's data in *directory*, as its fetch writes it: the tasks, the app table, the version.

    *tasks* (``SystemBrightnessMax`` alone when none are given) form the subset
    ``All``; ``open_app.json`` holds :data:`ANDROIDWORLD_APPS`; ``data_version.json``
    names the pinned commit, as ``spectragent benchmark fetch androidworld``
    records it.
    """
    from spectragent.benchmarks.androidworld.data import DATA
    from spectragent.benchmarks.androidworld.open_app import OPEN_APP_FILE, OPEN_APP_FORMAT
    from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
    from spectragent.benchmarks.androidworld.tasks import TASK_LIST_FORMAT
    from spectragent.core.data_loader._common import write_data_version

    directory.mkdir(parents=True, exist_ok=True)
    listed = list(tasks) or [androidworld_task(params={"max_or_min": "max"})]
    (directory / "All.json").write_text(json.dumps({"format": TASK_LIST_FORMAT, "tasks": listed}))
    (directory / "data_info.json").write_text(json.dumps({"split": {"All": "All.json"}}))
    table = {"format": OPEN_APP_FORMAT, "commit": PINNED_COMMIT, "apps": ANDROIDWORLD_APPS}
    (directory / OPEN_APP_FILE).write_text(json.dumps(table))
    write_data_version(directory, DATA)
    return directory

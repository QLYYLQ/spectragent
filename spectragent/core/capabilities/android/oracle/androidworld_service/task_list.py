# pyright: reportMissingImports=false
"""Write an AndroidWorld task list for Spectragent's ``androidworld`` task loader.

Runs offline (no device) under AndroidWorld's own interpreter, against the
checkout the oracle pins; like ``serve.py`` beside it, it is not a module of
Spectragent::

    <root>/.venv/bin/python -I task_list.py --src <root>/src --out <data dir> \\
        [--family android_world] [--seed 42] [--n 1] [--tasks NAME,NAME] [--split All]

It makes the suite AndroidWorld's own runner makes (``create_suite``: each
task's instances, their seeds derived from the suite seed, the task and the
index) and writes ``<data dir>/<split>.json``, format
``androidworld-tasks-1`` (``spectragent/core/capabilities/android/oracle/androidworld_tasks.py``),
then names the split in ``<data dir>/data_info.json``. Point the loader's
``data_dir`` there, or write it to
``$SPECTRAGENT_BENCHMARK_DATA/android/androidworld``.

Each task records the narrowest family whose registry holds it
(``android``, ``information_retrieval`` or ``miniwob``); asking for
``android_world`` yields the first two. A parameter that is not JSON is written
in a plain form: a dataclass as its fields, an image as its size, mode and the
sha256 of its pixels. A MiniWoB task's goal is made on the device when it is
initialized, so its ``goal`` is the template's fixed part.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

FORMAT = "androidworld-tasks-1"
#: Searched in this order for the family that holds a task.
FAMILIES = ("android", "information_retrieval", "miniwob")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write an AndroidWorld task list.")
    parser.add_argument("--src", required=True, help="The AndroidWorld checkout.")
    parser.add_argument("--out", required=True, help="The data directory to write into.")
    parser.add_argument("--family", default="android_world", help="The suite's family.")
    parser.add_argument("--seed", type=int, default=42, help="The suite seed.")
    parser.add_argument("--n", type=int, default=1, help="Instances of each task.")
    parser.add_argument("--tasks", default="", help="Comma-separated task keys; all by default.")
    parser.add_argument("--split", default="All", help="The split name (the loader's subset).")
    return parser.parse_args()


def _plain(value: object) -> object:
    """A JSON form of a parameter value ``json`` cannot write."""
    from PIL import Image

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Image.Image):
        return {
            "image_sha256": hashlib.sha256(value.tobytes()).hexdigest(),
            "size": list(value.size),
            "mode": value.mode,
        }
    raise TypeError(f"no plain JSON form for a {type(value).__name__} parameter")


def main() -> None:
    args = _arguments()
    sys.path.insert(0, args.src)
    from android_world import registry, suite_utils
    from android_world.task_evals.miniwob import miniwob_base

    commit = subprocess.run(
        ["git", "-C", args.src, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    registries = registry.TaskRegistry()
    holders = {family: registries.get_registry(family) for family in FAMILIES}
    suite = suite_utils.create_suite(
        registries.get_registry(args.family),
        n_task_combinations=args.n,
        seed=args.seed,
        tasks=[name for name in args.tasks.split(",") if name] or None,
    )
    tasks = []
    for name, instances in suite.items():
        family = next(family for family, held in holders.items() if name in held)
        for index, instance in enumerate(instances):
            made_at_initialize = isinstance(instance, miniwob_base.MiniWoBTask)
            if made_at_initialize:
                goal = type(instance).template.partition("{")[0].rstrip(" :") + "."
            else:
                goal = instance.goal
            tasks.append(
                {
                    "commit": commit,
                    "family": family,
                    "task": name,
                    "index": index,
                    "suite_seed": args.seed,
                    "params": json.loads(json.dumps(instance.params, default=_plain)),
                    "goal": goal,
                    "goal_made_at_initialize": made_at_initialize,
                    "complexity": float(instance.complexity),
                    "start_on_home_screen": bool(type(instance).start_on_home_screen),
                }
            )
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    listing = out / f"{args.split}.json"
    listing.write_text(json.dumps({"format": FORMAT, "tasks": tasks}, indent=1) + "\n")
    info_path = out / "data_info.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    info.setdefault("split", {})[args.split] = listing.name
    info_path.write_text(json.dumps(info, indent=2) + "\n")
    families = Counter(task["family"] for task in tasks)
    print(f"wrote {len(tasks)} tasks to {listing} ({dict(families)}), commit {commit}")


if __name__ == "__main__":
    main()

# pyright: reportMissingImports=false
"""Write an AndroidWorld task list for Spectragent's ``androidworld`` task loader.

Runs offline (no device) under AndroidWorld's own interpreter, against the
checkout the ``androidworld`` setup pins; like ``serve.py`` beside it, it is not a module of
Spectragent::

    <root>/.venv/bin/python -I task_list.py --src <root>/src --out <data dir> \\
        [--family android_world] [--seed 42] [--n 1] [--tasks NAME,NAME] [--split All]

It makes the suite AndroidWorld's own runner makes (``create_suite``: each
task's instances, their seeds derived from the suite seed, the task and the
index) and writes ``<data dir>/<split>.json``, format
``androidworld-tasks-2`` (``spectragent/benchmarks/androidworld/tasks.py``),
then names the split in ``<data dir>/data_info.json``. ``spectragent
benchmark fetch androidworld`` runs it for the subsets the benchmark ships
(``spectragent/benchmarks/androidworld/data.py``) into the benchmark's data
directory; run it by hand for a list of your own, and point the loader's
``data_dir`` there.

Beside the lists it writes ``<data dir>/open_app.json``, format
``androidworld-open-app-1`` (``spectragent/benchmarks/androidworld/open_app.py``):
AndroidWorld's app name table, every name of each pattern of
``adb_utils._PATTERN_TO_ACTIVITY`` with the component AndroidWorld starts for
it, read off the same checkout. Each name is checked to open its own pattern's
component under AndroidWorld's own matcher (``get_adb_activity``: a prefix
match, the first pattern first), so the table opens what AndroidWorld opens.
The benchmark hands it to the Android capability's ``android.open_app``
(``docs/architecture_design.md`` §11.2 "``open_app`` 的名称表").

Each task records the narrowest family whose registry holds it
(``android``, ``information_retrieval`` or ``miniwob``); asking for
``android_world`` yields the first two. A parameter that is not JSON is written
in a plain form: a dataclass as its fields, an image as its size, mode and the
sha256 of its pixels.

``goal_at_initialize`` says what AndroidWorld's initialize does to a goal,
read off its own code: a MiniWoB task's is ``made`` on the device then, so its
``goal`` is the template's fixed part; an information-retrieval task whose goal
names a date parameter has it ``rewritten`` (``_maybe_replace_date`` rewords
the date, "October 26 2023" into "the Thursday after next", drawing with the
instance's seed), so its ``goal`` is the one before initialize; every other
goal stays ``unchanged``.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import string
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from android_world.task_evals.task_eval import TaskEval

FORMAT = "androidworld-tasks-2"
OPEN_APP_FORMAT = "androidworld-open-app-1"
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


def _goal_at_initialize(instance: TaskEval) -> str:
    """What AndroidWorld's ``initialize_task`` does to *instance*'s goal: ``made``, ``rewritten`` or ``unchanged``."""
    from android_world.task_evals.information_retrieval import datetime_utils, information_retrieval
    from android_world.task_evals.miniwob import miniwob_base

    if isinstance(instance, miniwob_base.MiniWoBTask):
        return "made"
    if isinstance(instance, information_retrieval.InformationRetrieval):
        named = {field for _, field, _, _ in string.Formatter().parse(instance.template) if field}
        for name, value in instance.params.items():
            # The parameters _maybe_replace_date rewords: a date in its format.
            if name in named and name != "seed" and isinstance(value, str) and value:
                try:
                    datetime_utils.get_date(value)
                except ValueError:
                    continue
                return "rewritten"
    return "unchanged"


def _open_app_table() -> dict[str, str]:
    """AndroidWorld's app names, each with the component its ``launch_app`` starts for it."""
    from android_world.env import adb_utils

    apps: dict[str, str] = {}
    for pattern, component in adb_utils._PATTERN_TO_ACTIVITY.items():
        for name in pattern.split("|"):
            # AndroidWorld matches a name as a regex prefix, the first pattern
            # first: a name an earlier pattern takes would open that pattern's
            # component there, and this one here.
            if adb_utils.get_adb_activity(name) != component:
                raise SystemExit(
                    f"AndroidWorld opens {name!r} with {adb_utils.get_adb_activity(name)!r}, "
                    f"not with its own pattern's {component!r}"
                )
            apps[name] = component
    return apps


def main() -> None:
    args = _arguments()
    sys.path.insert(0, args.src)
    from android_world import registry, suite_utils

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
            at_initialize = _goal_at_initialize(instance)
            if at_initialize == "made":
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
                    "goal_at_initialize": at_initialize,
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
    apps = _open_app_table()
    table = {"format": OPEN_APP_FORMAT, "commit": commit, "apps": apps}
    (out / "open_app.json").write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
    families = Counter(task["family"] for task in tasks)
    print(
        f"wrote {len(tasks)} tasks to {listing} ({dict(families)}) and {len(apps)} app names "
        f"to {out / 'open_app.json'}, commit {commit}"
    )


if __name__ == "__main__":
    main()

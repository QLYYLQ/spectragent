"""AndroidWorld's task lists and ``open_app`` table, made from the pinned commit (``docs/architecture_design.md`` §4.10.1 "数据", §11.2).

AndroidWorld's tasks are not a file to download: its own code makes them,
each with parameters drawn from a seed. So its data is pinned by the commit
of its git repository, and a fetch runs ``task_list.py`` (beside the
launcher, ``androidworld_service/``) under the installation's interpreter, on
its checkout at that commit (``spectragent benchmark setup androidworld``
installs both; a fetch without them is refused naming that command). It
writes two subsets, each a suite as AndroidWorld's own runner makes it (suite
seed 42, one instance of each task): ``All``, the ``android_world`` family
(its Android and information-retrieval tasks), and ``miniwob``, the MiniWoB++
family; ``data_info.json`` naming both; and the ``open_app`` table
(``open_app.json``), read off the same checkout. Each file records the commit
it was made from, and the fetch checks it is the pinned one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from spectragent.benchmarks.androidworld.open_app import read_open_app_table
from spectragent.benchmarks.androidworld.service import PINNED_COMMIT, Installation
from spectragent.benchmarks.androidworld.tasks import AndroidWorldTaskList
from spectragent.benchmarks.data import Say, run_program
from spectragent.core.data_loader._common import BenchmarkDataError, validation_summary
from spectragent.schemas.data_version import DataVersion

#: AndroidWorld's repository, and the commit its tasks are made from.
REPOSITORY = "https://github.com/google-research/android_world.git"
DATA = DataVersion(source="git", repository=REPOSITORY, revision=PINNED_COMMIT)
#: The program that lists the tasks, run under AndroidWorld's interpreter.
TASK_LIST = Path(__file__).resolve().parent / "androidworld_service" / "task_list.py"
#: The subsets a fetch writes: AndroidWorld's suite family, and the subset's name.
SUBSETS = (("android_world", "All"), ("miniwob", "miniwob"))


@dataclass(frozen=True)
class AndroidWorldData:
    """AndroidWorld's task lists at :data:`DATA`, made in *installation*."""

    installation: Installation

    @property
    def version(self) -> DataVersion:
        return DATA

    def fetch(self, into: Path, say: Say) -> None:
        installation = self.installation
        problems = installation.checkout_problems()
        if problems:
            raise BenchmarkDataError(
                "AndroidWorld's task lists are made by its own code, in its installation, and "
                + "; ".join(problems)
                + f": install it first with {installation.setup_command()}"
            )
        for family, subset in SUBSETS:
            say(f"listing AndroidWorld's {family} tasks as the subset {subset}")
            run_program(
                [
                    installation.python,
                    "-I",
                    TASK_LIST,
                    "--src",
                    installation.src,
                    "--out",
                    into,
                    "--family",
                    family,
                    "--split",
                    subset,
                ],
                f"list AndroidWorld's {family} tasks",
            )
        for _, subset in SUBSETS:
            _check_listing(into / f"{subset}.json")
        read_open_app_table(into)


def _check_listing(path: Path) -> None:
    """Refuse a task list that is not one, or lists a task made from another commit."""
    try:
        listing = AndroidWorldTaskList.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        why = validation_summary(exc) if isinstance(exc, ValidationError) else str(exc)
        raise BenchmarkDataError(f"task_list.py wrote no task list at {path} ({why})") from None
    commits = sorted({task.commit for task in listing.tasks} - {PINNED_COMMIT})
    if commits:
        raise BenchmarkDataError(
            f"{path} lists tasks made from AndroidWorld {', '.join(commits)}, and the benchmark "
            f"pins {PINNED_COMMIT}"
        )


__all__ = ["DATA", "REPOSITORY", "SUBSETS", "TASK_LIST", "AndroidWorldData"]

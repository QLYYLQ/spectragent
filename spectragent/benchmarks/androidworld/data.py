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

:class:`TaskLists` is that fetch for any installation of AndroidWorld's code:
VenusBench's fork makes its lists with the same program, in its own
installation, from its own pinned commit and with its own subsets
(``spectragent/benchmarks/venusbench/``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from spectragent.benchmarks.androidworld.open_app import read_open_app_table
from spectragent.benchmarks.androidworld.service import ANDROIDWORLD, Installation
from spectragent.benchmarks.androidworld.tasks import AndroidWorldTaskList
from spectragent.benchmarks.data import Say, run_program
from spectragent.core.data_loader._common import BenchmarkDataError, validation_summary
from spectragent.schemas.data_version import DataVersion

#: The commit AndroidWorld's tasks are made from.
DATA = DataVersion(source="git", repository=ANDROIDWORLD.repository, revision=ANDROIDWORLD.commit)
#: The program that lists the tasks, run under the installation's interpreter.
TASK_LIST = Path(__file__).resolve().parent / "androidworld_service" / "task_list.py"


@dataclass(frozen=True)
class Subset:
    """One subset a fetch writes: the suite's family, the subset's name and the suite seed."""

    family: str
    name: str
    seed: int = 42


#: The subsets AndroidWorld's fetch writes.
SUBSETS = (Subset("android_world", "All"), Subset("miniwob", "miniwob"))
#: AndroidWorld's families, searched in this order for the narrowest one holding a task.
HOLDERS = ("android", "information_retrieval", "miniwob")


@dataclass(frozen=True)
class TaskLists:
    """Task lists made by the code in *installation*, from its pinned commit, one per subset.

    Attributes:
        installation: Where the code and its interpreter are.
        subsets: The subsets to write.
        holders: The families ``task_list.py`` searches, in order, for the
            narrowest one holding each task.
    """

    installation: Installation
    subsets: tuple[Subset, ...]
    holders: tuple[str, ...]

    @property
    def version(self) -> DataVersion:
        code = self.installation.code
        return DataVersion(source="git", repository=code.repository, revision=code.commit)

    def fetch(self, into: Path, say: Say) -> None:
        installation = self.installation
        name = installation.code.name
        problems = installation.checkout_problems()
        if problems:
            raise BenchmarkDataError(
                f"{name}'s task lists are made by its own code, in its installation, and "
                + "; ".join(problems)
                + f": install it first with {installation.setup_command()}"
            )
        for subset in self.subsets:
            say(f"listing {name}'s {subset.family} tasks as the subset {subset.name}")
            run_program(
                [
                    installation.python,
                    "-I",
                    TASK_LIST,
                    "--src",
                    installation.src,
                    "--out",
                    into,
                    "--holders",
                    ",".join(self.holders),
                    "--seed",
                    str(subset.seed),
                    "--family",
                    subset.family,
                    "--split",
                    subset.name,
                ],
                f"list {name}'s {subset.family} tasks",
            )
        for subset in self.subsets:
            _check_listing(into / f"{subset.name}.json", installation)
        read_open_app_table(into, installation.code)


def _check_listing(path: Path, installation: Installation) -> None:
    """Refuse a task list that is not one, or lists a task made from another commit."""
    code = installation.code
    try:
        listing = AndroidWorldTaskList.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        why = validation_summary(exc) if isinstance(exc, ValidationError) else str(exc)
        raise BenchmarkDataError(f"task_list.py wrote no task list at {path} ({why})") from None
    commits = sorted({task.commit for task in listing.tasks} - {code.commit})
    if commits:
        raise BenchmarkDataError(
            f"{path} lists tasks made from {code.name} {', '.join(commits)}, and the benchmark "
            f"pins {code.commit}"
        )


__all__ = ["DATA", "HOLDERS", "SUBSETS", "TASK_LIST", "Subset", "TaskLists"]

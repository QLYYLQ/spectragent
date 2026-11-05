"""Online-Mind2Web's data, pinned on Hugging Face (``docs/architecture_design.md`` §4.10.1 "数据").

The dataset is ``osunlp/Online-Mind2Web``, pinned at the commit holding its
2025-11-03 task update. It is gated: a fetch needs ``$HF_TOKEN``, a token of
an account that accepted its terms on Hugging Face. Its one file,
``Online_Mind2Web.json``, lists the 300 tasks as ``{task_id, confirmed_task,
website, reference_length, level}``; a fetch writes them as the task loader
reads them: the same file name holding each task's ``task_text`` (the
confirmed task), ``start_url`` (the website), ``difficulty`` (the level) and
``metadata.reference_length``, the subset ``Hard`` (the tasks of level
``hard``) in ``hard.json``, and ``data_info.json`` naming both subsets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from spectragent.benchmarks.data import Say, download_huggingface_file
from spectragent.core.data_loader._common import BenchmarkDataError, validation_summary
from spectragent.schemas.data_version import DataVersion

#: The dataset, at the commit this benchmark reads.
DATA = DataVersion(
    source="huggingface",
    repository="osunlp/Online-Mind2Web",
    revision="eacad896a84dc5b65e29b0b06e4699ab0544d701",
)
#: The dataset's file of tasks, and the file the task loader reads them from.
TASKS_FILE = "Online_Mind2Web.json"
#: The file of the subset ``Hard``.
HARD_FILE = "hard.json"


class UpstreamTask(BaseModel):
    """One task as the dataset lists it."""

    model_config = ConfigDict(extra="ignore", strict=True)

    task_id: str = Field(min_length=1)
    confirmed_task: str = Field(min_length=1)
    website: str = Field(min_length=1)
    reference_length: int
    level: Literal["easy", "medium", "hard"]


_UPSTREAM = TypeAdapter(list[UpstreamTask])


class OnlineMind2WebData:
    """Online-Mind2Web's tasks at :data:`DATA`, fetched from Hugging Face."""

    @property
    def version(self) -> DataVersion:
        return DATA

    def fetch(self, into: Path, say: Say) -> None:
        downloaded = download_huggingface_file(
            DATA.repository, DATA.revision, TASKS_FILE, into, say
        )
        tasks = [task_record(task) for task in read_upstream(downloaded)]
        hard = [task for task in tasks if task["difficulty"] == "hard"]
        _write_json(into / TASKS_FILE, tasks)
        _write_json(into / HARD_FILE, hard)
        _write_json(into / "data_info.json", {"split": {"All": TASKS_FILE, "Hard": HARD_FILE}})
        say(f"wrote {len(tasks)} tasks, {len(hard)} of them in the subset Hard")


def read_upstream(path: Path) -> list[UpstreamTask]:
    """The tasks the dataset's file at *path* lists.

    Raises:
        BenchmarkDataError: It is not a list of tasks of the shape above; the
            message names the keys its first record has.
    """
    raw = path.read_bytes()
    try:
        return _UPSTREAM.validate_json(raw)
    except ValidationError as exc:
        try:
            first = json.loads(raw)[0]
            keys = f"; its first record has the keys {sorted(first)}"
        except (ValueError, LookupError, TypeError):
            keys = ""
        raise BenchmarkDataError(
            f"{path.name} of {DATA.describe()} is not a list of tasks with task_id, "
            f"confirmed_task, website, reference_length and level ({validation_summary(exc)})"
            f"{keys}"
        ) from None


def task_record(task: UpstreamTask) -> dict[str, object]:
    """*task* as the ``online_mind2web`` task loader reads it."""
    return {
        "task_id": task.task_id,
        "benchmark": "online_mind2web",
        "env_type": "browser",
        "task_text": task.confirmed_task,
        "start_url": task.website,
        "difficulty": task.level,
        "metadata": {"reference_length": task.reference_length},
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


__all__ = ["DATA", "OnlineMind2WebData", "UpstreamTask", "read_upstream", "task_record"]

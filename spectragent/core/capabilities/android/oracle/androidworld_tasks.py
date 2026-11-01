"""AndroidWorld's tasks, as a task list records them and as the ``androidworld`` oracle receives them.

A task is addressed as AndroidWorld's own suite addresses it: the family whose
registry holds it, its task key, and its index in a suite made with a seed.
AndroidWorld derives each instance's seed from that address
(``sha256(f"{suite_seed}_{task}_{index}") % 2**32``), seeds ``random`` with it
and generates the task's parameters, so its service rebuilds them from the
address alone; the list records them for provenance only. 31 of the 116 tasks
of the pinned commit hold parameters that are not JSON (dataclass rows, one
image), which the list records in a plain form, so they could not be handed
back to the service in any case.

A task list is one JSON document, ``{"format": "androidworld-tasks-1",
"tasks": [<task>, ...]}``, written offline in AndroidWorld's own venv by
``androidworld_service/task_list.py`` (``docs/architecture_design.md`` §11.2).
The ``androidworld`` task loader reads it, and each task's record, unchanged,
is the task's ``oracle_task``. Nothing here imports the oracle, so the loader
reads a list without it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

#: The ``format`` a task list declares.
TASK_LIST_FORMAT = "androidworld-tasks-1"
#: The family whose tasks are scored on the agent's answer.
INFORMATION_RETRIEVAL = "information_retrieval"
#: Tasks of the pinned commit that the service cannot build again from their
#: address, so it never builds the listed one: task key → why. The loader
#: leaves them out and the oracle refuses them, both with these words.
UNREPRODUCIBLE: Mapping[str, str] = MappingProxyType(
    {
        "MarkorCreateFolder": (
            "it names its folder after the host's clock when the service builds it, so the "
            "service never builds the listed task"
        ),
    }
)


def instance_seed(suite_seed: int, task: str, index: int) -> int:
    """The seed AndroidWorld gives the *index*-th instance of *task* in a suite made with *suite_seed*."""
    digest = hashlib.sha256(f"{suite_seed}_{task}_{index}".encode()).hexdigest()
    return int(digest, 16) % 2**32


class AndroidWorldTask(BaseModel):
    """One AndroidWorld task.

    Attributes:
        commit: The AndroidWorld commit the list was generated with; the
            oracle refuses a task from any other commit than its own.
        family: The narrowest family whose registry holds the task:
            ``android``, ``information_retrieval`` or ``miniwob``.
        task: The task's key in that registry (its class name; a MiniWoB
            task's name, such as ``click-button``).
        index: Its index in the suite.
        suite_seed: The seed the suite was made with.
        params: Its parameters as generated, the instance seed under ``seed``;
            a value that is not JSON in a plain form (a dataclass as its
            fields, an image as its size, mode and a digest of its pixels).
        goal: What the agent is asked to do. When ``goal_made_at_initialize``,
            only the goal's fixed part: the rest is made on the device.
        goal_made_at_initialize: AndroidWorld makes the goal on the device
            when it initializes the task (MiniWoB's JavaScript writes its
            instruction then, differently each time), so it is not known
            before.
        complexity: AndroidWorld's estimate; its own runner gives a task
            ``int(10 * complexity)`` steps.
        start_on_home_screen: Whether the episode starts on the home screen
            (``/reset?go_home=``); MiniWoB tasks start in their app.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    commit: str
    family: str
    task: str
    index: int = Field(ge=0)
    suite_seed: int
    params: dict[str, JsonValue]
    goal: str
    goal_made_at_initialize: bool
    complexity: float = Field(gt=0)
    start_on_home_screen: bool

    @model_validator(mode="after")
    def _the_seed_is_the_addressed_instances(self) -> Self:
        expected = instance_seed(self.suite_seed, self.task, self.index)
        if self.params.get("seed") != expected:
            raise ValueError(
                f"params.seed is {self.params.get('seed')!r}, and the instance AndroidWorld "
                f"makes for {self.task} #{self.index} with suite seed {self.suite_seed} has seed "
                f"{expected}"
            )
        return self


class AndroidWorldTaskList(BaseModel):
    """A task list file: its format, and its tasks in order."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    format: Literal["androidworld-tasks-1"]
    tasks: list[AndroidWorldTask]

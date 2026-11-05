"""AndroidWorld's tasks, as a task list records them and as the ``androidworld`` setup receives them.

A task is addressed as AndroidWorld's own suite addresses it: the family whose
registry holds it, its task key, and its index in a suite made with a seed.
AndroidWorld derives each instance's seed from that address
(``sha256(f"{suite_seed}_{task}_{index}") % 2**32``), seeds ``random`` with it
and generates the task's parameters, so its service rebuilds them from the
address alone; the list records them for provenance only. 31 of the 116 tasks
of the pinned commit hold parameters that are not JSON (dataclass rows, one
image), which the list records in a plain form, so they could not be handed
back to the service in any case.

A task list is one JSON document, ``{"format": "androidworld-tasks-2",
"tasks": [<task>, ...]}``, written offline in AndroidWorld's own venv by
``androidworld_service/task_list.py`` (``docs/architecture_design.md`` §11.2).
VenusBench's fork of AndroidWorld writes its lists in the same format, with
the same program run in its own venv.
The ``androidworld`` task loader reads it, and each task's record, unchanged,
is the task's ``oracle_task``, which the ``androidworld`` setup component reads.
Nothing here imports the setup component, so the loader reads a list without it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from spectragent.core.data_loader._common import BenchmarkDataError, validation_summary

#: The ``format`` a task list declares.
TASK_LIST_FORMAT = "androidworld-tasks-2"
#: What AndroidWorld's ``initialize_task`` does to a task's goal (:class:`AndroidWorldTask`).
GoalAtInitialize = Literal["unchanged", "made", "rewritten"]
#: The family whose tasks are scored on the agent's answer.
INFORMATION_RETRIEVAL = "information_retrieval"
#: The family whose episodes the page ends: AndroidWorld's runner asks a MiniWoB++
#: page after every step whether it reported a reward.
MINIWOB = "miniwob"
#: Tasks of the pinned commit that the service cannot build again from their
#: address, so it never builds the listed one: task key → why. The loader
#: leaves them out and the setup component refuses them, both with these words.
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
            setup component refuses a task from any other commit than its own.
        family: The narrowest family whose registry holds the task:
            ``android``, ``information_retrieval`` or ``miniwob``.
        task: The task's key in that registry (its class name; a MiniWoB
            task's name, such as ``click-button``).
        index: Its index in the suite.
        suite_seed: The seed the suite was made with.
        params: Its parameters as generated, the instance seed under ``seed``;
            a value that is not JSON in a plain form (a dataclass as its
            fields, an image as its size, mode and a digest of its pixels).
        goal: What the agent is asked to do, as known before the task is
            initialized: when ``goal_at_initialize`` is ``made``, only the
            goal's fixed part; when ``rewritten``, the goal before its
            rewording.
        goal_at_initialize: What AndroidWorld's initialize does to the goal.
            ``made``: the goal exists only once the task is initialized
            (MiniWoB's JavaScript writes its instruction then, unseeded).
            ``rewritten``: initialize rewords a date the goal names
            (information retrieval: "October 26 2023" becomes, say, "the
            Thursday after next"; drawn with the instance's seed, so the same
            each time). ``unchanged``: the goal is final before.
            For the first two the setup component reads the goal after
            initialize, and it is the run's task text
            (``docs/architecture_design.md`` §11.2).
        complexity: AndroidWorld's estimate; its own runner gives a task
            ``int(10 * complexity)`` steps (:attr:`step_budget`).
        start_on_home_screen: Whether the episode starts on the home screen,
            as the task's class says; MiniWoB tasks start in their app. A task
            may change it in its initialize (VenusBench's
            VagueMarkorDeleteNewestTwoNotes starts in Markor), so the setup
            component resets as the initialized task says
            (``/task/start_on_home_screen``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    commit: str
    family: str
    task: str
    index: int = Field(ge=0)
    suite_seed: int
    params: dict[str, JsonValue]
    goal: str
    goal_at_initialize: GoalAtInitialize
    complexity: float = Field(gt=0)
    start_on_home_screen: bool

    @property
    def step_budget(self) -> int:
        """The steps AndroidWorld's own runner gives this task.

        ``suite_utils._allocate_step_budget`` of the pinned commit, the same
        expression on the same float: ``int(10 * complexity)``, truncated.
        """
        return int(10 * self.complexity)

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

    format: Literal["androidworld-tasks-2"]
    tasks: list[AndroidWorldTask]


def read_task_list(path: Path, *, benchmark: str = "androidworld") -> AndroidWorldTaskList:
    """The task list at *path*, read strictly, for *benchmark* (the ``benchmark_id`` that fetches it).

    Raises:
        BenchmarkDataError: It cannot be read, is not JSON, or is not a task
            list of :data:`TASK_LIST_FORMAT`; the message says to fetch it again.
    """
    try:
        return AndroidWorldTaskList.model_validate_json(path.read_bytes())
    except OSError as exc:
        raise BenchmarkDataError(f"cannot read the AndroidWorld task list {path}: {exc}") from exc
    except ValidationError as exc:
        raise BenchmarkDataError(
            f"{path} is not an AndroidWorld task list of format {TASK_LIST_FORMAT} "
            f"({validation_summary(exc)}): fetch it again with spectragent benchmark fetch "
            f"{benchmark}"
        ) from exc

"""The ``androidworld`` benchmark (``docs/architecture_design.md`` §4.10.1, §11.2)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, JsonValue

from spectragent.benchmarks._registrar import BenchmarkBase
from spectragent.benchmarks.androidworld.data import AndroidWorldData
from spectragent.benchmarks.androidworld.install import AndroidWorldInstallation, named_emulator
from spectragent.benchmarks.androidworld.open_app import read_open_app_table
from spectragent.benchmarks.androidworld.service import Installation
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.benchmarks.androidworld.tasks import MINIWOB, read_task_list
from spectragent.core.data_loader._common import resolve_split_file
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.verdict import Verdict

#: The Android capability and the action whose name table the benchmark gives.
_ANDROID = "android"
_OPEN_APP = "android.open_app"
#: The longest wait for a still screen before each capture, as AndroidWorld's
#: runner waits for every agent: up to 6 s at the start of each step
#: (``_get_stable_state``'s ``timeout``, https://github.com/google-research/android_world/blob/55ddaf5edc6f6c25673aed343907d5b9dca66f84/android_world/env/interface.py#L247,
#: called from android_world/agents/base_agent.py#L101-L106).
_STILL_SCREEN_WAIT_S = 6.0
#: The same wait on MiniWoB++ tasks. AndroidWorld's runner does not wait for a
#: still screen there (it pauses 0.2 s, ``run.py`` lines 141-142 and 218-222);
#: we keep our own wait, without a fixed pause (user ruling 2026-11-03), capped
#: at the 2 s of the MiniWoB++ regression that matched it (2026-11-04, 71 of 92).
_MINIWOB_STILL_SCREEN_WAIT_S = 2.0


class AndroidWorldBenchmark(BenchmarkBase):
    """AndroidWorld: tasks on an emulated phone, set up by its own service and judged on the device (§11.2).

    Its ``setup`` block is required: without the component no task starts.
    Its verifier, ``androidworld``, judges each task on the device at the
    scoring point (``verifier.py``); a block that lists none leaves its runs
    unjudged. Its part of the Android capability's configuration is its device image
    and its runner's typing and scroll, written in its block's ``capability``
    list, and its ``open_app`` name table and still-screen wait, computed in
    code (:meth:`computed_capability`).
    """

    __registry_key__ = "androidworld"
    __wiring__: ClassVar[dict[str, Any]] = {
        "task_loader": ("task_loader", ["androidworld"]),
        "setup": ("benchmark_setup", ["androidworld"]),
        "verifiers": ("verifier", ["androidworld"]),
    }
    __primary_verifier__ = "androidworld"

    # Without ``None``: a block that leaves the component out is refused.
    setup: AndroidWorldSetup  # type: ignore[assignment]

    @classmethod
    def computed_capability(cls, config: BaseModel) -> list[dict[str, JsonValue]]:
        """AndroidWorld's ``open_app`` name table, and how long its runner waits for a still screen.

        The table, as ``android.open_app``'s ``apps``, is read from beside the
        task lists of the block's task loader (``open_app.json``, which
        ``task_list.py`` writes from the pinned checkout), so ``open_app``
        opens the activity AndroidWorld opens for each of its names.

        The wait, ``settle_max_wait_s``, follows the subset the run takes
        (``--subset`` is written into the block's task loader before it is
        composed): 2 s when every task of the subset is a MiniWoB++ task, 6 s
        for any other subset (``docs/architecture_design.md`` §4.10.1
        "基准的 runner 设置归基准").

        Raises:
            BenchmarkDataError: The task lists' directory holds no table from
                the pinned commit, or the subset's task list cannot be read.
        """
        directory = cls.data_directory(config)
        table: dict[str, JsonValue] = dict(read_open_app_table(directory))
        subset = getattr(getattr(config, "task_loader", None), "subset", None)
        if not isinstance(subset, str):
            raise TypeError(f"the androidworld block's task loader has no subset: {config!r}")
        listing = read_task_list(resolve_split_file(directory, subset))
        miniwob = bool(listing.tasks) and all(task.family == MINIWOB for task in listing.tasks)
        return [
            {
                "capability_id": _ANDROID,
                "settle_max_wait_s": _MINIWOB_STILL_SCREEN_WAIT_S
                if miniwob
                else _STILL_SCREEN_WAIT_S,
                "actions": [{"action_id": _OPEN_APP, "apps": table}],
            }
        ]

    @classmethod
    def pinned_data(cls, config: BaseModel) -> AndroidWorldData:
        """Its task lists and ``open_app`` table, made from the pinned commit in its installation (``data.py``)."""
        return AndroidWorldData(_installation(config))

    @classmethod
    def installation(cls, config: BaseModel) -> AndroidWorldInstallation:
        """AndroidWorld's checkout, venv and cached forwarder where its setup looks, and the AVD it boots (``install.py``)."""
        return AndroidWorldInstallation(_installation(config), *named_emulator(config))

    def success(self, verdict: Verdict, result: AgentResult | None) -> float | None:
        """AndroidWorld's own rule: the device's score counts only when the agent declared the task done.

        Its runner records ``task_successful if done else 0``, partial credit
        included. A MiniWoB++ task's ending comes from its page, so its score
        alone counts (§11.2 "怎样与 AndroidWorld 比"). With no *result* the
        run's ending is unknown, so no success is claimed either way.
        """
        # The family as the task list and the verdict's evidence name it, read
        # from the task list's own model, so a rename cannot silently zero
        # every MiniWoB++ result.
        if verdict.status != "judged" or verdict.score is None:
            return None
        if verdict.evidence.get("family") == MINIWOB:
            return verdict.score
        if result is None:
            return None
        return verdict.score if result.agent_done == AgentDoneStatus.DONE else 0.0


def _installation(config: BaseModel) -> Installation:
    """The installation the block's setup component looks for: at its ``root``, else the default."""
    root: object = getattr(getattr(config, "setup", None), "root", None)
    if not (root is None or isinstance(root, str)):
        raise TypeError(f"the androidworld setup's root is {root!r}")
    return Installation.at(root)

"""The ``androidworld`` benchmark (``docs/architecture_design.md`` §4.10.1, §11.2)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, JsonValue

from spectragent.benchmarks._registrar import BenchmarkBase
from spectragent.benchmarks.androidworld.open_app import read_open_app_table
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.benchmarks.androidworld.tasks import MINIWOB
from spectragent.core.data_loader._common import resolve_data_dir
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.verdict import Verdict

#: The Android capability and the action whose name table the benchmark gives.
_ANDROID = "android"
_OPEN_APP = "android.open_app"


class AndroidWorldBenchmark(BenchmarkBase):
    """AndroidWorld: tasks on an emulated phone, set up by its own service and judged on the device (§11.2).

    Its ``setup`` block is required: without the component no task starts.
    Its verifier, ``androidworld``, judges each task on the device at the
    scoring point (``verifier.py``); a block that lists none leaves its runs
    unjudged. Its part of the Android capability's configuration is its device image,
    written in its block's ``capability`` list, and its ``open_app`` name
    table, computed in code (:meth:`computed_capability`).
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
        """AndroidWorld's ``open_app`` name table, as ``android.open_app``'s ``apps``.

        Read from beside the task lists of the block's task loader
        (``open_app.json``, which ``task_list.py`` writes from the pinned
        checkout), so ``open_app`` opens the activity AndroidWorld opens for
        each of its names.

        Raises:
            BenchmarkDataError: The task lists' directory holds no table from
                the pinned commit.
        """
        loader: object = getattr(config, "task_loader", None)
        data_dir: object = getattr(loader, "data_dir", None)
        if data_dir is not None and not isinstance(data_dir, str):
            raise TypeError(f"the androidworld task loader's data_dir is {data_dir!r}")
        apps = read_open_app_table(resolve_data_dir(_ANDROID, cls.__registry_key__, data_dir))
        table: dict[str, JsonValue] = dict(apps)
        return [{"capability_id": _ANDROID, "actions": [{"action_id": _OPEN_APP, "apps": table}]}]

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

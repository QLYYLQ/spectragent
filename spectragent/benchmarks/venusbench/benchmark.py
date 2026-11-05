"""The ``venusbench`` benchmark (``docs/architecture_design.md`` §4.10.1, §11.2 "VenusBench-Mobile 的接入方案")."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, JsonValue

from spectragent.benchmarks._registrar import BenchmarkBase
from spectragent.benchmarks.androidworld.data import Subset, TaskLists
from spectragent.benchmarks.androidworld.install import named_emulator
from spectragent.benchmarks.androidworld.open_app import read_open_app_table
from spectragent.benchmarks.venusbench.install import VenusBenchInstaller
from spectragent.benchmarks.venusbench.service import VenusBenchInstallation
from spectragent.benchmarks.venusbench.setup import VenusBenchSetup
from spectragent.benchmarks.venusbench.tasks import FAMILY, SUITE_SEED, VENUSBENCH
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.verdict import Verdict

#: The Android capability and the action whose name table the benchmark gives.
_ANDROID = "android"
_OPEN_APP = "android.open_app"
#: The longest wait for a still screen before each capture: VenusBench's runner
#: is AndroidWorld's, which waits up to 6 s at the start of each step.
_STILL_SCREEN_WAIT_S = 6.0
#: The one subset a fetch writes: every task of the ``venus`` family, as
#: VenusBench's own runner makes the suite (seed 30, one instance of each).
SUBSETS = (Subset(FAMILY, "All", SUITE_SEED),)


class VenusBenchBenchmark(BenchmarkBase):
    """VenusBench-Mobile: AndroidWorld's fork, its tasks set up by AndroidWorld's service and judged on the device (§11.2).

    Its ``setup`` block is required: without the component no task starts.
    Its verifier, ``venusbench``, judges each task on the device at the
    scoring point (``verifier.py``); a block that lists none leaves its runs
    unjudged. Its part of the Android capability's configuration is its
    device image and its runner's typing and scroll, written in its block's
    ``capability`` list, and its ``open_app`` name table and still-screen
    wait, computed in code (:meth:`computed_capability`).
    """

    __registry_key__ = "venusbench"
    __wiring__: ClassVar[dict[str, Any]] = {
        "task_loader": ("task_loader", ["venusbench"]),
        "setup": ("benchmark_setup", ["venusbench"]),
        "verifiers": ("verifier", ["venusbench"]),
    }
    __primary_verifier__ = "venusbench"

    # Without ``None``: a block that leaves the component out is refused.
    setup: VenusBenchSetup  # type: ignore[assignment]

    @classmethod
    def computed_capability(cls, config: BaseModel) -> list[dict[str, JsonValue]]:
        """VenusBench's ``open_app`` name table, and how long its runner waits for a still screen.

        The table, as ``android.open_app``'s ``apps``, is read from beside the
        task lists (``open_app.json``, which ``task_list.py`` writes from the
        pinned checkout: the fork keeps AndroidWorld's table and adds its own
        apps). Its ``calculator`` names Simple Calculator, which VenusBench
        never installs (it installs Google's calculator); the table stays as
        the fork has it, so opening ``calculator`` by name fails and says
        which apps are installed.

        Raises:
            BenchmarkDataError: The task lists' directory holds no table from
                the pinned commit.
        """
        table: dict[str, JsonValue] = dict(
            read_open_app_table(cls.data_directory(config), VENUSBENCH)
        )
        return [
            {
                "capability_id": _ANDROID,
                "settle_max_wait_s": _STILL_SCREEN_WAIT_S,
                "actions": [{"action_id": _OPEN_APP, "apps": table}],
            }
        ]

    @classmethod
    def pinned_data(cls, config: BaseModel) -> TaskLists:
        """Its task list and ``open_app`` table, made from the pinned commit in its installation."""
        return TaskLists(_installation(config), SUBSETS, (FAMILY,))

    @classmethod
    def installation(cls, config: BaseModel) -> VenusBenchInstaller:
        """VenusBench's checkout, venv, service app, files and FitBook build, and the AVD it boots (``install.py``)."""
        return VenusBenchInstaller(_installation(config), *named_emulator(config))

    def success(self, verdict: Verdict, result: AgentResult | None) -> float | None:
        """VenusBench's runner's rule, AndroidWorld's: the score counts only when the agent declared the task done.

        With no *result* the run's ending is unknown, so no success is
        claimed either way. VenusBench has no MiniWoB++, whose page alone
        ends its episodes.
        """
        if verdict.status != "judged" or verdict.score is None or result is None:
            return None
        return verdict.score if result.agent_done == AgentDoneStatus.DONE else 0.0


def _installation(config: BaseModel) -> VenusBenchInstallation:
    """The installation the block's setup component looks for: at its ``root``, else the default."""
    root: object = getattr(getattr(config, "setup", None), "root", None)
    if not (root is None or isinstance(root, str)):
        raise TypeError(f"the venusbench setup's root is {root!r}")
    return VenusBenchInstallation.at(root)


__all__ = ["SUBSETS", "VenusBenchBenchmark"]

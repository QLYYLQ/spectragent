"""The Android task's setup: what an Android task asks of its environment (§4.10).

Subclassing ``SetupBase`` registers it in ``TaskSetupRegistrar`` under
``android``, and this package's ``declarations.py`` names it ``TASK_SETUP``. It
imports nothing of the Android implementation, so reading the declarations
stays light.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import JsonValue

from spectragent.schemas.task import SetupBase


class AndroidSetup(SetupBase):
    """An Android task's requirements of its device.

    The device boots from the AVD the provider's configuration names. A
    benchmark task carries its data for its benchmark's setup component in
    ``oracle_task``: AndroidWorld's family, task key, index, suite seed and
    parameters, seed included (``docs/architecture_design.md`` §11.2, "任务裁判与
    ``AndroidSetup``"). The capability passes it to the component it hosts
    unread, and the component refuses data that is not its own; ``None`` is a
    task no benchmark prepares. ``start_app`` is the app the task starts in, by
    a name the run's ``open_app`` takes (the home screen's names, unless a
    benchmark gives its own table), opened before the agent's first step; a
    single task sets it with ``--start-app``.
    """

    __registry_key__ = "android"
    __env_types__: ClassVar[tuple[str, ...]] = ("android",)
    __raw_keys__: ClassVar[tuple[str, ...]] = ("oracle_task", "start_app")

    oracle_task: dict[str, JsonValue] | None = None
    start_app: str | None = None

    def to_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {}
        if self.oracle_task is not None:
            context["oracle_task"] = self.oracle_task
        if self.start_app is not None:
            context["start_app"] = self.start_app
        return context

    @classmethod
    def start_point(cls, context: Mapping[str, Any]) -> str | None:
        """The app the task starts in, as written, ignoring case and extra spaces; ``None`` without one.

        A task from a benchmark that names no start app (AndroidWorld's)
        names no start point (``docs/architecture_design.md`` §4.10
        "任务的起始点归 capability").
        """
        start_app = context.get("start_app")
        if not isinstance(start_app, str) or not start_app.strip():
            return None
        return " ".join(start_app.split()).casefold()

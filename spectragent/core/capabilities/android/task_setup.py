"""The Android task's setup: what an Android task asks of its environment (§4.10).

Subclassing ``SetupBase`` registers it in ``TaskSetupRegistrar`` under
``android``, and this package's ``declarations.py`` names it ``TASK_SETUP``. It
imports nothing of the Android implementation, so reading the declarations
stays light.
"""

from __future__ import annotations

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
    task no benchmark prepares.
    """

    __registry_key__ = "android"
    __env_types__: ClassVar[tuple[str, ...]] = ("android",)
    __raw_keys__: ClassVar[tuple[str, ...]] = ("oracle_task",)

    oracle_task: dict[str, JsonValue] | None = None

    def to_context(self) -> dict[str, Any]:
        return {} if self.oracle_task is None else {"oracle_task": self.oracle_task}

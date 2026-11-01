"""The Android task's setup: what an Android task asks of its environment (§4.10).

Subclassing ``SetupBase`` registers it in ``TaskSetupRegistrar`` under
``android``, and this package's ``declarations.py`` names it ``TASK_SETUP``. It
imports nothing of the Android implementation, so reading the declarations
stays light.
"""

from __future__ import annotations

from typing import Any, ClassVar

from spectragent.schemas.task import SetupBase


class AndroidSetup(SetupBase):
    """An Android task's requirements of its device: none yet.

    The device boots from the AVD the provider's configuration names, so a task
    asks nothing of the capability's ``setup()`` and its context is empty. A
    task oracle that prepares a task's starting state on the device
    (``docs/architecture_design.md`` §11.2, "任务裁判与 ``AndroidSetup``") adds
    its own fields here.
    """

    __registry_key__ = "android"
    __env_types__: ClassVar[tuple[str, ...]] = ("android",)

    def to_context(self) -> dict[str, Any]:
        return {}

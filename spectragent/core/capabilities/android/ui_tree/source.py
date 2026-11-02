"""The UI tree sources (``docs/architecture_design.md`` §11.2 "策略轴"; the axis is in ``_registrar.py``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from spectragent.core.capabilities.android.device.clients import MAX_NODES, TreeCapture
from spectragent.core.capabilities.android.input._registrar import DevicePrograms
from spectragent.core.capabilities.android.ui_tree._registrar import AndroidUITreeSourceBase


@dataclass
class AccessibilityServiceTreeSource(AndroidUITreeSourceBase):
    """The forest our accessibility service reads (``device/forest.py``).

    The full tree: views that are not important for accessibility come with
    it, view ids too, and each node says whether it is important, so a
    cleaner decides what to drop and a recorded tree can be cleaned again
    another way (§11.2, user ruling 2026-11-01). Off-screen nodes are kept up
    to ``max_nodes``: a long list or web page can hold far more nodes than the
    screen shows. Settings' main page took 69 ms for 168 nodes on the API 33
    emulator (2026-11-01).
    """

    __registry_key__ = "accessibility_service"

    max_nodes: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "How many nodes a capture holds before it leaves out nodes the user cannot see, "
                "each with everything under it (counted in the fragment's omitted_offscreen). "
                "Visible nodes are always kept, past the limit too."
            ),
        ),
    ] = MAX_NODES

    async def capture(self, device: DevicePrograms) -> TreeCapture:
        return await device.service.tree(max_nodes=self.max_nodes)

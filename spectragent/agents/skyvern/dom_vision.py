"""``skyvern.dom_vision``: the cleaned element tree plus the screenshot shown to the model.

``click`` stays addressed by element index; ``click_coordinate`` is offered
besides, for a target the element tree gives no index (a widget inside a
cross-origin frame). The member's whole part in coordinates is
``coordinate_space``, the grid its model writes points on; the framework
frames the run's point actions on it, converts every point a reply writes and
writes every point fed back on the frame of its step
(``core/loops/coordinate_frames.py``, ``docs/architecture_design.md`` §4.1
"坐标系归框架").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field as PydanticField

from spectragent.agents.rendering.capture_state import CAPTURE_STATE_KEY
from spectragent.agents.skyvern.family import ImageDetail, SkyvernAgent


@dataclass
class SkyvernDomVisionAgent(SkyvernAgent):
    """Skyvern reading the element tree and the screenshot; clicks by index or by point."""

    __registry_key__ = "skyvern.dom_vision"

    __wiring__: ClassVar[dict[str, Any]] = {
        # ``screenshot`` is required twice over: the model reads it, and the
        # framework lays the coordinate grid over it.
        "observation": {
            "registry": "observation",
            "keys": ["dom", "screenshot", "platform", "task", "browser_meta", "history"],
            "optional_keys": ["step_info", CAPTURE_STATE_KEY],
            "declare_only": True,
        },
        # Index-addressed verbs as in ``skyvern.dom``, plus ``click_coordinate``
        # where the capability offers it. The two click verbs stay distinct:
        # ``click`` takes an index the element tree gives, ``click_coordinate``
        # a point on the screenshot.
        "action": {
            "registry": "action",
            "keys": ["click", "type", "done"],
            "optional_keys": [
                "click_coordinate",
                "scroll",
                "send_keys",
                "go_back",
                "go_forward",
                "select_dropdown",
                "extract_content",
            ],
            "declare_only": True,
        },
    }

    coordinate_space: Annotated[
        Literal["screenshot_pixels", "normalized_1000"],
        PydanticField(
            description=(
                "The grid the model writes points on: 'screenshot_pixels' (pixels "
                "of the screenshot it is shown) or 'normalized_1000' (0-1000 over "
                "the screenshot on both axes, x first). The prompt teaches it; the "
                "framework converts every point the model writes into the units of "
                "the capability's point actions, and writes every point fed back on "
                "this grid. Unused when the run negotiated no point action."
            )
        ),
    ] = "screenshot_pixels"
    vision_detail: Annotated[
        ImageDetail,
        PydanticField(
            description=(
                "Detail the screenshot is sent at: 'auto' (the provider decides), "
                "'low' or 'high'. A point is read off this image, so 'low' makes "
                "click_coordinate guess."
            )
        ),
    ] = "auto"

    def _screenshot_detail(self) -> ImageDetail | None:
        return self.vision_detail

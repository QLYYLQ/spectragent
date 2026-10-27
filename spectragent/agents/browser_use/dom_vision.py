"""``browser_use.dom_vision``: the cleaned DOM text plus the screenshot.

``click`` and ``type`` address an element by its index, as in
``browser_use.dom``. ``click_coordinate`` is offered beside them: a point on
the screenshot reaches what an index cannot (a cross-origin iframe widget).
The two click verbs stay distinct, so neither carries half-inapplicable
params; typing and dragging stay index-addressed, where an index exists.

A run that negotiates ``click_coordinate`` teaches its model a grid
(``coordinate_space``) over the screenshot it is shown; the declaration is
this member's whole part in coordinates (``core/loops/coordinate_frames.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field as PydanticField

from spectragent.agents.browser_use.family import BrowserUseAgent
from spectragent.agents.rendering.capture_state import CAPTURE_STATE_KEY
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    UserMessage,
)
from spectragent.schemas.observation.geometry import CoordinateSpace, PointFrame
from spectragent.schemas.observation.image import image_media_type, read_model_image

#: The paragraph the prompt adds when this run has a coordinate action.
#: Said in code rather than in the surface fragment because it depends on
#: ``coordinate_space``, which is config — the same split UI-TARS makes.
_COORDINATE_GUIDE: Mapping[CoordinateSpace, str] = {
    "screenshot_pixels": (
        "<coordinates>\n"
        "A point is (x, y) in pixels of the screenshot you are shown: (0, 0) "
        "is its top-left corner, x grows to the right and y downwards. Every "
        "step states the screenshot's size in pixels. Write the numbers only "
        "— nothing there names an element, it only says where one is. Every "
        "point in the agent history and the browser state is on this same grid.\n"
        "</coordinates>"
    ),
    "normalized_1000": (
        "<coordinates>\n"
        "A point is (x, y) on a 0-1000 grid laid over the screenshot, whatever "
        "its size in pixels: (0, 0) is the top-left corner, (1000, 1000) the "
        "bottom-right corner, x grows to the right and y downwards. Write the "
        "numbers only — nothing there names an element, it only says where one "
        "is. Every point in the agent history and the browser state is on this "
        "same grid.\n"
        "</coordinates>"
    ),
}


@dataclass
class BrowserUseDomVisionAgent(BrowserUseAgent):
    """BrowserUse reading the cleaned DOM and the screenshot."""

    __registry_key__ = "browser_use.dom_vision"

    _SURFACE: ClassVar[str] = "dom_vision"

    __wiring__: ClassVar[dict[str, Any]] = {
        # The same observation as ``browser_use.dom``; the screenshot that
        # member only records, this one also shows.
        "observation": {
            "registry": "observation",
            "keys": ["dom", "screenshot", "platform", "task", "browser_meta"],
            "optional_keys": ["step_info", "history", CAPTURE_STATE_KEY],
            "declare_only": True,
        },
        "action": {
            "registry": "action",
            "keys": ["navigate", "done", "click", "type"],
            "optional_keys": [
                "click_coordinate",
                "scroll",
                "send_keys",
                "extract_content",
                "go_back",
                "go_forward",
                "switch_tab",
                "wait",
            ],
            "declare_only": True,
        },
    }

    coordinate_space: Annotated[
        Literal["screenshot_pixels", "normalized_1000"],
        PydanticField(
            description=(
                "The grid the model writes points on: 'screenshot_pixels' "
                "(pixels of the screenshot it is shown) or 'normalized_1000' "
                "(0-1000 over the screenshot on both axes, x first). The "
                "prompt teaches it; the framework converts every point the "
                "model writes into the units of the capability's point "
                "actions, and writes every point fed back (the actions run, "
                "their effects, the pointer) on this grid. Unused when this "
                "run negotiated no coordinate action: its feedback keeps "
                "viewport pixels."
            ),
        ),
    ] = "screenshot_pixels"
    vision_detail: Annotated[
        Literal["auto", "low", "high"],
        PydanticField(
            description=(
                "Detail the screenshot is sent at: 'auto' (the default: the "
                "provider decides), 'low' or 'high'. A point is read off this "
                "image, so 'low' makes click_coordinate guess."
            )
        ),
    ] = "auto"

    def _step_messages(self, observation: Any, frame: PointFrame | None) -> list[BaseMessage]:
        """This step's user message: the page as text, with the screenshot attached.

        A step whose screenshot has no bytes to show is sent as text alone,
        as ``browser_use.dom`` sends every step. On the pixel grid the
        browser state ends by stating the screenshot's size; a 0-1000 grid
        is defined over whatever size the image has, so stating the pixels
        would invite the model to answer in them.
        """
        image = read_model_image(getattr(observation, "screenshot", None))
        if image is None:
            return super()._step_messages(observation, frame)
        grid = frame.grid if frame is not None else None
        note = ""
        if grid is not None and grid.space == "screenshot_pixels":
            note = (
                f"\n\nScreenshot: {grid.width}x{grid.height} pixels, showing the "
                f"whole visible viewport."
            )
        body = self._build_step_text(observation, frame, screenshot_note=note)
        media_type = image_media_type(image) or "image/png"
        content: list[Any] = [
            ContentPartTextParam(text=body),
            ContentPartImageParam.from_bytes(
                image, media_type=media_type, detail=self.vision_detail
            ),
        ]
        return [UserMessage(content=content, cache=True)]

    def _render_surface(self, variant: str, fragment_formats: Mapping[str, str]) -> str:
        """The family's surface, then the grid paragraph when this run writes points.

        A run teaches a grid only when the framework framed its action space
        on one: it negotiated ``click_coordinate``.
        """
        surface = super()._render_surface(variant, fragment_formats)
        space = self._action_space.coordinate_space
        return surface if space is None else f"{surface}\n{_COORDINATE_GUIDE[space]}"

"""DragAction — press at one coordinate, travel, release at another.

Coordinate-native like ``click_coordinate``: a drag is a gesture over
pixels, and the agents that emit one (UI-TARS and its computer-use kin)
see pixels, not a selector_map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import PointAxis
from spectragent.core.capabilities.browser.actions._common import (
    effect_from,
    focused_scope,
    handler_field,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.effects import PointerEffect
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import DragEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class DragAction(SettleableAction, ActionBase):
    """Drag from one point of the viewport to another, both in CSS pixels."""

    __registry_key__ = "browser.drag"

    #: The press and release points, what the page hit at the press, and the
    #: focus afterwards.
    Effect = PointerEffect

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        start_x: Annotated[
            int,
            PointAxis("start", "x"),
            PydanticField(description="CSS px from the viewport's left edge"),
        ]
        start_y: Annotated[
            int,
            PointAxis("start", "y"),
            PydanticField(description="CSS px from the viewport's top edge"),
        ]
        end_x: Annotated[
            int,
            PointAxis("end", "x"),
            PydanticField(description="CSS px from the viewport's left edge"),
        ]
        end_y: Annotated[
            int,
            PointAxis("end", "y"),
            PydanticField(description="CSS px from the viewport's top edge"),
        ]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            (
                "Press the mouse at one point in the page viewport, move to "
                "a second one and release — for sliders, canvas selection "
                "and drag-and-drop."
            ),
            **context,
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            scope = focused_scope(capability)
            event = DragEvent(
                start_x=int(params["start_x"]),
                start_y=int(params["start_y"]),
                end_x=int(params["end_x"]),
                end_y=int(params["end_y"]),
            )
            await dispatch_request(scope, event)
            # The handler answers with an ``error`` when the press, the travel or
            # the release was not sent: the drag did not happen, so it failed.
            effect = effect_from(self, handler_field(event, "effect_facts"))
            await self.settle()
            return ActionResult(success=True, effect=effect)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except (TypeError, ValueError) as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

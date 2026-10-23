"""ClickCoordinateAction — click at absolute screen coordinates."""

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
from spectragent.core.capabilities.browser.watchdogs.events import (
    ClickCoordinateEvent,
)
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class ClickCoordinateAction(SettleableAction, ActionBase):
    """Click at a point in the viewport, no element index involved.

    The coordinate space is **CSS pixels of the page viewport**, origin at
    its top-left corner — what ``Input.dispatchMouseEvent`` takes. That is
    *not* necessarily the pixel grid of the screenshot a model looked at:
    the capture is rendered at the device scale factor (see
    :class:`~spectragent.core.capabilities.browser.observations.ScreenshotObs`),
    so an agent that takes a point off the image converts before emitting
    this action. The descriptor says so, because the params it declares are
    the contract every agent has to meet.
    """

    __registry_key__ = "browser.click_coordinate"

    #: Where it pressed, what the page hit there, and the focus afterwards.
    Effect = PointerEffect

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        coordinate_x: Annotated[
            int,
            PointAxis("coordinate", "x"),
            PydanticField(description="CSS px from the viewport's left edge"),
        ]
        coordinate_y: Annotated[
            int,
            PointAxis("coordinate", "y"),
            PydanticField(description="CSS px from the viewport's top edge"),
        ]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Click at a point in the page viewport.", **context)

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            scope = focused_scope(capability)
            event = ClickCoordinateEvent(
                coordinate_x=int(params["coordinate_x"]),
                coordinate_y=int(params["coordinate_y"]),
            )
            await dispatch_request(scope, event)
            # The handler answers with an ``error`` when the mouse input was not
            # sent: the click never happened, so the action failed.
            effect = effect_from(self, handler_field(event, "effect_facts"))
            await self.settle()
            return ActionResult(success=True, effect=effect)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except (TypeError, ValueError) as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

"""ScrollAction — scroll the page, a specific element, or whatever is under a start point."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import AddressesElement, PointAxis
from spectragent.core.capabilities.browser.actions._common import (
    ElementRef,
    effect_from,
    focused_scope,
    handler_field,
    resolve_index,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.effects import ScrollEffect
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import ScrollEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability

#: What a scroll's start does, in the parenthesis of its two params: framing
#: rewrites a point param's description into the model's grid and keeps its
#: parenthesis, so the meaning reaches every model that is offered the start.
_START_HINT = "the pointer moves there, then the wheel turns; never with index"


@dataclass
class ScrollAction(SettleableAction, ActionBase):
    """Scroll with the wheel where the pointer is, inside the element ``index`` names, or at a start point.

    With a start point the pointer moves there first, then the wheel turns
    there (``docs/architecture_design.md`` §4.5, the ``ScrollAction`` example).
    Its effect (:class:`~spectragent.core.capabilities.browser.effects.ScrollEffect`)
    says which container actually moved, its offsets before and after, and
    whether it reached its edge — measured by the executor around the wheel.
    """

    __registry_key__ = "browser.scroll"
    Effect = ScrollEffect

    # ⭐ capability-side configurable side-effect parameters
    default_amount: Annotated[
        int,
        PydanticField(description="Default scroll distance in pixels"),
    ] = 300
    max_amount: Annotated[
        int,
        PydanticField(description="Per-call cap on scroll distance"),
    ] = 2000
    default_direction: Annotated[
        str,
        PydanticField(description="Fallback direction when agent omits one"),
    ] = "down"

    class Params(BaseModel):
        """Scrolling needs no DOM — except to scroll *inside* one element.

        ``index`` is the only param that reads the selector map, and it is
        optional, so a run without ``browser.dom`` keeps the action and
        loses the param: ``describe(fragments=...)`` drops it from both
        the prompt and the schema rather than advertising an index the run
        cannot resolve.

        ``start`` is an optional point, so a run whose agent declares no
        grid (it shows its model no screenshot to point at) keeps the action
        and loses the point (``coordinate_frames.framed_action_space``). A
        scroll names an element or a start point, never both.
        """

        model_config = ConfigDict(extra="forbid")

        direction: Literal["up", "down", "left", "right"]
        amount: int | None = None
        index: Annotated[
            int | None,
            AddressesElement("browser.dom"),
            PydanticField(description="scroll inside specific element"),
        ] = None
        start_x: Annotated[
            int | None,
            PointAxis("start", "x"),
            PydanticField(description="CSS px from the viewport's left edge"),
        ] = None
        start_y: Annotated[
            int | None,
            PointAxis("start", "y"),
            PydanticField(description="CSS px from the viewport's top edge"),
        ] = None

        @model_validator(mode="after")
        def _one_target(self) -> Self:
            if (self.start_x is None) != (self.start_y is None):
                raise ValueError("a scroll start needs both start_x and start_y")
            if self.index is not None and self.start_x is not None:
                raise ValueError("scroll takes an element index or a start point, not both")
            return self

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            (
                f"Scroll the page or an element "
                f"(default {self.default_amount}px {self.default_direction})."
            ),
            hints={"start_x": _START_HINT, "start_y": _START_HINT},
            **context,
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            direction = params.get("direction") or self.default_direction
            amount = min(
                int(params.get("amount") or self.default_amount),
                self.max_amount,
            )
            element: ElementRef | None = None
            if "index" in params and params["index"] is not None:
                element = resolve_index(capability, params["index"])
            scope = focused_scope(capability)
            event = ScrollEvent(
                direction=direction,
                amount=amount,
                backend_node_id=element.backend_node_id if element else None,
                session_id=element.session_id if element else None,
                start_x=params.get("start_x"),
                start_y=params.get("start_y"),
            )
            await dispatch_request(scope, event)
            await self.settle()
            return ActionResult(
                success=True, effect=effect_from(self, handler_field(event, "effect_facts"))
            )
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

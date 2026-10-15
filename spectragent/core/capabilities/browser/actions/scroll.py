"""ScrollAction — scroll the page or a specific element."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import RequiresFragment
from spectragent.core.capabilities.browser.actions._common import (
    ElementRef,
    focused_scope,
    resolve_index,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import ScrollEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class ScrollAction(SettleableAction, ActionBase):
    """Scroll the viewport (or a specific element when ``index`` is given)."""

    __registry_key__ = "browser.scroll"

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
        """

        model_config = ConfigDict(extra="forbid")

        direction: Literal["up", "down", "left", "right"]
        amount: int | None = None
        index: Annotated[
            int | None,
            RequiresFragment("browser.dom"),
            PydanticField(description="scroll inside specific element"),
        ] = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            (
                f"Scroll the page or an element "
                f"(default {self.default_amount}px {self.default_direction})."
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
            )
            await dispatch_request(scope, event)
            await self.settle()
            return ActionResult(success=True)
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

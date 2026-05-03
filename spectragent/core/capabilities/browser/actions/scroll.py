"""ScrollAction — scroll the page or a specific element."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    resolve_index_to_backend_node_id,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
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

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="browser.scroll",
            description=(
                f"Scroll the page or an element "
                f"(default {self.default_amount}px {self.default_direction})."
            ),
            params={
                "direction": "up|down|left|right",
                "amount": "int (optional)",
                "index": "int (optional) -- scroll inside specific element",
            },
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
        session_id: str,
    ) -> ActionResult:
        try:
            direction = params.get("direction") or self.default_direction
            amount = min(
                int(params.get("amount") or self.default_amount),
                self.max_amount,
            )
            backend_node_id: int | None = None
            if "index" in params and params["index"] is not None:
                backend_node_id = resolve_index_to_backend_node_id(
                    capability, session_id, params["index"],
                )
            scope = capability._scopes[session_id]
            event = ScrollEvent(
                direction=direction,
                amount=amount,
                backend_node_id=backend_node_id,
            )
            scope.emit(event)
            await event
            await self.settle()
            return ActionResult(success=True)
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

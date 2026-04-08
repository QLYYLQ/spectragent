"""GoForwardAction — navigate forward in browser history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.watchdogs.events import GoForwardEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class GoForwardAction(ActionBase):
    """Navigate forward in the current tab's history."""

    __registry_key__ = "go_forward"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="go_forward",
            description="Navigate forward in browser history.",
            params={},
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: "BrowserCapability",
        session_id: str,
    ) -> ActionResult:
        try:
            scope = capability._scopes[session_id]
            event = GoForwardEvent()
            scope.emit(event)
            await event
            return ActionResult(success=True)
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

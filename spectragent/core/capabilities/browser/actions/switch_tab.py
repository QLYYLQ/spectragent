"""SwitchTabAction — switch agent focus to a different tab."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.watchdogs.events import SwitchTabEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class SwitchTabAction(ActionBase):
    """Switch the active tab by target_id (``None`` = most recent)."""

    __registry_key__ = "switch_tab"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="switch_tab",
            description="Switch to a different browser tab.",
            params={"target_id": "str | None -- None = most recent"},
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
            event = SwitchTabEvent(target_id=params.get("target_id"))
            scope.emit(event)
            await event
            return ActionResult(success=True)
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

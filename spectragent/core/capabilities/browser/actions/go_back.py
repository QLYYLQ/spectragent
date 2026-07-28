"""GoBackAction — navigate back in browser history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.events import GoBackEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class GoBackAction(SettleableAction, ActionBase):
    """Navigate back in the current tab's history."""

    __registry_key__ = "browser.go_back"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="browser.go_back",
            description="Navigate back in browser history.",
            params={},
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            scope = focused_scope(capability)
            event = GoBackEvent()
            scope.emit(event)
            await event
            await self.settle()
            return ActionResult(success=True)
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

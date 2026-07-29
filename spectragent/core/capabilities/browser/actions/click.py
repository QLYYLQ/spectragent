"""ClickAction — click a DOM element by index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
    resolve_index_to_backend_node_id,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.events import ClickElementEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class ClickAction(SettleableAction, ActionBase):
    """Click a DOM element by its selector_map index."""

    __registry_key__ = "browser.click"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name=self.action_id,
            description="Click a DOM element by its index in the selector map.",
            params={"index": "int -- element index from selector_map"},
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            backend_node_id = resolve_index_to_backend_node_id(
                capability, params["index"],
            )
            scope = focused_scope(capability)
            event = ClickElementEvent(backend_node_id=backend_node_id)
            scope.emit(event)
            await event
            await self.settle()
            return ActionResult(success=True)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

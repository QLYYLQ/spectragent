"""SelectDropdownAction — select an option from a <select> element."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
    resolve_index,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.events import (
    SelectDropdownEvent,
)
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class SelectDropdownAction(SettleableAction, ActionBase):
    """Pick an option from a dropdown by value / visible text / sub-index."""

    __registry_key__ = "browser.select_dropdown"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name=self.action_id,
            description="Select an option from a dropdown element.",
            params={
                "index": "int -- element index of the <select>",
                "value": "str (optional)",
                "text": "str (optional) -- visible option text",
                "option_index": "int (optional) -- 0-based option index",
            },
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            element = resolve_index(capability, params["index"])
            scope = focused_scope(capability)
            event = SelectDropdownEvent(
                backend_node_id=element.backend_node_id,
                session_id=element.session_id,
                value=params.get("value"),
                text=params.get("text"),
                index=params.get("option_index"),
            )
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

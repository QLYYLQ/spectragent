"""TypeAction — type text into a DOM element.

Filename uses ``type_action`` to avoid shadowing the Python ``type``
builtin, while the conscribe registry key stays ``"type"`` to match the
canonical BrowserCapability vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
    handler_error,
    resolve_index,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.events import TypeTextEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class TypeAction(SettleableAction, ActionBase):
    """Type text into a DOM element by its selector_map index."""

    __registry_key__ = "browser.type"

    # ⭐ capability-side configurable side-effect
    clear_by_default: Annotated[
        bool,
        PydanticField(description="Whether to clear the target element first"),
    ] = True

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name=self.action_id,
            description="Type text into a DOM element.",
            params={
                "index": "int -- element index",
                "text": "str",
                # Interpolated, not literal: ``clear_by_default`` is a
                # YAML knob, and this string is what the prompt teaches.
                "clear": f"bool (optional, default={str(self.clear_by_default).lower()})",
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
            text = params.get("text", "")
            clear = bool(params.get("clear", self.clear_by_default))
            scope = focused_scope(capability)
            event = TypeTextEvent(
                backend_node_id=element.backend_node_id,
                session_id=element.session_id,
                text=text,
                clear=clear,
            )
            scope.emit(event)
            await event
            error = handler_error(event)
            if error is not None:
                return ActionResult(success=False, error=error)
            await self.settle()
            return ActionResult(success=True)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

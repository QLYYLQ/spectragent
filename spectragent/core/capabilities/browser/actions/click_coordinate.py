"""ClickCoordinateAction — click at absolute screen coordinates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.watchdogs.events import (
    ClickCoordinateEvent,
)
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class ClickCoordinateAction(ActionBase):
    """Click at absolute viewport coordinates (used by vision-only agents)."""

    __registry_key__ = "click_coordinate"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="click_coordinate",
            description="Click at absolute screen coordinates.",
            params={"coordinate_x": "int", "coordinate_y": "int"},
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
            event = ClickCoordinateEvent(
                coordinate_x=int(params["coordinate_x"]),
                coordinate_y=int(params["coordinate_y"]),
            )
            scope.emit(event)
            await event
            return ActionResult(success=True)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except (TypeError, ValueError) as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

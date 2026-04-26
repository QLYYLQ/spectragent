"""SendKeysAction — send keyboard keys (e.g. Enter, Escape, Ctrl+A)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.watchdogs.events import SendKeysEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class SendKeysAction(ActionBase):
    """Send raw keyboard input (key names or chords)."""

    __registry_key__ = "browser.send_keys"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="browser.send_keys",
            description="Send keyboard keys (e.g. Enter, Escape, Ctrl+A).",
            params={"keys": "str"},
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
        session_id: str,
    ) -> ActionResult:
        try:
            scope = capability._scopes[session_id]
            event = SendKeysEvent(keys=params["keys"])
            scope.emit(event)
            await event
            return ActionResult(success=True)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

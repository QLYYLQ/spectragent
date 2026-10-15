"""SendKeysAction — literal keys and native chords on the measured browser platform."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import SendKeysEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class SendKeysAction(SettleableAction, ActionBase):
    """Send raw keyboard input (key names or chords)."""

    __registry_key__ = "browser.send_keys"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        keys: str

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Send literal keys or chords on the observed browser platform. "
            "For select-all use Meta/Cmd/Command+A on macOS or Ctrl/Control+A on Windows/Linux; "
            "other chords retain their literal modifiers.",
            **context,
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            scope = focused_scope(capability)
            event = SendKeysEvent(keys=params["keys"])
            await dispatch_request(scope, event)
            await self.settle()
            return ActionResult(success=True)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

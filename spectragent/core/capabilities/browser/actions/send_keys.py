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
from spectragent.core.capabilities.browser.input.chords import InvalidKeysError, parse_key_chord
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import SendKeysEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class SendKeysAction(SettleableAction, ActionBase):
    """Send one key or chord: a named key, a single character, or a ``+`` chord.

    Any other string — a URL, a word, a sentence — is refused before a key
    event is dispatched, with an error that tells the model text belongs in
    ``type`` (``docs/architecture_design.md`` §4.13; ``input/chords.py``).
    """

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
            parse_key_chord(str(params["keys"]))
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except InvalidKeysError as exc:
            return ActionResult(success=False, error=str(exc))
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

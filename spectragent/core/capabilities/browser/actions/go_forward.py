"""GoForwardAction — navigate forward in browser history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._browser_commands import history_step
from spectragent.core.capabilities.browser.actions._common import effect_from
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.effects import NavigationEffect
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class GoForwardAction(SettleableAction, ActionBase):
    """Navigate forward in the current tab's history.

    Reports a :class:`NavigationEffect`: the URL before, the URL once the
    history entry committed, and whether that document had finished loading.
    """

    __registry_key__ = "browser.go_forward"
    Effect = NavigationEffect

    class Params(BaseModel):
        """No parameters. Still a model: a provider dialect that cannot
        express an empty object (Gemini's) is the provider's problem to
        solve, not a reason for one action to have no contract."""

        model_config = ConfigDict(extra="forbid")

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Navigate forward in browser history.", **context)

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            facts = await history_step(capability, offset=1)
            await self.settle()
            return ActionResult(success=True, effect=effect_from(self, facts))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

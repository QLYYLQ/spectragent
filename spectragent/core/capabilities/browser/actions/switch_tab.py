"""SwitchTabAction — switch agent focus to a different tab."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._browser_commands import switch_to_tab
from spectragent.core.capabilities.browser.actions._common import effect_from
from spectragent.core.capabilities.browser.effect_probes import ProbeBudget
from spectragent.core.capabilities.browser.effects import TabSwitchEffect
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class SwitchTabAction(ActionBase):
    """Switch the active tab by target_id (``None`` = most recent).

    Delegates to :meth:`BrowserCapability.switch_tab` (through
    ``_browser_commands.switch_to_tab``, which the browser's tab-switching
    shortcuts of ``browser.send_keys`` use too), which owns the
    ``_default_session_id`` that every later observe / act resolves
    against. This action deliberately does not go through the watchdog
    event bus: a watchdog is scoped to a single tab and cannot repoint
    the capability's focus.

    Reports a :class:`TabSwitchEffect`, measured here for the same reason:
    the tab focused before (read off the focused session before switching)
    and after (the tab ``switch_tab`` selected), each with its current
    URL, and whether the newly focused tab's document had finished loading.
    The probes run within ``action_effect_timeout_s`` through the
    capability's public control connection; a probe that fails or runs out
    leaves its fact unknown and never changes the outcome.
    """

    __registry_key__ = "browser.switch_tab"
    Effect = TabSwitchEffect

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        #: Required *and* nullable, so a model always answers the question
        #: and ``null`` is how it says "the most recent one". An optional
        #: param would have let a strict-mode provider decide.
        target_id: Annotated[
            str | None,
            PydanticField(
                description=(
                    "the tab_id shown in the observation's tab list; omit or "
                    "null for the most recently opened tab"
                ),
            ),
        ]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            (
                "Switch to a different browser tab. Subsequent actions and "
                "observations apply to the newly focused tab."
            ),
            **context,
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        budget = ProbeBudget(capability.action_effect_timeout_s)
        try:
            selected, facts = await switch_to_tab(capability, params.get("target_id"), budget)
        except Exception as exc:
            # Unknown tab / single-tab browser / CDP refusal — the agent
            # must learn the switch did not happen, so it can pick another
            # tab rather than acting blind on the old one.
            return ActionResult(success=False, error=str(exc))
        return ActionResult(
            success=True,
            extracted_content=f"Switched to tab {selected.target_id}",
            effect=effect_from(self, facts),
        )

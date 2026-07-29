"""SwitchTabAction — switch agent focus to a different tab."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class SwitchTabAction(ActionBase):
    """Switch the active tab by target_id (``None`` = most recent).

    Delegates to :meth:`BrowserCapability.switch_tab`, which owns the
    ``_default_session_id`` that every later observe / act resolves
    against. This action deliberately does not go through the watchdog
    event bus: a watchdog is scoped to a single tab and cannot repoint
    the capability's focus.
    """

    __registry_key__ = "browser.switch_tab"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name=self.action_id,
            description=(
                "Switch to a different browser tab. Subsequent actions and "
                "observations apply to the newly focused tab."
            ),
            params={
                "target_id": (
                    "str | None -- the tab_id shown in the observation's tab "
                    "list; omit or null for the most recently opened tab"
                ),
            },
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            new_session_id = await capability.switch_tab(params.get("target_id"))
        except Exception as exc:
            # Unknown tab / single-tab browser / CDP refusal — the agent
            # must learn the switch did not happen, so it can pick another
            # tab rather than acting blind on the old one.
            return ActionResult(success=False, error=str(exc))

        target_id = capability._session_to_target.get(new_session_id, new_session_id)
        return ActionResult(
            success=True,
            extracted_content=f"Switched to tab {target_id}",
        )

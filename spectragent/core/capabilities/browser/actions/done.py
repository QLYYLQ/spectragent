"""DoneAction — signal that the task is complete.

Unlike other browser actions, ``DoneAction`` does not emit a CDP event.
It terminates the run by returning ``ActionResult(is_done=True)`` with
the agent-provided answer in ``extracted_content``. This is the single
source of truth for the done-answer payload — no aliasing, no renames.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class DoneAction(ActionBase):
    """Terminal action — carry the final answer out of the loop."""

    __registry_key__ = "done"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="done",
            description="Signal that the task is complete.",
            params={
                "extracted_content": "str | None -- final answer / result text",
                "success": "bool (optional)",
            },
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: "BrowserCapability",
        session_id: str,
    ) -> ActionResult:
        return ActionResult(
            success=bool(params.get("success", True)),
            is_done=True,
            extracted_content=params.get("extracted_content"),
        )

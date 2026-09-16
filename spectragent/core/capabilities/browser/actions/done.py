"""DoneAction — signal that the task is complete.

Unlike other browser actions, ``DoneAction`` does not emit a CDP event.
It terminates the run by returning ``ActionResult(is_done=True)`` with
the agent-provided answer in ``extracted_content``. This is the single
source of truth for the done-answer payload — no aliasing, no renames.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class DoneAction(ActionBase):
    """Terminal action — carry the final answer out of the loop."""

    __registry_key__ = "browser.done"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        #: Required *and* nullable: every run ends by stating its answer,
        #: and ``null`` is the honest answer to a task that has none.
        extracted_content: Annotated[
            str | None, PydanticField(description="final answer / result text")
        ]
        success: bool | None = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Signal that the task is complete.", **context)

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        requested = params.get("success")
        return ActionResult(
            success=True if requested is None else bool(requested),
            is_done=True,
            extracted_content=params.get("extracted_content"),
        )

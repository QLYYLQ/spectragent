"""DoneAction — end the run with the agent's final answer.

Unlike other browser actions, ``DoneAction`` does not emit a CDP event.
It terminates the run by returning ``ActionResult(is_done=True)`` with
the agent-provided answer in ``extracted_content`` and the agent's claim
about the task in ``reported_success``. This is the single source of truth
for the done payload — no aliasing, no renames.

Its ``success`` is ``True`` whenever it executed, like every other action's:
it says the run was closed, not that the task was accomplished. What the
agent says about the task is ``Params.success``, carried out unchanged as
``reported_success`` — ``None`` when the agent did not say
(``docs/architecture_design.md`` §4.1, "agent 的任务结论").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import TaskClaim
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class DoneAction(ActionBase):
    """Terminal action — carry the final answer and the agent's claim out of the loop."""

    __registry_key__ = "browser.done"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        #: Required *and* nullable: every run ends by stating its answer,
        #: and ``null`` is the honest answer to a task that has none.
        extracted_content: Annotated[
            str | None, PydanticField(description="final answer / result text")
        ]
        #: The agent's claim, not a verdict. Unset is a claim of nothing: an
        #: agent whose reply format cannot say it (UI-TARS's ``finished``)
        #: leaves it unset rather than inventing ``true``.
        success: Annotated[
            bool | None,
            TaskClaim(),
            PydanticField(
                description="whether the task was accomplished; leave it unset if you cannot tell"
            ),
        ] = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Signal that the task is complete.", **context)

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        return ActionResult(
            success=True,
            is_done=True,
            extracted_content=params.get("extracted_content"),
            reported_success=params.get("success"),
        )

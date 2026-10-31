"""The actions a Loop provides and executes itself: ``loop.done``.

An action that never directs the environment is not a capability's. ``done``
ends the run with the agent's answer and its claim about the task, so it
belongs to the Loop (the user's ruling of 2026-10-31,
``docs/architecture_design.md`` §4.5 "Loop 提供的动作"). It is registered like
every action — its ``Params`` declared, its descriptor rendered by
``describe`` — and a Loop declares it in ``__loop_actions__``, the twin of
``__loop_fragments__``. ``build_guarded_act`` hands it to the Loop's
``execute_action``; it never reaches ``env.act``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase, ActionRegistrar
from spectragent.core.actions.params import TaskClaim
from spectragent.schemas.action import ActionDescriptor, ActionResult


class LoopActionBase(ActionBase):
    """An action a Loop provides and executes: it directs no environment.

    Executed by the Loop, never against a capability, so :meth:`execute`
    takes its params alone. Its key is ``loop.<name>``: the prefix is what
    routes it to the Loop.
    """

    __abstract__ = True

    async def execute(self, params: dict[str, Any]) -> ActionResult:
        """Execute this action from its validated *params*."""
        raise NotImplementedError(f"{self.__registry_key__} must implement execute()")


@dataclass
class DoneAction(LoopActionBase):
    """End the run with the agent's answer and its claim about the task.

    Its ``success`` is ``True`` whenever it executed, like every other
    action's: it says the run was closed, not that the task was accomplished.
    What the agent says about the task is ``Params.success``, carried out
    unchanged as ``reported_success``, ``None`` when the agent did not say
    (``docs/architecture_design.md`` §4.1, "agent 的任务结论").
    """

    __registry_key__ = "loop.done"

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

    async def execute(self, params: dict[str, Any]) -> ActionResult:
        return ActionResult(
            success=True,
            is_done=True,
            extracted_content=params.get("extracted_content"),
            reported_success=params.get("success"),
        )


#: The actions every Loop built on ``AgentLoopBase`` provides by default
#: (its ``__loop_actions__``), read off the action class so the two cannot drift.
LOOP_ACTION_KEYS: frozenset[str] = frozenset({DoneAction.__registry_key__})


def loop_action(key: str) -> LoopActionBase:
    """A fresh instance of the loop action registered under *key*.

    Raises:
        TypeError: *key* names no registered loop action.
    """
    cls = ActionRegistrar.get_or_none(key)
    if not (isinstance(cls, type) and issubclass(cls, LoopActionBase)):
        raise TypeError(f"{key!r} is not a registered loop action (a LoopActionBase)")
    return cls()

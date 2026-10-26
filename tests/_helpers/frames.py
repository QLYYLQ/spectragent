"""What a ReAct step does with coordinates around an Agent, for tests that drive an Agent by hand.

A run's Loop builds each step's frame from the observation it decides on and
the run's (framed) action space, hands it to the Agent in
``ReactPromptContext.frame``, moves the parsed reply's points into the
capability's units with it and records it on the step
(``spectragent/core/loops/coordinate_frames.py``). A test that calls an
Agent's slots directly does the same here, so the Agent sees what a run shows
it and the actions come out as a run executes them. A reply written as text
is read as a run reads it (``tests/_helpers/replies.py``).
"""

from __future__ import annotations

from typing import Any

from spectragent.core.loops.coordinate_frames import actions_in_capability_units, step_frame
from spectragent.schemas.action import Action
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.replies import read_reply

__all__ = ["decided", "step_context"]


def step_context(
    space: ActionSpace,
    observation: Any,
    *,
    step: int = 0,
    phase: Any = "policy",
    **fields: Any,
) -> ReactPromptContext:
    """The prompt context a Loop hands an Agent for a step deciding on *observation*."""
    return ReactPromptContext(
        step=step, phase=phase, frame=step_frame(space, observation), **fields
    )


def decided(
    agent: Any, space: ActionSpace, reply: Any, context: ReactPromptContext
) -> list[Action]:
    """*reply* read by *agent* and moved off the step's frame, as the Loop executes it."""
    return actions_in_capability_units(read_reply(agent, reply, space), context.frame, space)

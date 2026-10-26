"""A reply read the way a run reads it, for tests that hand an Agent a reply by hand.

A run never hands a structured Agent's parser text: the provider validates a
reply against the Agent's output model, and text (a reply the provider
refused) is validated by the framework, which refuses a misfit in its own
words (``core/loops/react.py::validated_reply``). A test that writes a reply as
JSON text reads it through the same function, so a structured Agent's parser
sees what it sees in a run.
"""

from __future__ import annotations

from typing import Any

from spectragent.core.loops.react import validated_reply
from spectragent.schemas.action import Action
from spectragent.schemas.action_space import ActionSpace
from tests._helpers.action_spaces import space_of

__all__ = ["read_reply"]


def read_reply(agent: Any, reply: object, space: ActionSpace | None = None) -> list[Action]:
    """*agent*'s Actions for *reply*, as its parser gives them to ``ReactLoop.read_reply``.

    Points are left as the reply wrote them; a test of the point conversion
    applies ``actions_in_capability_units`` itself.

    Args:
        agent: A prepared Agent.
        reply: The reply: a validated output model, or text.
        space: The run's action space, which words a refusal; the Agent's own
            descriptors when omitted.

    Raises:
        ParseError: The framework refuses the text (``reply_refusal``), or
            the Agent's parser refuses the reply.
    """
    output_model = (
        agent.get_react_output_model() if hasattr(agent, "get_react_output_model") else None
    )
    if space is None:
        space = space_of(agent.get_action_space())
    return agent.parse_react_output(validated_reply(space, output_model, reply))

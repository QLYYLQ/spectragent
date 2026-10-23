"""Skyvern's reply: one closed schema per run, built from the negotiated actions.

The shape the system prompt teaches (``prompts/agents/skyvern/system_prompt.md``)::

    {"actions": [{"reasoning": "...", "action": {"browser.click": {"index": 42}}}]}

Each item pairs one action with the model's reason for it, which is Skyvern's
own habit: it reasons per action, not per step. The action is one member of
the run's closed action union (``core/actions/action_union.py``): an object
with exactly one key, the negotiated action's fully-qualified name, whose value
is that action's parameters as its descriptor declares them. So the schema can
spell only what this run can execute, each verb with its own parameter types,
and a reply that hands an action a parameter it does not take is refused at
validation, with one line naming the verb, rather than reaching the action.

Finishing is not a separate field. ``done`` is a negotiated action like any
other and is written as one (``{"browser.done": {"extracted_content": ...,
"success": ...}}``), so its parameters are the action's own.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model
from pydantic import Field as PydanticField

from spectragent.core.actions.action_union import action_union
from spectragent.schemas.action import ActionDescriptor

__all__ = ["ACTIONS_FIELD", "REASONING_FIELD", "reply_model"]

#: The reply's list of action items.
ACTIONS_FIELD = "actions"

#: Each item's reason for its action: the thought ``parse_react_thought`` reads.
REASONING_FIELD = "reasoning"

#: ``create_model(name, **{field: (annotation, FieldInfo)})`` is Pydantic's
#: documented dynamic form, which its overloads cannot express.
_create_model: Any = create_model


def reply_model(descriptors: Sequence[ActionDescriptor]) -> type[BaseModel]:
    """This run's reply model: a non-empty list of ``{reasoning, action}`` items.

    Raises:
        ValueError: *descriptors* is empty; a run whose model can name no
            action has no reply to write.
        UnsupportedParamsSchema: An action's params are outside the closed
            union's subset — at ``prepare()``, before the first model call.
    """
    union, _members = action_union(descriptors)
    allowed = ", ".join(descriptor.name for descriptor in descriptors)
    item = _create_model(
        "SkyvernActionForRun",
        __config__=ConfigDict(extra="forbid"),
        **{
            REASONING_FIELD: (
                str,
                PydanticField(
                    default="",
                    description="One short sentence: why this action, and why this element.",
                ),
            ),
            "action": (
                union,
                PydanticField(
                    description=(
                        "One action: an object with exactly one key, the action's name, "
                        f"whose value is the object of its parameters. Names: {allowed}."
                    )
                ),
            ),
        },
    )
    return _create_model(
        "SkyvernOutputForRun",
        # Providers and gateways add metadata of their own to a reply;
        # refusing it would fail a reply whose actions are right.
        __config__=ConfigDict(extra="allow"),
        **{
            ACTIONS_FIELD: (
                list[item],  # type: ignore[valid-type]
                PydanticField(
                    min_length=1,
                    description="The actions to run this step, in order.",
                ),
            ),
        },
    )

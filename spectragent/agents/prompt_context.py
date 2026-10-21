"""Agent-owned message rendering for external instructions and restored facts.

These helpers return messages; they never mutate memory or interpret an old
reply as a new decision. Agents opt into this layout from their public builders.
"""

import json
from collections.abc import Callable
from typing import Any

from spectragent.agents._action_effects import result_facts
from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.schemas.action import Action, PointWriter, write_viewport_point
from spectragent.schemas.prompt import ReactPromptContext, RestoredHistory

#: How an Agent writes an action it shows as a fact: a JSON value.
ActionWriter = Callable[[Action], Any]


def action_json(action: Action) -> dict[str, Any]:
    """The default :data:`ActionWriter`: the action's own JSON, points in viewport units."""
    return action.model_dump(mode="json")


def place_prompt_contributions(
    messages: list[BaseMessage], *, context: ReactPromptContext
) -> list[BaseMessage]:
    """Place system instructions before the observation and turn instructions after it.

    Contributions become ordinary memory messages through the Loop's existing
    message contract. Retention therefore follows the selected Memory strategy.
    With no contribution, the original messages and their bytes are untouched.
    """
    if not context.contributions:
        return messages
    system: list[BaseMessage] = []
    turn: list[BaseMessage] = []
    for contribution in context.contributions:
        text = f"External prompt contribution:\n{contribution.text}"
        if contribution.placement == "system":
            system.append(SystemMessage(content=text))
        else:
            turn.append(UserMessage(content=text))
    return [*system, *messages, *turn]


def restored_history_message(
    history: RestoredHistory,
    *,
    show_action_effects: bool,
    write_action: ActionWriter = action_json,
    write_point: PointWriter = write_viewport_point,
) -> UserMessage:
    """Describe actual restored execution separately from its source material.

    The sensitivity marker is trace-only evidence, just as in the normal
    action-result observation. It must not become model input in this path.
    Each result's effect is the one measured while restoring, never the
    source's; it is shown as its ``describe()`` line when
    *show_action_effects* is on, and its raw fields never are
    (``agents/_action_effects.py``).

    The layout is shared; how an action and a point are written is the
    Agent's. *write_action* writes both the executed and the recorded
    actions, *write_point* every point an effect line states. The defaults
    write the action's JSON and viewport units; an Agent whose model writes
    points on another grid passes writers for that grid (§4.1,
    "模型看到的每个点都写在它写点的那个网格上").
    """
    history.validate_execution()
    facts = {
        "source_material": {
            "run_id": history.source_run_id,
            "step": history.source_step,
            "recorded_reply": history.source_reply,
            "recorded_actions": [write_action(action) for action in history.source_actions],
        },
        "actual_restored_execution": [
            {
                "action": write_action(action),
                "result": result_facts(
                    result, show_effects=show_action_effects, write_point=write_point
                ),
            }
            for action, result in zip(history.actions, history.results, strict=True)
        ],
    }
    return UserMessage(
        content=(
            "Restored execution history: the preceding observation was captured during "
            "this restoration. The actions and results below are what actually executed. "
            "Source material is an older recorded decision, not a new reply or thought "
            "from this Agent.\n" + json.dumps(facts, ensure_ascii=False, sort_keys=True)
        )
    )

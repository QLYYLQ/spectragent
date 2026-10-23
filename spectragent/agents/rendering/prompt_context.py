"""Agent-owned message rendering for external instructions and restored facts.

These helpers return messages; they never mutate memory or interpret an old
reply as a new decision. Agents opt into this layout from their public builders.
"""

import json
from collections.abc import Callable
from typing import Any

from spectragent.agents.rendering.action_effects import result_facts
from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.core.loops.coordinate_frames import fact_in_model_frame, point_writer
from spectragent.schemas.action import Action
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.prompt import ReactPromptContext, RestoredHistory

#: How an Agent writes an action it shows as a fact, its points already on the
#: model's frame: a JSON value.
ActionWriter = Callable[[Action], Any]


def action_json(action: Action) -> dict[str, Any]:
    """The default :data:`ActionWriter`: the action's own JSON."""
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
    context: ReactPromptContext,
    action_space: ActionSpace,
    write_action: ActionWriter = action_json,
    max_content_chars: int | None = None,
) -> UserMessage:
    """Describe actual restored execution separately from its source material.

    The sensitivity marker is trace-only evidence, just as in the normal
    action-result observation. It must not become model input in this path.
    Each result's effect is the one measured while restoring, never the
    source's; it is shown as its ``describe()`` line when
    *show_action_effects* is on, and its raw fields never are
    (``agents/rendering/action_effects.py``).

    Every point is written on the restored step's frame (``context.frame``):
    the restored actions ran after the observation it lies over was captured.
    The framework puts each action's points there (``fact_in_model_frame``,
    with *action_space* saying which params are points) and writes each
    effect line's (``point_writer``); a run framed on no grid keeps the
    capability's units (``docs/architecture_design.md`` §4.1 "坐标系归框架").
    The layout is shared; how an action reads is the Agent's: *write_action*
    writes both the executed and the recorded actions, the action's JSON by
    default. *max_content_chars* bounds each result's returned content as
    ``result_facts`` does, so an Agent whose live history bounds it shows a
    restored result the same way.
    """
    history.validate_execution()
    frame = context.frame

    def written(action: Action) -> Any:
        return write_action(fact_in_model_frame(action, frame, action_space))

    write_point = point_writer(frame)
    facts = {
        "source_material": {
            "run_id": history.source_run_id,
            "step": history.source_step,
            "recorded_reply": history.source_reply,
            "recorded_actions": [written(action) for action in history.source_actions],
        },
        "actual_restored_execution": [
            {
                "action": written(action),
                "result": result_facts(
                    result,
                    show_effects=show_action_effects,
                    write_point=write_point,
                    max_content_chars=max_content_chars,
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

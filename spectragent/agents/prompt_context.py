"""Agent-owned message rendering for external instructions and restored facts.

These helpers return messages; they never mutate memory or interpret an old
reply as a new decision. Agents opt into this layout from their public builders.
"""

import json

from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.schemas.prompt import ReactPromptContext, RestoredHistory


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


def restored_history_message(history: RestoredHistory) -> UserMessage:
    """Describe actual restored execution separately from its source material.

    The sensitivity marker is trace-only evidence, just as in the normal
    action-result observation. It must not become model input in this path.
    """
    history.validate_execution()
    facts = {
        "source_material": {
            "run_id": history.source_run_id,
            "step": history.source_step,
            "recorded_reply": history.source_reply,
            "recorded_actions": [
                action.model_dump(mode="json") for action in history.source_actions
            ],
        },
        "actual_restored_execution": [
            {
                "action": action.model_dump(mode="json"),
                "result": result.model_dump(mode="json", exclude={"typed_into_sensitive_field"}),
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

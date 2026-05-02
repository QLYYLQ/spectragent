"""Structured output Pydantic models for each agent's LLM responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── browser-use ────────────────────────────────────────────────
#
# Shape mirrors the upstream browser-use prompt template
# (spectragent/prompts/browser/agents/browser_use/system_prompt*.md):
#
#     {
#       "thinking": "...",
#       "evaluation_previous_goal": "...",
#       "memory": "...",
#       "next_goal": "...",
#       "current_plan_item": 0,           // optional
#       "plan_update": ["..."],           // optional
#       "action": [
#         {"navigate": {"url": "..."}},
#         {"click_element": {"index": 5}}
#       ]
#     }
#
# Each action is a single-key dict where the key is the action name
# (browser-use vocabulary) and the value is its params dict.
# ``BrowserUseAgent.parse_react_output`` translates these to the
# canonical ``browser.*`` action names before they reach guarded_act.


class BrowserUseOutput(BaseModel):
    """Structured output for BrowserUseAgent (matches the prompt template)."""

    # Allow extra fields so providers/proxies that inject extra metadata
    # (e.g. anthropic 'thought_signature') don't break validation.
    model_config = ConfigDict(extra="allow")

    thinking: str = Field(default="", description="Structured reasoning block")
    evaluation_previous_goal: str = Field(
        default="", description="One-sentence analysis of the previous action."
    )
    memory: str = Field(
        default="",
        description="1-3 sentences of step-level memory and overall progress.",
    )
    next_goal: str = Field(
        default="", description="Immediate next goal in one sentence."
    )
    current_plan_item: int | None = Field(
        default=None, description="Optional plan-item index."
    )
    plan_update: list[str] | None = Field(
        default=None, description="Optional updated plan."
    )
    action: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Action list. Each item is a single-key dict where the key is "
            "the action name (browser-use vocabulary) and the value is the "
            "params dict, e.g. ``{'navigate': {'url': 'https://...'}}``."
        ),
    )


# ── UI-TARS ────────────────────────────────────────────────────


class UITarsOutput(BaseModel):
    """Structured output for UITarsAgent.

    UI-TARS produces a thought followed by a single action
    with screen coordinates.
    """

    thought: str = Field(description="Chain-of-thought reasoning")
    action_type: str = Field(description="Action type (click, type, scroll, etc.)")
    point: str | None = Field(default=None, description="Screen coordinates as '(x, y)'")
    text: str | None = Field(default=None, description="Text to type or content")


# ── Skyvern ────────────────────────────────────────────────────


class SkyvernActionItem(BaseModel):
    """A single action within a Skyvern step."""

    action_type: str = Field(description="Action type")
    element_id: str | None = Field(default=None, description="Target element ID from element tree")
    text: str | None = Field(default=None, description="Text to type (if applicable)")
    reasoning: str = Field(default="", description="Why this action was chosen")


class SkyvernOutput(BaseModel):
    """Structured output for SkyvernAgent."""

    actions: list[SkyvernActionItem] = Field(
        description="One or more actions to execute"
    )
    is_done: bool = Field(default=False, description="Whether the task is complete")
    extracted_content: str | None = Field(default=None, description="Extracted answer if done")

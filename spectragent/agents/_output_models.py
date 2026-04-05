"""Structured output Pydantic models for each agent's LLM responses."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── browser-use ────────────────────────────────────────────────


class BrowserUseStateAssessment(BaseModel):
    """LLM's assessment of the current page state."""

    evaluation_previous_goal: str = Field(default="", description="Did the previous action succeed?")
    memory: str = Field(default="", description="Running memory / notes")
    next_goal: str = Field(default="", description="What to do next")


class BrowserUseActionItem(BaseModel):
    """A single action within a browser-use multi-action step.

    Uses explicit fields instead of a generic dict to ensure
    strict JSON schema compatibility with API proxies.
    """

    action_name: str = Field(description="Name of the action to execute (e.g. click_element, input_text, scroll, go_to_url, done)")
    index: int | None = Field(default=None, description="DOM element index (if applicable)")
    text: str | None = Field(default=None, description="Text to type or answer content")
    url: str | None = Field(default=None, description="URL to navigate to")
    direction: str | None = Field(default=None, description="Scroll direction: up or down")
    success: bool | None = Field(default=None, description="Whether done was successful (for done action)")


class BrowserUseOutput(BaseModel):
    """Structured output for BrowserUseAgent.

    Supports multi-action: the LLM may return several actions per step.
    """

    current_state: BrowserUseStateAssessment
    actions: list[BrowserUseActionItem] = Field(
        description="One or more actions to execute"
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

"""Structured output Pydantic models for each agent's LLM responses."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, Field

# ── browser-use ────────────────────────────────────────────────
#
# There is no static ``BrowserUseOutput``. The shape of a reply is a
# function of the template the run selected, and the two used to be
# written out twice — one model carrying every field any template might
# teach, and eight ``<output>`` blocks each teaching a subset. Under
# OpenAI's strict dialect that stops being cosmetic: strict makes *every*
# property required, so a flash run whose prompt asks for ``memory`` and
# ``action`` was forced to emit ``thinking``, ``next_goal`` and a plan it
# was never told about.
#
# So the fields are declared here per variant, in the order that variant's
# ``<output>`` block lists them, and
# ``BrowserUseAgent._build_per_run_output_model`` composes them with the
# closed action union (``agents/_action_union.py``).
# ``tests/test_agents/test_browser_use_modes.py`` reads the keys back out
# of each rendered template and asserts they are exactly these.

_FIELDS: Mapping[str, tuple[Any, Any]] = MappingProxyType(
    {
        # Reasoning fields keep a default. Under a strict dialect the
        # optimizer makes every property required anyway, so the default
        # costs nothing there; on the *non*-strict paths it is what keeps a
        # model that skipped a prose field from failing a reply whose
        # actions are perfectly good. The action list has no default — that
        # one really is mandatory.
        "thinking": (
            str,
            Field(default="", description="Structured reasoning block"),
        ),
        "evaluation_previous_goal": (
            str,
            Field(
                default="",
                description="One-sentence analysis of the previous action.",
            ),
        ),
        "memory": (
            str,
            Field(
                default="",
                description=("1-3 sentences of step-level memory and overall progress."),
            ),
        ),
        "next_goal": (
            str,
            Field(default="", description="Immediate next goal in one sentence."),
        ),
        "current_plan_item": (
            int | None,
            Field(default=None, description="Optional plan-item index."),
        ),
        "plan_update": (
            list[str] | None,
            Field(default=None, description="Optional updated plan."),
        ),
    }
)

#: Template stem → the reasoning fields its ``<output>`` block teaches, in
#: the order it lists them. The action list is not here: every variant
#: carries one, and its type is built per run from the negotiated verbs.
REASONING_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "system_prompt": (
            "thinking",
            "evaluation_previous_goal",
            "memory",
            "next_goal",
            "current_plan_item",
            "plan_update",
        ),
        "system_prompt_no_thinking": (
            "evaluation_previous_goal",
            "memory",
            "next_goal",
            "current_plan_item",
            "plan_update",
        ),
        "system_prompt_flash": ("memory",),
        "system_prompt_flash_anthropic": ("memory",),
        "system_prompt_browser_use": (
            "thinking",
            "evaluation_previous_goal",
            "memory",
            "next_goal",
        ),
        "system_prompt_browser_use_flash": ("memory",),
        "system_prompt_browser_use_no_thinking": (
            "evaluation_previous_goal",
            "memory",
            "next_goal",
        ),
    }
)


def reasoning_fields(variant: str) -> dict[str, tuple[Any, Any]]:
    """``create_model`` field definitions for one template variant.

    Raises:
        KeyError: The variant has no declared output shape. That is a
            template added without saying what it teaches — caught here
            rather than by a model that silently asks for nothing.
    """
    names = REASONING_FIELDS[variant]
    return {name: _FIELDS[name] for name in names}


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
#
# Shape mirrors spectragent/prompts/agents/skyvern/system_prompt.md,
# which is the only template SkyvernAgent loads. The two are pinned together
# by tests/test_agents/test_skyvern_prompt_action_vocabulary.py: every verb
# the template teaches has to survive ``parse_react_output`` and be accepted
# by ``guarded_act``.
#
# The per-item fields are exactly the union of the params the negotiated
# browser actions need and cannot default: ``index`` addresses the element
# (BrowserCapability's selector_map is index-keyed — see
# ``actions/_common.resolve_index``), ``text`` types or
# selects, ``direction`` steers a scroll, ``keys`` drives send_keys. Anything
# else the capability exposes has a usable default, so widening this model is
# a decision to make deliberately, not a reflex.


class SkyvernActionItem(BaseModel):
    """A single action within a Skyvern step."""

    action_type: str = Field(description="Canonical action name, e.g. 'browser.click'")
    index: int | None = Field(
        default=None,
        description="Target element index from the element tree; null if the action takes no element",
    )
    text: str | None = Field(
        default=None, description="Text to type, or visible option label to select"
    )
    direction: str | None = Field(
        default=None, description="Scroll direction: up | down | left | right"
    )
    keys: str | None = Field(default=None, description="Keyboard keys to send, e.g. 'Enter'")
    reasoning: str = Field(default="", description="Why this action was chosen")


class SkyvernOutput(BaseModel):
    """Structured output for SkyvernAgent."""

    actions: list[SkyvernActionItem] = Field(
        description="Actions to execute this step; empty when is_done is true"
    )
    is_done: bool = Field(
        default=False,
        description="True to end the run — this is how browser.done is invoked",
    )
    success: bool = Field(
        default=True,
        description="Whether the goal was reached; only meaningful when is_done is true",
    )
    extracted_content: str | None = Field(
        default=None,
        description="Final answer / extracted data when is_done is true, or the reason for giving up",
    )

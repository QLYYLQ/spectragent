"""The reasoning fields of BrowserUse's structured reply, per template variant."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import Field

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
# closed action union (``core/actions/action_union.py``).
# ``tests/test_agents/test_browser_use/test_browser_use_modes.py`` reads the keys back out
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

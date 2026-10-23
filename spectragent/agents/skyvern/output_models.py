"""Structured output Pydantic models for SkyvernAgent's LLM responses."""

from __future__ import annotations

from pydantic import BaseModel, Field

# Shape mirrors spectragent/prompts/agents/skyvern/system_prompt.md,
# which is the only template SkyvernAgent loads. The two are pinned together
# by tests/test_agents/test_skyvern/test_skyvern_prompt_action_vocabulary.py: every verb
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
    #: The agent's claim, carried into ``browser.done``'s ``success``. No
    #: default of ``true``: a reply that leaves it out claims nothing, and the
    #: run's ``agent_reported_success`` is then ``None``.
    success: bool | None = Field(
        default=None,
        description=(
            "Whether the goal was reached; only meaningful when is_done is true. "
            "null if you cannot tell"
        ),
    )
    extracted_content: str | None = Field(
        default=None,
        description="Final answer / extracted data when is_done is true, or the reason for giving up",
    )

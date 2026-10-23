"""``skyvern.dom``: the cleaned element tree only, every action addressed by element index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from spectragent.agents.rendering.capture_state import CAPTURE_STATE_KEY
from spectragent.agents.skyvern.family import SkyvernAgent


@dataclass
class SkyvernDomAgent(SkyvernAgent):
    """Skyvern reading the element tree; the screenshot is recorded, never shown."""

    __registry_key__ = "skyvern.dom"

    # Capability-relative keys: the agent declares domain verbs, and which
    # capability's ``click`` they mean is decided at negotiation.
    __wiring__: ClassVar[dict[str, Any]] = {
        # ``screenshot`` is required although the model never sees it: the
        # trace keeps it, and evaluators score runs from it. ``browser_meta``
        # (URL, title, tabs) is the page this browser agent reads; ``history``
        # is the Loop's record of what ran, the action-history window's
        # source; ``capture_state`` is optional, as for every agent
        # (``docs/architecture_design.md`` §4.4, "观测增强按消费方分层").
        "observation": {
            "registry": "observation",
            "keys": ["dom", "screenshot", "platform", "task", "browser_meta", "history"],
            "optional_keys": ["step_info", CAPTURE_STATE_KEY],
            "declare_only": True,
        },
        # Index-addressed only: a point action reads a point off an image
        # this member never shows its model.
        "action": {
            "registry": "action",
            "keys": ["click", "type", "done"],
            "optional_keys": [
                "scroll",
                "send_keys",
                "go_back",
                "go_forward",
                "select_dropdown",
                "extract_content",
            ],
            "declare_only": True,
        },
    }

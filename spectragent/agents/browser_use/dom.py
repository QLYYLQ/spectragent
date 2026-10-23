"""``browser_use.dom``: the page as its cleaned DOM text, every action addressed by index.

The screenshot is captured and recorded (evaluators score from it) but never
shown to the model, so no action takes a point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from spectragent.agents.browser_use.family import BrowserUseAgent
from spectragent.agents.rendering.capture_state import CAPTURE_STATE_KEY


@dataclass
class BrowserUseDomAgent(BrowserUseAgent):
    """BrowserUse reading the cleaned DOM only."""

    __registry_key__ = "browser_use.dom"

    _SURFACE: ClassVar[str] = "dom"

    __wiring__: ClassVar[dict[str, Any]] = {
        # ``screenshot`` is required because the evaluators read it out of
        # the trace, not because the model sees it. ``browser_meta`` (URL,
        # title, tabs) is required because this is a browser agent: it
        # never reads a page without knowing which page. ``capture_state``
        # is the platform-neutral capture fact every agent declares
        # optional; ``show_capture_state`` may exclude it
        # (``docs/architecture_design.md`` §4.4, "观测增强按消费方分层").
        "observation": {
            "registry": "observation",
            "keys": ["dom", "screenshot", "platform", "task", "browser_meta"],
            "optional_keys": ["step_info", "history", CAPTURE_STATE_KEY],
            "declare_only": True,
        },
        "action": {
            "registry": "action",
            "keys": ["navigate", "done", "click", "type"],
            "optional_keys": [
                "scroll",
                "send_keys",
                "extract_content",
                "go_back",
                "go_forward",
                "switch_tab",
                "wait",
            ],
            "declare_only": True,
        },
    }

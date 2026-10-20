"""Render the browser's observed input platform, never the agent host's OS."""

from typing import Any

from spectragent.agents._input_platform import keyboard_rules, platform_fragment


def render_browser_platform(observation: Any) -> str:
    """The required relative ``platform`` fragment and literal keyboard semantics.

    For browser-specific Agents (Skyvern, BrowserUse). A cross-platform
    computer-use Agent renders the same fragment with
    ``_input_platform.render_computer_use_platform``. Negotiation normally
    guarantees this fragment; an observation without it raises rather than
    acquiring a platform.
    """
    _fragment, operating_system, label = platform_fragment(observation)
    return (
        "<input_platform>\n"
        f"Actual browser input platform: {label} ({operating_system}).\n"
        f"{keyboard_rules(operating_system, label)}\n"
        "</input_platform>"
    )

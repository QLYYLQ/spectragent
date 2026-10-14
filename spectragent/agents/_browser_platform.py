"""Render the browser's observed input platform, never the agent host's OS."""

from typing import Any


def render_browser_platform(observation: Any) -> str:
    """The required relative ``platform`` fragment and literal keyboard semantics.

    Negotiation normally guarantees this fragment. An old trace or a
    hand-built observation without it must not silently acquire a platform.
    The structural field is sufficient; agents do not import a concrete
    capability or resolver class.
    """
    fragment = getattr(observation, "platform", None)
    operating_system = getattr(fragment, "operating_system", None)
    labels = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}
    if not isinstance(operating_system, str) or operating_system not in labels:
        raise ValueError(
            "Browser input platform observation is missing or invalid; "
            "platform.operating_system must be macos, windows or linux."
        )
    label = labels[operating_system]
    shortcut = (
        "On macOS, Command is Meta: use Meta+A for Select All. "
        "Ctrl+A still sends Control+A; it is not rewritten to Command+A."
        if operating_system == "macos"
        else f"On {label}, use Control+A for Select All. "
        "Meta is a different modifier; it is not rewritten to Control."
    )
    return (
        "<input_platform>\n"
        f"Actual browser input platform: {label} ({operating_system}).\n"
        "Keyboard modifiers are literal: Ctrl/Control sends Control; "
        "Cmd/Command/Meta sends Meta. "
        f"{shortcut}\n"
        "</input_platform>"
    )

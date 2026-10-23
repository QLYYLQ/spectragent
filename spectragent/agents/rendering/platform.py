"""The input platform an Agent's keystrokes reach: measured, never the agent host's OS.

Two renderings of the one required relative ``platform`` fragment, with the
same literal keyboard rules. :func:`render_computer_use_platform` is for a
cross-platform computer-use Agent (UI-TARS): it names the environment it
operates from the fragment's own key (``browser.platform`` → ``browser``), so
the Agent imports no browser type and the same words serve a desktop or an
Android environment. Browser-specific Agents use
:func:`render_browser_platform`.

Negotiation normally guarantees the fragment. An old trace or a hand-built
observation without it must not silently acquire a platform. The structural
fields are sufficient; agents do not import a concrete capability or resolver.
"""

from typing import Any

_LABELS = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}


def platform_fragment(observation: Any) -> tuple[Any, str, str]:
    """The ``platform`` fragment, its operating system and display label.

    Raises:
        ValueError: The fragment is missing or names no supported system.
    """
    fragment = getattr(observation, "platform", None)
    operating_system = getattr(fragment, "operating_system", None)
    if not isinstance(operating_system, str) or operating_system not in _LABELS:
        raise ValueError(
            "Browser input platform observation is missing or invalid; "
            "platform.operating_system must be macos, windows or linux."
        )
    return fragment, operating_system, _LABELS[operating_system]


def keyboard_rules(operating_system: str, label: str) -> str:
    """The literal-modifier rule and the platform's own select-all chord, in one line."""
    shortcut = (
        "On macOS, Command is Meta: use Meta+A for Select All. "
        "Ctrl+A still sends Control+A; it is not rewritten to Command+A."
        if operating_system == "macos"
        else f"On {label}, use Control+A for Select All. "
        "Meta is a different modifier; it is not rewritten to Control."
    )
    return (
        "Keyboard modifiers are literal: Ctrl/Control sends Control; "
        f"Cmd/Command/Meta sends Meta. {shortcut}"
    )


def render_computer_use_platform(observation: Any) -> str:
    """The environment a computer-use Agent operates, its input platform and keyboard rules."""
    fragment, operating_system, label = platform_fragment(observation)
    key = getattr(fragment, "fragment_id", "")
    environment = key.split(".", 1)[0] if isinstance(key, str) and "." in key else ""
    if not environment.isidentifier():
        raise ValueError(
            "The platform observation names no environment; its fragment_id must be "
            "'<environment>.platform'."
        )
    return (
        "<input_platform>\n"
        f"Computer-use agent; environment: {environment}.\n"
        f"Actual input platform: {label} ({operating_system}).\n"
        f"{keyboard_rules(operating_system, label)}\n"
        "</input_platform>"
    )


def render_browser_platform(observation: Any) -> str:
    """The required relative ``platform`` fragment and literal keyboard semantics.

    For browser-specific Agents (Skyvern, BrowserUse). A cross-platform
    computer-use Agent renders the same fragment with
    :func:`render_computer_use_platform`. Negotiation normally
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


__all__ = [
    "keyboard_rules",
    "platform_fragment",
    "render_browser_platform",
    "render_computer_use_platform",
]

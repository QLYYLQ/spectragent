"""The input platform an Agent's keystrokes reach: measured, never the agent host's OS.

Two renderings of the one required relative ``platform`` fragment, with the
same literal keyboard rules. :func:`render_computer_use_platform` is for a
cross-platform computer-use Agent (UI-TARS): it names the environment it
operates from the fragment's own key (``browser.platform`` → ``browser``), so
the Agent imports no browser type and the same words serve a desktop or an
Android environment. Browser-specific Agents use
:func:`render_browser_platform`.

Each rendering states a fixed set of platforms: :data:`COMPUTER_USE_PLATFORMS`
is every input platform, Android with a phone's touch keyboard rules;
:data:`BROWSER_PLATFORMS` is a desktop browser's, since a mobile browser is
not designed (``docs/architecture_design.md`` §11.2). An Agent declares the set
of the rendering it calls as ``__input_platforms__``, and
``AgentRuntime.prepare()`` refuses a run whose platform fragment may report a
platform outside it (``core/negotiation/platform.py``), before anything is set
up.

Negotiation normally guarantees the fragment. An old trace or a hand-built
observation without it, or with a platform the rendering does not state, must
not silently acquire one: the rendering raises. The structural fields are
sufficient; agents do not import a concrete capability or resolver.
"""

from collections.abc import Set
from typing import Any, get_args

from spectragent.schemas.observation.platform import InputPlatform

_LABELS: dict[InputPlatform, str] = {
    "macos": "macOS",
    "windows": "Windows",
    "linux": "Linux",
    "android": "Android",
}

#: Every platform :func:`render_computer_use_platform` states.
COMPUTER_USE_PLATFORMS: frozenset[InputPlatform] = frozenset(get_args(InputPlatform))
#: Every platform :func:`render_browser_platform` states: a desktop browser's.
BROWSER_PLATFORMS: frozenset[InputPlatform] = frozenset({"macos", "windows", "linux"})


def platform_fragment(
    observation: Any, platforms: Set[InputPlatform]
) -> tuple[Any, InputPlatform, str]:
    """The ``platform`` fragment, its operating system and display label.

    Raises:
        ValueError: The fragment is missing or names no system of *platforms*.
    """
    fragment = getattr(observation, "platform", None)
    operating_system = getattr(fragment, "operating_system", None)
    for platform in platforms:
        if operating_system == platform:
            return fragment, platform, _LABELS[platform]
    stated = ", ".join(p for p in get_args(InputPlatform) if p in platforms)
    raise ValueError(
        "The input platform observation is missing or invalid; "
        f"platform.operating_system must be one of {stated}."
    )


def keyboard_rules(operating_system: InputPlatform, label: str) -> str:
    """What a keystroke does on *operating_system*, in one line: modifiers and Select All.

    A desktop's modifiers are literal, with its own select-all chord. A phone
    has an input method's on-screen keyboard and no Control or Meta at all.
    """
    if operating_system == "android":
        return (
            "On Android, text goes in through an input method's on-screen keyboard, which "
            "can cover part of the screen while a text field has focus. There are no Control "
            "or Meta modifiers, so no Control+A or Meta+A selects all."
        )
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
    """The environment a computer-use Agent operates, its input platform and keyboard rules.

    States every input platform (:data:`COMPUTER_USE_PLATFORMS`).
    """
    fragment, operating_system, label = platform_fragment(observation, COMPUTER_USE_PLATFORMS)
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

    For browser-specific Agents (Skyvern, BrowserUse), on a desktop browser
    (:data:`BROWSER_PLATFORMS`). A cross-platform computer-use Agent renders
    the same fragment with :func:`render_computer_use_platform`. Negotiation
    normally guarantees this fragment; an observation without it raises
    rather than acquiring a platform.
    """
    _fragment, operating_system, label = platform_fragment(observation, BROWSER_PLATFORMS)
    return (
        "<input_platform>\n"
        f"Actual browser input platform: {label} ({operating_system}).\n"
        f"{keyboard_rules(operating_system, label)}\n"
        "</input_platform>"
    )


__all__ = [
    "BROWSER_PLATFORMS",
    "COMPUTER_USE_PLATFORMS",
    "keyboard_rules",
    "platform_fragment",
    "render_browser_platform",
    "render_computer_use_platform",
]

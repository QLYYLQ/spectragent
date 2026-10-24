"""Which ``send_keys`` chords are the browser's own shortcuts, per input platform.

``docs/architecture_design.md`` §4.13 (user ruling 2026-10-24): a small, fixed,
named table — not inference of what an arbitrary chord was meant to do. Key
events af dispatches reach only the page; Chrome's own accelerators never run
(``input/keyboard.py``, "No native key code"). So on the measured input
platform, a chord in :data:`COMMANDS` runs its browser command through the code
the matching action uses (``actions/_browser_commands.py``), and a chord in
:data:`BROWSER_UI` — a shortcut for browser UI this headless browser does not
show — goes to the page as any key does, its effect saying what it is. A chord
that is either only on *another* platform is matched too, so the effect can
name the measured platform's binding; it is never run, and another platform's
modifiers are never rewritten.

The bindings are Chrome's, as its keyboard shortcuts page lists them
(https://support.google.com/chrome/answer/157179, checked 2026-10-24) and as
its accelerator tables define them, which the page does not list in full
(Chromium ``chrome/browser/ui/cocoa/accelerators_cocoa.mm`` and
``chrome/browser/global_keyboard_shortcuts_mac.mm`` for macOS,
``chrome/browser/ui/accelerator_table.cc`` for Windows and Linux):

==================  ==========================================  ===========================================
Command             macOS                                       Windows / Linux
==================  ==========================================  ===========================================
Back                Meta+[, Meta+ArrowLeft                      Alt+ArrowLeft
Forward             Meta+], Meta+ArrowRight                     Alt+ArrowRight
Reload              Meta+R                                      Control+R, F5
Hard reload         Meta+Shift+R                                Control+Shift+R, Shift+F5, Control+F5
Close tab           Meta+W                                      Control+W, Control+F4
Next tab            Control+Tab, Meta+Shift+], Control+PageDown,  Control+Tab, Control+PageDown
                    Meta+Alt+ArrowRight
Previous tab        Control+Shift+Tab, Meta+Shift+[,            Control+Shift+Tab, Control+PageUp
                    Control+PageUp, Meta+Alt+ArrowLeft
Tab 1-8             Meta+1 … Meta+8                             Control+1 … Control+8 (Linux also Alt+1 … 8)
Last tab            Meta+9                                      Control+9 (Linux also Alt+9)
==================  ==========================================  ===========================================

Browser UI, not run: the address bar (macOS Meta+L; Windows/Linux Control+L,
Alt+D, F6, and Control+K / Control+E, its search), a new tab (Meta+T /
Control+T), reopening a closed tab (Meta+Shift+T / Control+Shift+T), a new or
incognito window (Meta+N, Meta+Shift+N / Control+N, Control+Shift+N), the
history page (Meta+Y / Control+H), the downloads page (Meta+Shift+J,
Meta+Alt+L / Control+J), the bookmarks manager (Meta+Alt+B / Control+Shift+O)
and the find bar (Meta+F / Control+F, F3).

macOS's Meta+ArrowLeft / Meta+ArrowRight are Chrome's "delayed" shortcuts:
Chrome goes back or forward only when the page did not use the key, and in a
text field the key moves the caret to the start or end of the line. So they
run only when nothing that takes text has focus (:attr:`Shortcut.yields_to_text`);
otherwise the key goes to the page. A chord typed with Shift and a bracket is
matched by the key under it (``Meta+Shift+]`` and ``Meta+Shift+}`` are one
chord); letters match in either case.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import get_args

from spectragent.core.capabilities.browser.effects import BrowserCommand, BrowserUiCommand
from spectragent.core.capabilities.browser.input.chords import KeyChord, parse_key_chord
from spectragent.core.capabilities.browser.platform import InputPlatform

#: A chord as the table matches it: its modifiers and its key.
ChordKey = tuple[frozenset[str], str]

#: Shifted characters on a key the table names, as the key under them.
_SHIFTED_KEYS = {"{": "[", "}": "]"}


@dataclass(frozen=True)
class Shortcut:
    """A browser command a chord runs on one platform.

    ``tab``: the 1-based position for ``select_tab``. ``yields_to_text``: the
    chord runs only when nothing that takes text has focus (module docstring).
    """

    command: BrowserCommand
    tab: int | None = None
    yields_to_text: bool = False


@dataclass(frozen=True)
class Runs:
    """The chord is the measured platform's shortcut for a command the capability runs."""

    shortcut: Shortcut


@dataclass(frozen=True)
class WithoutUi:
    """The chord is the measured platform's shortcut for browser UI this browser does not show."""

    command: BrowserUiCommand


@dataclass(frozen=True)
class OtherPlatform:
    """The chord is a browser shortcut on another platform only.

    ``binding``: the chord the measured platform has for the same command.
    """

    command: BrowserCommand | BrowserUiCommand
    tab: int | None
    binding: str


ShortcutMatch = Runs | WithoutUi | OtherPlatform


def chord_key(chord: KeyChord) -> ChordKey:
    """How the table matches *chord*: its modifiers, and its key in one spelling."""
    key = str(chord.payload["key"])
    if len(key) == 1:
        key = key.lower()
        if "Shift" in chord.modifiers:
            key = _SHIFTED_KEYS.get(key, key)
    return frozenset(chord.modifiers), key


def _keys(*spellings: str) -> list[ChordKey]:
    return [chord_key(parse_key_chord(spelling)) for spelling in spellings]


def _table(
    entries: Iterable[tuple[Shortcut, list[str]]],
) -> dict[ChordKey, Shortcut]:
    table: dict[ChordKey, Shortcut] = {}
    for shortcut, spellings in entries:
        for key in _keys(*spellings):
            assert key not in table, f"{key} bound twice"
            table[key] = shortcut
    return table


def _numbered(modifier: str) -> list[tuple[Shortcut, list[str]]]:
    return [
        *((Shortcut("select_tab", tab=n), [f"{modifier}+{n}"]) for n in range(1, 9)),
        (Shortcut("last_tab"), [f"{modifier}+9"]),
    ]


_MACOS: list[tuple[Shortcut, list[str]]] = [
    (Shortcut("back"), ["Meta+["]),
    (Shortcut("back", yields_to_text=True), ["Meta+ArrowLeft"]),
    (Shortcut("forward"), ["Meta+]"]),
    (Shortcut("forward", yields_to_text=True), ["Meta+ArrowRight"]),
    (Shortcut("reload"), ["Meta+R"]),
    (Shortcut("hard_reload"), ["Meta+Shift+R"]),
    (Shortcut("close_tab"), ["Meta+W"]),
    (
        Shortcut("next_tab"),
        ["Control+Tab", "Meta+Shift+]", "Control+PageDown", "Meta+Alt+ArrowRight"],
    ),
    (
        Shortcut("previous_tab"),
        ["Control+Shift+Tab", "Meta+Shift+[", "Control+PageUp", "Meta+Alt+ArrowLeft"],
    ),
    *_numbered("Meta"),
]
_WINDOWS: list[tuple[Shortcut, list[str]]] = [
    (Shortcut("back"), ["Alt+ArrowLeft"]),
    (Shortcut("forward"), ["Alt+ArrowRight"]),
    (Shortcut("reload"), ["Control+R", "F5"]),
    (Shortcut("hard_reload"), ["Control+Shift+R", "Shift+F5", "Control+F5"]),
    (Shortcut("close_tab"), ["Control+W", "Control+F4"]),
    (Shortcut("next_tab"), ["Control+Tab", "Control+PageDown"]),
    (Shortcut("previous_tab"), ["Control+Shift+Tab", "Control+PageUp"]),
    *_numbered("Control"),
]

#: Each platform's shortcuts for the commands the capability runs.
COMMANDS: dict[InputPlatform, dict[ChordKey, Shortcut]] = {
    "macos": _table(_MACOS),
    "windows": _table(_WINDOWS),
    "linux": _table([*_WINDOWS, *_numbered("Alt")]),
}

_UI_MACOS: dict[BrowserUiCommand, list[str]] = {
    "address_bar": ["Meta+L"],
    "new_tab": ["Meta+T"],
    "reopen_closed_tab": ["Meta+Shift+T"],
    "new_window": ["Meta+N", "Meta+Shift+N"],
    "history": ["Meta+Y"],
    "downloads": ["Meta+Shift+J", "Meta+Alt+L"],
    "bookmarks": ["Meta+Alt+B"],
    "find": ["Meta+F"],
}
_UI_WINDOWS: dict[BrowserUiCommand, list[str]] = {
    "address_bar": ["Control+L", "Alt+D", "F6", "Control+K", "Control+E"],
    "new_tab": ["Control+T"],
    "reopen_closed_tab": ["Control+Shift+T"],
    "new_window": ["Control+N", "Control+Shift+N"],
    "history": ["Control+H"],
    "downloads": ["Control+J"],
    "bookmarks": ["Control+Shift+O"],
    "find": ["Control+F", "F3"],
}


def _ui_table(entries: dict[BrowserUiCommand, list[str]]) -> dict[ChordKey, BrowserUiCommand]:
    table: dict[ChordKey, BrowserUiCommand] = {}
    for command, spellings in entries.items():
        for key in _keys(*spellings):
            assert key not in table, f"{key} bound twice"
            table[key] = command
    return table


#: Each platform's shortcuts for browser UI this browser does not show.
BROWSER_UI: dict[InputPlatform, dict[ChordKey, BrowserUiCommand]] = {
    "macos": _ui_table(_UI_MACOS),
    "windows": _ui_table(_UI_WINDOWS),
    "linux": _ui_table(_UI_WINDOWS),
}

#: The chord each platform's effect names for a command: its first binding.
_BINDINGS: dict[InputPlatform, dict[tuple[str, int | None], str]] = {
    "macos": {
        **{(s.command, s.tab): spellings[0] for s, spellings in reversed(_MACOS)},
        **{(command, None): spellings[0] for command, spellings in _UI_MACOS.items()},
    },
    "windows": {
        **{(s.command, s.tab): spellings[0] for s, spellings in reversed(_WINDOWS)},
        **{(command, None): spellings[0] for command, spellings in _UI_WINDOWS.items()},
    },
}
_BINDINGS["linux"] = _BINDINGS["windows"]


def binding(platform: InputPlatform, command: str, tab: int | None = None) -> str:
    """The chord *platform*'s effect line names for *command* (``Meta+[`` for Back on macOS).

    Written canonically, as the effect writes the chord pressed
    (:attr:`KeyChord.text`: ``Shift+Meta+R``).
    """
    return parse_key_chord(_BINDINGS[platform][(command, tab)]).text


def browser_shortcut(chord: KeyChord, platform: InputPlatform) -> ShortcutMatch | None:
    """What *chord* is to the browser on *platform*; ``None`` when no browser shortcut of the table.

    The measured platform's own shortcuts come first; another platform's are
    only reported (:class:`OtherPlatform`), with the measured platform's
    binding for the same command.
    """
    key = chord_key(chord)
    own = COMMANDS[platform].get(key)
    if own is not None:
        return Runs(own)
    ui = BROWSER_UI[platform].get(key)
    if ui is not None:
        return WithoutUi(ui)
    for other in get_args(InputPlatform):
        if other == platform:
            continue
        shortcut = COMMANDS[other].get(key)
        if shortcut is not None:
            return OtherPlatform(
                shortcut.command, shortcut.tab, binding(platform, shortcut.command, shortcut.tab)
            )
        ui = BROWSER_UI[other].get(key)
        if ui is not None:
            return OtherPlatform(ui, None, binding(platform, ui))
    return None


__all__ = [
    "BROWSER_UI",
    "COMMANDS",
    "ChordKey",
    "OtherPlatform",
    "Runs",
    "Shortcut",
    "ShortcutMatch",
    "WithoutUi",
    "binding",
    "browser_shortcut",
    "chord_key",
]

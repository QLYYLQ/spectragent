"""What ``browser.send_keys`` accepts: a named key, a single character, or a chord.

``docs/architecture_design.md`` §4.13: ``send_keys`` takes exactly three kinds
of input — a named key (``Enter``, ``ArrowDown``, ``F5``), a single character
(``a``, ``%``), or modifiers joined to one of those with ``+``
(``Control+A``, ``Shift+Tab``). The plus key itself is the character ``+``:
alone (``+``), or as the last key of a chord (``Control++``). Anything else is
refused before a single key event is sent, with an error that says text
belongs in ``type``.

The rule exists because the old executor sent any unknown multi-character
string as one key event whose ``key`` was the whole string and which carried
no text: a model that wrote a whole URL into ``send_keys`` (Bilibili, step 20
of a 2026-10-19 recording) was told the action succeeded while nothing was
typed. An unchanged page is never an input error; a string that is not a key
is.

:func:`parse_key_chord` is the one parser. The action refuses with it before
it emits anything, and the executor (``ActionWatchdog._on_send_keys``) builds
its key events from the same result, so the two cannot disagree about what a
key is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from spectragent.core.capabilities.browser.input.keyboard import (
    ALT_MODIFIER,
    CONTROL_MODIFIER,
    META_MODIFIER,
    SHIFT_MODIFIER,
)

# Key name → (DOM ``key``, DOM ``code``, Windows virtual key code, optional
# ``char`` text) for CDP Input.dispatchKeyEvent. The 4th element, when not
# None, means this key has a text interpretation and a separate
# ``type=char`` event is sandwiched between keyDown and keyUp; without that
# event Chrome won't fire ``keypress`` and many JS handlers (Amazon's
# search-on-Enter, autosuggest selectors, contenteditable inputs) treat the
# press as half-hearted and skip their reaction. Two-event keyDown + keyUp is
# enough for navigation keys (arrows, Esc, Home/End, Pg*). No key event
# carries a ``nativeVirtualKeyCode``: the ``input/keyboard.py`` module
# docstring ("No native key code") says what it did on macOS.
KEY_INFO: dict[str, tuple[str, str, int, str | None]] = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, "\t"),
    "escape": ("Escape", "Escape", 27, None),
    "backspace": ("Backspace", "Backspace", 8, None),
    "delete": ("Delete", "Delete", 46, None),
    "insert": ("Insert", "Insert", 45, None),
    "arrowup": ("ArrowUp", "ArrowUp", 38, None),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, None),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, None),
    "arrowright": ("ArrowRight", "ArrowRight", 39, None),
    "home": ("Home", "Home", 36, None),
    "end": ("End", "End", 35, None),
    "pageup": ("PageUp", "PageUp", 33, None),
    "pagedown": ("PageDown", "PageDown", 34, None),
    "space": (" ", "Space", 32, " "),
    "shift": ("Shift", "ShiftLeft", 16, None),
    "control": ("Control", "ControlLeft", 17, None),
    "alt": ("Alt", "AltLeft", 18, None),
    "meta": ("Meta", "MetaLeft", 91, None),
    **{f"f{n}": (f"F{n}", f"F{n}", 111 + n, None) for n in range(1, 13)},
}

#: Other spellings of a named key that agents write (UI-TARS's ``esc``, a
#: bare arrow direction). Each names exactly one key of :data:`KEY_INFO`.
KEY_ALIASES: dict[str, str] = {
    "esc": "escape",
    "return": "enter",
    "up": "arrowup",
    "down": "arrowdown",
    "left": "arrowleft",
    "right": "arrowright",
    "ctrl": "control",
    "cmd": "meta",
    "command": "meta",
    "option": "alt",
}

#: Modifier spelling → (canonical name, CDP modifier bit).
MODIFIERS: dict[str, tuple[str, int]] = {
    "ctrl": ("Control", CONTROL_MODIFIER),
    "control": ("Control", CONTROL_MODIFIER),
    "alt": ("Alt", ALT_MODIFIER),
    "option": ("Alt", ALT_MODIFIER),
    "shift": ("Shift", SHIFT_MODIFIER),
    "meta": ("Meta", META_MODIFIER),
    "cmd": ("Meta", META_MODIFIER),
    "command": ("Meta", META_MODIFIER),
}

#: The order modifiers are written in a chord's canonical text.
_MODIFIER_ORDER = ("Control", "Alt", "Shift", "Meta")
#: How much of a refused input the error repeats back.
_ECHO_CHARS = 80
#: Whitespace around a ``+``, dropped before the plus key is recognised.
_PLUS_SPACING = re.compile(r"\s*\+\s*")


class InvalidKeysError(ValueError):
    """The input is not a key or a key chord; the message tells the model what to write instead."""


@dataclass(frozen=True)
class KeyChord:
    """One parsed ``send_keys`` input: modifiers and the key they are held with.

    ``payload`` is the shared part of the CDP key event (``key``, ``code``,
    ``windowsVirtualKeyCode``); ``char_text`` is the text a ``type=char``
    event carries between keyDown and keyUp, ``None`` for a key that types
    nothing. ``modifier_mask`` is the CDP modifier bits.
    """

    modifiers: tuple[str, ...]
    modifier_mask: int
    payload: dict[str, Any]
    char_text: str | None

    @property
    def text(self) -> str:
        """The chord as written canonically: ``Control+Shift+A``, ``Enter``, ``a``.

        A letter held with Control, Alt or Meta is written in capitals, the
        way shortcuts are named (``Control+a`` and ``Control+A`` are one
        chord); a letter alone, or with Shift only, is written as given,
        because there its case is the character typed.
        """
        key = str(self.payload["key"])
        name = "Space" if key == " " else key
        if len(name) == 1 and name.isalpha() and set(self.modifiers) - {"Shift"}:
            name = name.upper()
        return "+".join([*self.modifiers, name])


def _refuse(raw: str, why: str) -> InvalidKeysError:
    shown = raw if len(raw) <= _ECHO_CHARS else raw[: _ECHO_CHARS - 3] + "..."
    return InvalidKeysError(
        f"send_keys accepts only a key or a key chord: a named key (Enter, Escape, ArrowDown, "
        f"F5, ...), a single character, or modifiers joined to one with '+' (Control+A, "
        f"Shift+Tab; the plus key is '+', or 'Control++' in a chord). {why} (got {shown!r}). "
        "To enter text, use type."
    )


def _key(raw_key: str) -> tuple[dict[str, Any], str | None] | None:
    """The CDP payload and char text of one key name or character; ``None`` when it is neither."""
    name = KEY_ALIASES.get(raw_key.lower(), raw_key.lower())
    info = KEY_INFO.get(name)
    if info is not None:
        key, code, vk, char = info
        return {"key": key, "code": code, "windowsVirtualKeyCode": vk}, char
    if len(raw_key) == 1:
        upper = raw_key.upper()
        if raw_key.isalpha():
            code = f"Key{upper}"
        elif raw_key.isdigit():
            code = f"Digit{raw_key}"
        else:
            code = upper
        return {"key": raw_key, "code": code, "windowsVirtualKeyCode": ord(upper)}, raw_key
    return None


def parse_key_chord(raw: str) -> KeyChord:
    """Parse one ``send_keys`` input, or refuse it before anything is dispatched.

    Named keys and modifier names are case-insensitive. A single character
    is sent as itself: ``"A"`` is the key ``A`` with its text. Whitespace
    around each ``+``-separated part is ignored. ``+`` alone is the plus key,
    and so is a final ``++`` (``Control++``: Control held with the plus key);
    a ``+`` anywhere else separates the parts.

    Raises:
        InvalidKeysError: an empty input or chord part (``ctrl+``, ``+a``,
            ``++``), an unknown modifier, or a final part that is neither a
            named key nor one character — a URL, a word, a sentence.
    """
    compact = _PLUS_SPACING.sub("+", raw.strip())
    if compact == "+":
        parts = ["+"]
    elif compact.endswith("++"):
        parts = [part.strip() for part in compact[:-2].split("+")] + ["+"]
    else:
        parts = [part.strip() for part in raw.split("+")]
    if not all(parts):
        raise _refuse(raw, "Key names and chord parts must not be empty")
    *modifier_names, key_name = parts
    mask = 0
    modifiers: set[str] = set()
    for name in modifier_names:
        modifier = MODIFIERS.get(name.lower())
        if modifier is None:
            raise _refuse(
                raw,
                f"A part before the last '+' is not a modifier (expected one of {sorted(MODIFIERS)})",
            )
        modifiers.add(modifier[0])
        mask |= modifier[1]
    resolved = _key(key_name)
    if resolved is None:
        raise _refuse(raw, "The key is neither a named key nor a single character")
    payload, char_text = resolved
    return KeyChord(
        modifiers=tuple(name for name in _MODIFIER_ORDER if name in modifiers),
        modifier_mask=mask,
        payload=payload,
        char_text=char_text,
    )


__all__ = [
    "KEY_ALIASES",
    "KEY_INFO",
    "MODIFIERS",
    "InvalidKeysError",
    "KeyChord",
    "parse_key_chord",
]

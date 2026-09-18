"""HumanKeyboard — the keystroke middle layer.

A plain controller (NOT an agent-cdp watchdog) that owns a typing-rhythm
strategy and performs every text entry by driving the agent-cdp CDP client
directly. ``ActionWatchdog`` holds one per tab and calls it directly, exactly
as it holds one ``HumanMouse``.

Why a direct-call controller rather than an event-driven watchdog: agent-cdp's
scope event loop is a single-consumer FIFO that runs each handler to
completion before dequeuing the next event, so a watchdog handler that
emits-and-awaits a sub-event deadlocks until ``event_timeout``. Typing is
driven from inside ``ActionWatchdog._on_type_text``, which is such a handler.

Which CDP call, and why it matters
----------------------------------
``Input.insertText`` is the whole string in one round trip. Measured against
headless Chrome, a page listening on an ``<input>`` sees **only**
``beforeinput`` and ``input``, once, with ``data`` equal to the entire
string: no ``keydown``, no ``keypress``, no ``keyup``, and therefore no
inter-key intervals at all. Every keystroke-dynamics collector reads
``keydown``/``keyup``, so that path is not "fast typing", it is *no typing* —
a field that fills instantly with no key events is one of the cheapest
automation signals there is.

``Input.dispatchKeyEvent`` with ``type: "keyDown"`` and a ``text`` field
produces the real sequence — ``keydown``, ``keypress``, ``beforeinput``,
``input`` on the press and ``keyup`` at its own time — with correct ``key``,
``code`` and ``keyCode``. That is the path a humanized rhythm has to use, and
the cost is one round trip per event instead of one per string: typing a
20-character query takes ~20 x (flight + hold) ≈ 4-5 s where the bulk insert
took ~15 ms. That is the trade, and it is why ``instant`` remains the default.

(The ``keyDown`` + separate ``type: "char"`` + ``keyUp`` shape that
``_on_send_keys`` uses for named keys is deliberately not used here: measured,
its ``keypress`` arrives with ``key`` and ``code`` **empty**, which no
physical keyboard produces.)

Non-ASCII, and why CJK is explicitly different
----------------------------------------------
A Chinese character is not typed by pressing a key that bears it: it is
composed through an IME, which fires ``keydown`` with ``key: "Process"`` and
``keyCode: 229`` for each *pinyin* keystroke plus ``compositionstart`` /
``compositionupdate`` / ``compositionend``, and only then ``input`` with the
committed characters. Dispatching a ``keydown`` whose ``key`` is ``"你"``
(measured: ``code`` empty, ``keyCode`` 0) is not a closer imitation of that,
it is a contradiction — no physical key could produce it.

So a character with no US-layout key is delivered by its own
``Input.insertText``, one per character, **on the rhythm's schedule**. The
page sees ``beforeinput`` / ``input`` per character at human intervals, which
is a strict subset of what a real IME commit emits rather than a
contradiction of it. What is still missing is the composition events and the
229 keydowns; ``Input.imeSetComposition`` could supply them, but only by
inventing the pinyin the user never typed, so it is deliberately not done
here. **A page that gates on ``keydown`` sees nothing for CJK text.**
"""

from __future__ import annotations

import logging
from typing import Any

from agent_cdp import CDPCommandProtocol

from spectragent.core.capabilities.browser.input._schedule import play_on_deadline
from spectragent.core.capabilities.browser.input.instant_rhythm import InstantRhythm
from spectragent.core.capabilities.browser.input.rhythm import TypingRhythmProtocol

logger = logging.getLogger(__name__)

#: CDP ``modifiers`` bit for Shift.
_SHIFT = 8

#: US-layout punctuation reachable without Shift: char → (code, virtual key).
_PUNCTUATION_KEYS: dict[str, tuple[str, int]] = {
    "`": ("Backquote", 192),
    "-": ("Minus", 189),
    "=": ("Equal", 187),
    "[": ("BracketLeft", 219),
    "]": ("BracketRight", 221),
    "\\": ("Backslash", 220),
    ";": ("Semicolon", 186),
    "'": ("Quote", 222),
    ",": ("Comma", 188),
    ".": ("Period", 190),
    "/": ("Slash", 191),
}

#: Shifted character → the unshifted character on the same physical key, so
#: ``code`` and ``keyCode`` report the *key*, which is what they mean.
_SHIFT_PAIRS: dict[str, str] = {
    "~": "`",
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
}

#: Control characters a typed string can carry, as their own keys.
_CONTROL_KEYS: dict[str, tuple[str, str, int, str]] = {
    "\n": ("Enter", "Enter", 13, "\r"),
    "\r": ("Enter", "Enter", 13, "\r"),
    "\t": ("Tab", "Tab", 9, "\t"),
}


def _key_event(ch: str) -> tuple[dict[str, Any], str] | None:
    """CDP key-event fields for *ch*, or ``None`` if no US key bears it.

    Returns ``(shared_payload, text)``: the caller wraps the payload with
    ``type: "keyDown"`` (adding ``text``, which is what makes Chrome fire
    ``keypress`` and ``input``) and ``type: "keyUp"``.

    ``None`` means the character has to go through ``Input.insertText`` —
    every non-ASCII character, which on a real keyboard would have come from
    an IME rather than a key. See the module docstring.
    """
    control = _CONTROL_KEYS.get(ch)
    if control is not None:
        key, code, vk, text = control
        return (
            {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk},
            text,
        )
    if len(ch) != 1 or not (" " <= ch <= "~"):
        return None

    modifiers = 0
    if ch == " ":
        code, vk = "Space", 32
    elif ch.isalpha():
        upper = ch.upper()
        code, vk = f"Key{upper}", ord(upper)
        if ch.isupper():
            modifiers = _SHIFT
    elif ch.isdigit():
        code, vk = f"Digit{ch}", ord(ch)
    elif ch in _PUNCTUATION_KEYS:
        code, vk = _PUNCTUATION_KEYS[ch]
    elif ch in _SHIFT_PAIRS:
        base = _SHIFT_PAIRS[ch]
        modifiers = _SHIFT
        if base.isdigit():
            code, vk = f"Digit{base}", ord(base)
        else:
            code, vk = _PUNCTUATION_KEYS[base]
    else:  # pragma: no cover — the printable ASCII range is covered above
        return None

    payload: dict[str, Any] = {
        "key": ch,
        "code": code,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
    }
    if modifiers:
        # The Shift *key* is not dispatched as its own event: the rhythm
        # models one press per character, and a listener reads shiftKey off
        # the character's own event anyway. ``shift_factor`` is what carries
        # the cost of reaching for it into the timing.
        payload["modifiers"] = modifiers
    return payload, ch


class HumanKeyboard:
    """Owns a typing-rhythm strategy; performs every text entry.

    One instance per tab. The session is per call rather than per instance
    (unlike :class:`HumanMouse`): keystrokes follow *focus*, and focus can
    be inside an out-of-process iframe, whose node is addressed through its
    own CDP target.
    """

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        rhythm: TypingRhythmProtocol | None = None,
    ) -> None:
        self._cdp = cdp
        # Nothing configured → the no-op strategy, exactly the legacy bulk
        # insert. The strategy alone decides whether we humanize.
        self._rhythm: TypingRhythmProtocol = rhythm if rhythm is not None else InstantRhythm()
        self._humanized = self._rhythm.humanized

    @property
    def humanized(self) -> bool:
        return self._humanized

    async def type_text(self, text: str, *, session_id: str | None = None) -> None:
        """Enter *text* into whatever currently has focus.

        Off (``instant``, and the fallback when nothing is configured): one
        ``Input.insertText`` carrying the whole string — one CDP call, no key
        events, byte-for-byte the pre-rhythm behaviour. On: a real
        ``keydown``/``keyup`` pair per character, played against absolute
        deadlines so the flight and hold times the strategy drew are the ones
        the page observes, not those plus a CDP round trip each.

        A character with no US-layout key (any non-ASCII, so all CJK) is sent
        as its own ``Input.insertText`` in its own slot: same rhythm, no key
        events. See the module docstring for why that is the honest choice.

        Under a transport slower than the schedule, a slot whose time is gone
        is not dropped the way a cursor waypoint is — a dropped ``keyUp``
        leaves the key down forever. Its events ride along with the next slot
        that is dispatched, so the typing coarsens rather than corrupting,
        and the last slot is never dropped.
        """
        if not self._humanized or not text:
            await self._cdp.send("Input.insertText", {"text": text}, session_id=session_id)
            return

        keystrokes = self._rhythm.generate(text)
        if len(keystrokes) != len(text) or any(
            k.char != ch for k, ch in zip(keystrokes, text, strict=False)
        ):
            raise ValueError(
                f"{type(self._rhythm).__name__}.generate() must return one "
                f"Keystroke per character of the text it was given, carrying "
                f"those characters unchanged — a rhythm decides when, never "
                f"what. Got {len(keystrokes)} keystrokes for {len(text)} "
                f"characters."
            )

        # Two slots per character — press, then dwell — so the schedule's
        # total is the string's typing duration whichever delivery a
        # character takes. A character delivered by ``insertText`` has no
        # release to send, so its dwell slot spends its time and sends
        # nothing, the way a repeated waypoint holds the cursor still.
        schedule: list[float] = []
        events: list[tuple[str, dict[str, Any]] | None] = []
        for keystroke in keystrokes:
            resolved = _key_event(keystroke.char)
            schedule.append(keystroke.gap_ms)
            schedule.append(keystroke.hold_ms)
            if resolved is None:
                events.append(("Input.insertText", {"text": keystroke.char}))
                events.append(None)
            else:
                payload, key_text = resolved
                events.append(
                    ("Input.dispatchKeyEvent", {**payload, "type": "keyDown", "text": key_text})
                )
                events.append(("Input.dispatchKeyEvent", {**payload, "type": "keyUp"}))

        unsent = 0
        async for i in play_on_deadline(schedule):
            for event in events[unsent : i + 1]:
                if event is None:
                    continue
                method, params = event
                await self._cdp.send(method, params, session_id=session_id)
            unsent = i + 1

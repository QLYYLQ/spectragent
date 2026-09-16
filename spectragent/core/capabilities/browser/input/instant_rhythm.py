"""InstantRhythm — the default no-op typing strategy.

Zero timing for every character. With this strategy ``HumanKeyboard`` sends
the whole string in one ``Input.insertText`` — one CDP call, no key events —
which is byte-for-byte the pre-rhythm behaviour.

It is the only way to switch keystroke humanization off, and it is also what
runs when no strategy is configured at all (a hand-built capability, no
Composer). The keystrokes it returns are honest rather than empty: they say
what the timing *would* be if they were dispatched, which is nothing.
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.input.rhythm import (
    Keystroke,
    TypingRhythmBase,
)


class InstantRhythm(TypingRhythmBase):
    """No per-key timing == one bulk insert (current behavior, the default)."""

    __registry_key__ = "instant"

    def generate(self, text: str) -> list[Keystroke]:
        return [Keystroke(char=ch) for ch in text]

    @property
    def humanized(self) -> bool:
        return False

"""HumanRhythm — per-character keystroke timing drawn from a typing model.

Not a fixed delay between keys. A fixed delay is its own tell: real typing
has a *distribution* with a long right tail, and structure inside it —
the hand slows at a word boundary, slows more around punctuation, pays for a
Shift, and occasionally stops altogether while the typist thinks or reads.
A collector of keystroke dynamics measures exactly those two quantities, per
key: the **flight time** (previous release → this press) and the **hold
time** (this press → this release). Both are modelled here.

The shape of the model
----------------------
Each character's press-to-press interval is drawn from a lognormal — the
distribution inter-key intervals actually follow, because it is bounded
below, unimodal and right-skewed, which a normal is not and a uniform is
emphatically not. ``median_iki_ms`` sets its median and ``iki_sigma`` the
weight of the tail (0.5 puts the 95th percentile at ~2.3x the median). The
median is then scaled per character before the draw, so the tail scales with
it rather than being added to it:

- a space costs ``space_factor`` — the word boundary is where a typist's
  hands re-position;
- a punctuation character, *and the character after one*, cost
  ``punctuation_factor`` — the reach out and the sentence break back;
- a character needing Shift costs ``shift_factor``.

On top of that, ``pause_probability`` of characters carry a long pause
(uniform in ``[pause_min_ms, pause_max_ms]``) — the typist reading back what
they wrote, or looking at the screen.

The hold time is its own, much tighter lognormal around
``median_hold_ms``; the flight time is what is left of the press-to-press
interval once the previous key's hold is subtracted, floored at
``min_gap_ms`` so a fast draw can never produce a release *after* the next
press.

Everything is drawn from a per-instance ``random.Random(seed)``: no global
state, and the same seed replays the same typing.
"""

from __future__ import annotations

import math
import random

from spectragent.core.capabilities.browser.input.rhythm import (
    Keystroke,
    TypingRhythmBase,
)

#: Characters treated as punctuation for timing. CJK marks included: this
#: strategy times a Chinese string too, even though its characters are
#: delivered differently (see ``HumanKeyboard``).
_PUNCTUATION = frozenset(".,;:!?'\"()[]{}<>/\\-_=+@#$%^&*|~`" "。，、；：！？（）【】《》…—“”‘’")

#: Characters a US layout needs Shift for, beyond the uppercase letters.
_SHIFTED = frozenset('~!@#$%^&*()_+{}|:"<>?')


class HumanRhythm(TypingRhythmBase):
    """Lognormal inter-key intervals with word/punctuation/pause structure."""

    __registry_key__ = "human"

    def __init__(
        self,
        *,
        median_iki_ms: float = 170.0,
        iki_sigma: float = 0.5,
        min_gap_ms: float = 25.0,
        max_gap_ms: float = 3000.0,
        space_factor: float = 1.45,
        punctuation_factor: float = 1.9,
        shift_factor: float = 1.25,
        pause_probability: float = 0.02,
        pause_min_ms: float = 350.0,
        pause_max_ms: float = 1200.0,
        median_hold_ms: float = 85.0,
        hold_sigma: float = 0.22,
        min_hold_ms: float = 20.0,
        max_hold_ms: float = 300.0,
        seed: int | None = None,
    ) -> None:
        self._median_iki = max(1.0, median_iki_ms)
        self._iki_sigma = max(0.0, iki_sigma)
        self._min_gap = max(0.0, min_gap_ms)
        self._max_gap = max(self._min_gap, max_gap_ms)
        self._space_factor = space_factor
        self._punctuation_factor = punctuation_factor
        self._shift_factor = shift_factor
        self._pause_probability = min(1.0, max(0.0, pause_probability))
        self._pause_min = max(0.0, pause_min_ms)
        self._pause_max = max(self._pause_min, pause_max_ms)
        self._median_hold = max(1.0, median_hold_ms)
        self._hold_sigma = max(0.0, hold_sigma)
        self._min_hold = max(0.0, min_hold_ms)
        self._max_hold = max(self._min_hold, max_hold_ms)
        self._rng = random.Random(seed)

    # ── the model ─────────────────────────────────────────────────────

    def _factor(self, ch: str, previous: str | None) -> float:
        """How much longer this character's press-to-press interval runs."""
        factor = 1.0
        if ch.isspace():
            factor *= self._space_factor
        if ch in _PUNCTUATION:
            factor *= self._punctuation_factor
        elif previous is not None and previous in _PUNCTUATION:
            # The break *after* a sentence mark, not only the reach to it.
            factor *= self._punctuation_factor
        if ch in _SHIFTED or (ch.isalpha() and ch.isupper()):
            factor *= self._shift_factor
        return factor

    def _lognormal(self, median: float, sigma: float) -> float:
        if sigma <= 0.0:
            return median
        return self._rng.lognormvariate(math.log(median), sigma)

    def generate(self, text: str) -> list[Keystroke]:
        keystrokes: list[Keystroke] = []
        previous: str | None = None
        previous_hold = 0.0
        for ch in text:
            interval = self._lognormal(
                self._median_iki * self._factor(ch, previous), self._iki_sigma
            )
            if self._rng.random() < self._pause_probability:
                interval += self._rng.uniform(self._pause_min, self._pause_max)

            # The press-to-press interval covers the previous key's hold;
            # what is left of it is this key's flight time. Floored, so a
            # short draw can never place a release after the next press.
            hold = min(
                self._max_hold,
                max(self._min_hold, self._lognormal(self._median_hold, self._hold_sigma)),
            )
            gap = min(self._max_gap, max(self._min_gap, interval - previous_hold))

            keystrokes.append(Keystroke(char=ch, gap_ms=gap, hold_ms=hold))
            previous, previous_hold = ch, hold
        return keystrokes

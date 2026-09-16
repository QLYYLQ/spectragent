"""Typing rhythm strategy registry.

A rhythm strategy turns a string into a sequence of :class:`Keystroke`
timings. ``HumanKeyboard`` (``input/keyboard.py``) plays them on the same
absolute-deadline schedule the cursor uses, dispatching a real
``Input.dispatchKeyEvent`` per character — which is what makes the
keystrokes visible to the page at all.

It is exactly the shape of ``mouse_trajectory``, deliberately: the strategy
*is* the humanization switch, and there is no boolean beside it to disagree
with it. ``instant`` reports ``humanized == False`` and the controller then
keeps the legacy behaviour — the whole string in one bulk
``Input.insertText``, one CDP call, no key events at all.

Registered strategies (conscribe, mirrors ``input/trajectory``):

- ``instant`` — one bulk ``Input.insertText`` == today's behaviour (default)
- ``human``   — per-character inter-key intervals drawn from a lognormal with
  a heavy right tail, stretched at word boundaries and around punctuation,
  with occasional long pauses and a modelled key-hold time

Why this needs its own registrar rather than a knob on ``mouse_trajectory``:
they are independent axes (a run may want a curved cursor and a bulk insert,
or the reverse), and they produce different things — waypoints versus
keystrokes. A shared "humanization level" enum would make the two
inseparable and the third axis unaddable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from conscribe import create_registrar


@dataclass(slots=True, frozen=True)
class Keystroke:
    """One character's timing.

    ``gap_ms`` is the interval **before** this key goes down — the flight
    time from the previous key's release, which is what a keystroke-dynamics
    collector records as the inter-key interval. ``hold_ms`` is the dwell
    between this key's ``keydown`` and its ``keyup``.

    Unlike a trajectory's first waypoint, the first keystroke's ``gap_ms`` is
    a real interval rather than zero: there is no "current keystroke" the way
    there is a current cursor position, and a human who has just clicked into
    a field does not begin typing in the same instant.

    The pair is a schedule, not a list of sleeps: ``HumanKeyboard`` gives each
    event an absolute deadline, so the CDP round trip is spent inside the
    interval rather than added to it, and ``sum(gap_ms + hold_ms)`` is the
    string's wall-clock typing duration.
    """

    char: str
    gap_ms: float = 0.0
    hold_ms: float = 0.0


@runtime_checkable
class TypingRhythmProtocol(Protocol):
    """Strategy: produce per-character timings for a string."""

    def generate(self, text: str) -> list[Keystroke]:
        """Return one :class:`Keystroke` per character of *text*, in order.

        The list MUST be the same length as *text* and carry its characters
        unchanged: the rhythm decides *when*, never *what*. A strategy that
        reports ``humanized == False`` still returns the keystrokes — the
        controller, not the strategy, decides that a bulk insert is enough.
        """
        ...

    @property
    def humanized(self) -> bool:
        """Whether this strategy models human typing (``False`` only for ``instant``).

        ``HumanKeyboard`` keys the *delivery* off it: ``False`` is one bulk
        ``Input.insertText``, ``True`` is a real ``keydown``/``keyup`` pair
        per character.
        """
        ...


TypingRhythmRegistrar = create_registrar(
    "typing_rhythm",
    TypingRhythmProtocol,
    discriminator_field="rhythm_type",
)


class TypingRhythmBase(metaclass=TypingRhythmRegistrar.Meta):
    """Base for typing rhythm strategies — subclass to auto-register.

    Each concrete strategy sets ``__registry_key__`` explicitly (don't rely
    on name inference) — that string is the YAML-facing ``typing_rhythm``
    value used to select it.
    """

    __abstract__ = True
    __registry_key__: str

    @property
    def rhythm_type(self) -> str:
        return self.__registry_key__

    @property
    def humanized(self) -> bool:
        """Every strategy that models timing is humanized; ``instant`` overrides."""
        return True

"""Browser input layer — human-like mouse and keyboard middle layers.

``HumanMouse`` is the middle layer through which all cursor movement flows and
``HumanKeyboard`` the one through which all text entry flows. Both take a
conscribe-registered strategy — trajectory strategies (``instant`` /
``windmouse`` / ``bezier`` / ``learned``) chosen by the ``mouse_trajectory``
capability config, typing rhythms (``instant`` / ``human``) chosen by
``typing_rhythm`` — and both play it against the one deadline scheduler in
``_schedule.py``. In each pair the strategy *is* the humanization switch;
there is no boolean beside it to disagree with it. Importing this package
registers the built-in strategies as a side effect.
"""

from __future__ import annotations

# Importing the concrete strategies registers them (conscribe metaclass).
from spectragent.core.capabilities.browser.input.bezier import BezierTrajectory
from spectragent.core.capabilities.browser.input.human_rhythm import HumanRhythm
from spectragent.core.capabilities.browser.input.instant import InstantTrajectory
from spectragent.core.capabilities.browser.input.instant_rhythm import InstantRhythm
from spectragent.core.capabilities.browser.input.keyboard import HumanKeyboard
from spectragent.core.capabilities.browser.input.learned import LearnedTrajectory
from spectragent.core.capabilities.browser.input.mouse import HumanMouse
from spectragent.core.capabilities.browser.input.rhythm import (
    Keystroke,
    TypingRhythmBase,
    TypingRhythmProtocol,
    TypingRhythmRegistrar,
)
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryBase,
    MouseTrajectoryProtocol,
    MouseTrajectoryRegistrar,
    TrajectoryPoint,
)
from spectragent.core.capabilities.browser.input.windmouse import WindMouseTrajectory

__all__ = [
    "BezierTrajectory",
    "HumanKeyboard",
    "HumanMouse",
    "HumanRhythm",
    "InstantRhythm",
    "InstantTrajectory",
    "Keystroke",
    "LearnedTrajectory",
    "MouseTrajectoryBase",
    "MouseTrajectoryProtocol",
    "MouseTrajectoryRegistrar",
    "TrajectoryPoint",
    "TypingRhythmBase",
    "TypingRhythmProtocol",
    "TypingRhythmRegistrar",
    "WindMouseTrajectory",
]

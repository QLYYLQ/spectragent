"""Browser input layer — human-like mouse trajectory strategies + controller.

``HumanMouse`` is the middle layer through which all cursor movement flows;
trajectory strategies (``instant`` / ``windmouse`` / ``bezier``) are conscribe-
registered and selected by the ``mouse_trajectory`` capability config. Importing
this package registers the built-in strategies as a side effect.
"""

from __future__ import annotations

# Import concrete strategies for side-effect registration (conscribe metaclass).
from spectragent.core.capabilities.browser.input import bezier as _bezier  # noqa: F401, E402
from spectragent.core.capabilities.browser.input import instant as _instant  # noqa: F401, E402
from spectragent.core.capabilities.browser.input import windmouse as _windmouse  # noqa: F401, E402
from spectragent.core.capabilities.browser.input.bezier import BezierTrajectory
from spectragent.core.capabilities.browser.input.instant import InstantTrajectory
from spectragent.core.capabilities.browser.input.mouse import HumanMouse
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryBase,
    MouseTrajectoryProtocol,
    MouseTrajectoryRegistrar,
    TrajectoryPoint,
)
from spectragent.core.capabilities.browser.input.windmouse import WindMouseTrajectory

__all__ = [
    "BezierTrajectory",
    "HumanMouse",
    "InstantTrajectory",
    "MouseTrajectoryBase",
    "MouseTrajectoryProtocol",
    "MouseTrajectoryRegistrar",
    "TrajectoryPoint",
    "WindMouseTrajectory",
]

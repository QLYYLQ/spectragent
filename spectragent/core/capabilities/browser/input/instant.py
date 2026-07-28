"""InstantTrajectory — the default no-op strategy.

A single waypoint at the destination, dt=0. With this strategy
``HumanMouse.move_to`` dispatches exactly one ``mouseMoved`` (at the target,
no sleep), so ``click`` collapses to the legacy three-event sequence and
``wheel`` stays a single ``mouseWheel`` — byte-for-byte the pre-humanization
behavior. This is what runs whenever ``humanize_mouse`` is off.
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryBase,
    TrajectoryPoint,
)


class InstantTrajectory(MouseTrajectoryBase):
    """Single-point path == instant jump (current behavior, the default)."""

    __registry_key__ = "instant"

    def generate(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        target_w: float = 0.0,
        target_h: float = 0.0,
    ) -> list[TrajectoryPoint]:
        return [TrajectoryPoint(x=round(end_x), y=round(end_y), dt_ms=0.0)]

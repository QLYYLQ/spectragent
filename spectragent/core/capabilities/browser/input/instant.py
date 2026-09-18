"""InstantTrajectory — the default no-op strategy.

A single waypoint at the destination, dt=0. With this strategy
``HumanMouse.move_to`` dispatches exactly one ``mouseMoved`` (at the target,
no sleep), so ``click`` collapses to the legacy three-event sequence and
``wheel`` stays a single ``mouseWheel`` — byte-for-byte the pre-humanization
behavior.

It is the only way to switch humanization off: ``humanized`` is ``False``, so
``HumanMouse`` also keeps the legacy ``(0, 0)`` cold start, the single-event
wheel and no cursor approach before type/select. It is also what runs when no
strategy is configured at all (direct construction, no Composer).
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

    def click_hold_ms(self) -> float:
        """Zero, and it must stay zero.

        Press and release in the same instant is *the* legacy click — three
        input events, no wall time between them, no sleep anywhere. Giving
        this a human default would quietly humanize the one strategy whose
        whole job is not to, so the value is pinned by a test.
        """
        return 0.0

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        """Dead centre, every time — the legacy landing point.

        Pinned by the same registry-wide test as the hold: whatever a box
        measures, this strategy clicks the arithmetic centre of it, because
        "not humanized" has to mean the pre-humanization behaviour down to
        the pixel.
        """
        return (0.0, 0.0)

    @property
    def humanized(self) -> bool:
        return False

"""BezierTrajectory — cubic Bézier path for human-like cursor movement.

Two control points are offset perpendicular to the straight start→end line, on
a single (random) side so the curve doesn't wobble unnaturally (the ghost-cursor
approach). Sampled at a distance-scaled number of points, then timed via Fitts's
law + ease-out (:mod:`._timing`). Pure stdlib, seedable.
"""

from __future__ import annotations

import math
import random

from spectragent.core.capabilities.browser.input._timing import (
    assign_timing,
    fitts_duration_ms,
)
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryBase,
    TrajectoryPoint,
)


class BezierTrajectory(MouseTrajectoryBase):
    """Cubic-Bézier path + Fitts's-law timing."""

    __registry_key__ = "bezier"

    def __init__(
        self,
        *,
        fitts_a: float = 80.0,
        fitts_b: float = 55.0,
        min_duration_ms: float = 120.0,
        max_duration_ms: float = 1400.0,
        points_per_100px: float = 14.0,
        min_points: int = 6,
        max_points: int = 80,
        curvature: float = 0.18,
        ease_out_power: float = 1.7,
        jitter: float = 0.18,
        seed: int | None = None,
    ) -> None:
        self._fitts_a = fitts_a
        self._fitts_b = fitts_b
        self._min_ms = min_duration_ms
        self._max_ms = max_duration_ms
        self._points_per_100px = points_per_100px
        self._min_points = max(2, min_points)
        self._max_points = max(self._min_points, max_points)
        self._curvature = curvature
        self._ease = ease_out_power
        self._jitter = jitter
        self._rng = random.Random(seed)

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
        ex, ey = round(end_x), round(end_y)
        distance = math.hypot(end_x - start_x, end_y - start_y)
        if distance < 1.0:
            return [TrajectoryPoint(ex, ey, 0.0)]

        rng = self._rng
        n = int(round(distance / 100.0 * self._points_per_100px))
        n = max(self._min_points, min(self._max_points, n))

        # Unit direction + perpendicular.
        dir_x, dir_y = (end_x - start_x) / distance, (end_y - start_y) / distance
        perp_x, perp_y = -dir_y, dir_x

        # Two control points, offset on one (random) side of the line.
        side = 1.0 if rng.random() < 0.5 else -1.0
        off1 = side * distance * self._curvature * rng.uniform(0.4, 1.0)
        off2 = side * distance * self._curvature * rng.uniform(0.4, 1.0)
        c1x = start_x + dir_x * distance * 0.33 + perp_x * off1
        c1y = start_y + dir_y * distance * 0.33 + perp_y * off1
        c2x = start_x + dir_x * distance * 0.66 + perp_x * off2
        c2y = start_y + dir_y * distance * 0.66 + perp_y * off2

        pts: list[tuple[int, int]] = []
        for i in range(n + 1):
            t = i / n
            mt = 1.0 - t
            bx = mt**3 * start_x + 3 * mt**2 * t * c1x + 3 * mt * t**2 * c2x + t**3 * end_x
            by = mt**3 * start_y + 3 * mt**2 * t * c1y + 3 * mt * t**2 * c2y + t**3 * end_y
            p = (round(bx), round(by))
            if not pts or pts[-1] != p:
                pts.append(p)

        if pts[-1] != (ex, ey):
            pts.append((ex, ey))
        if len(pts) == 1:
            return [TrajectoryPoint(ex, ey, 0.0)]

        total_ms = fitts_duration_ms(
            distance,
            target_w,
            a=self._fitts_a,
            b=self._fitts_b,
            min_ms=self._min_ms,
            max_ms=self._max_ms,
        )
        return assign_timing(
            pts,
            total_ms,
            ease_out_power=self._ease,
            jitter=self._jitter,
            rng=self._rng,
        )

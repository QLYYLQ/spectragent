"""Shared timing math for spatial-path trajectory strategies.

Spatial strategies (WindMouse, Bézier) produce a list of integer ``(x, y)``
waypoints; this module turns that into ``TrajectoryPoint``s with per-point
``dt_ms`` so the cursor accelerates then decelerates into the target.

- :func:`fitts_duration_ms` — total movement time from Fitts's law.
- :func:`assign_timing` — distribute that total across the waypoints with an
  ease-out profile (big gaps early = fast, tiny gaps near the target = slow)
  plus per-point jitter so the cadence isn't robotic.
"""

from __future__ import annotations

import math
import random

from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint


def fitts_duration_ms(
    distance: float,
    target_w: float,
    *,
    a: float,
    b: float,
    min_ms: float,
    max_ms: float,
) -> float:
    """Total movement time (ms) via Fitts's law, clamped to [min_ms, max_ms].

    ``ID = log2(distance / target_w + 1)`` (index of difficulty — bigger,
    closer targets are easier → faster); ``MT = a + b * ID``.
    """
    width = max(target_w, 1.0)
    index_of_difficulty = math.log2(distance / width + 1.0)
    mt = a + b * index_of_difficulty
    return max(min_ms, min(mt, max_ms))


def assign_timing(
    points: list[tuple[int, int]],
    total_ms: float,
    *,
    ease_out_power: float,
    jitter: float,
    rng: random.Random,
) -> list[TrajectoryPoint]:
    """Attach ease-out per-point ``dt_ms`` to spatial waypoints.

    The first point carries ``dt_ms == 0`` (no pre-move sleep). Subsequent
    gaps follow ``progress_i = 1 - (1 - i/(n-1))**ease_out_power`` so the
    cumulative time telescopes to ``total_ms`` (before jitter).
    """
    n = len(points)
    if n == 0:
        return []
    if n == 1:
        px, py = points[0]
        return [TrajectoryPoint(px, py, 0.0)]

    result: list[TrajectoryPoint] = []
    prev_progress = 0.0
    for i, (px, py) in enumerate(points):
        if i == 0:
            dt = 0.0
        else:
            progress = 1.0 - (1.0 - i / (n - 1)) ** ease_out_power
            dt = total_ms * (progress - prev_progress)
            prev_progress = progress
            if jitter > 0.0:
                dt *= 1.0 + rng.uniform(-jitter, jitter)
                dt = max(0.0, dt)
        result.append(TrajectoryPoint(px, py, dt))
    return result

"""WindMouseTrajectory — gravity/wind physics model for human-like paths.

Clean-room re-implementation of the WindMouse algorithm (BenLand100): the
cursor is a point mass pulled toward the target by a constant *gravity* while
*wind* injects decaying random perturbations, producing curved, slightly
overshooting paths. Spatial points are then timed via Fitts's law + ease-out
(:mod:`._timing`). Pure stdlib, seedable for reproducibility.
"""

from __future__ import annotations

import math
import random
from typing import Annotated

from pydantic import Field as PydanticField

from spectragent.core.capabilities.browser.input._aim import (
    DEFAULT_AIM_MARGIN_PX,
    DEFAULT_AIM_SIGMA_RATIO,
    sample_aim_offset_px,
)
from spectragent.core.capabilities.browser.input._timing import (
    DEFAULT_CLICK_HOLD_MAX_MS,
    DEFAULT_CLICK_HOLD_MEDIAN_MS,
    DEFAULT_CLICK_HOLD_MIN_MS,
    DEFAULT_CLICK_HOLD_SIGMA,
    assign_timing,
    fitts_duration_ms,
    sample_click_hold_ms,
)
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryBase,
    TrajectoryPoint,
)

_SQRT3 = math.sqrt(3.0)
_SQRT5 = math.sqrt(5.0)


class WindMouseTrajectory(MouseTrajectoryBase):
    """Gravity/wind path + Fitts's-law timing."""

    __registry_key__ = "windmouse"

    def __init__(
        self,
        *,
        gravity: float = 9.0,
        wind: float = 3.0,
        max_step: float = 15.0,
        target_area: float = 12.0,
        fitts_a: float = 80.0,
        fitts_b: float = 55.0,
        min_duration_ms: float = 120.0,
        max_duration_ms: float = 1400.0,
        max_points: int = 120,
        ease_out_power: float = 1.7,
        jitter: float = 0.18,
        click_hold_median_ms: float = DEFAULT_CLICK_HOLD_MEDIAN_MS,
        click_hold_sigma: float = DEFAULT_CLICK_HOLD_SIGMA,
        click_hold_min_ms: float = DEFAULT_CLICK_HOLD_MIN_MS,
        click_hold_max_ms: float = DEFAULT_CLICK_HOLD_MAX_MS,
        aim_sigma_ratio: float = DEFAULT_AIM_SIGMA_RATIO,
        aim_margin_px: float = DEFAULT_AIM_MARGIN_PX,
        seed: Annotated[
            int | None,
            PydanticField(
                description=(
                    "Seed for this instance's random stream, which draws the path, "
                    "its timing, the click hold and the aim point: one seed replays "
                    "the cursor. None (the default) draws fresh entropy."
                ),
            ),
        ] = None,
    ) -> None:
        self._g = gravity
        self._w = wind
        self._max_step = max_step
        self._target_area = target_area
        self._fitts_a = fitts_a
        self._fitts_b = fitts_b
        self._min_ms = min_duration_ms
        self._max_ms = max_duration_ms
        self._max_points = max(2, max_points)
        self._ease = ease_out_power
        self._jitter = jitter
        self._hold_median_ms = click_hold_median_ms
        self._hold_sigma = click_hold_sigma
        self._hold_min_ms = click_hold_min_ms
        self._hold_max_ms = max(click_hold_min_ms, click_hold_max_ms)
        self._aim_sigma_ratio = aim_sigma_ratio
        self._aim_margin_px = aim_margin_px
        self._rng = random.Random(seed)

    def click_hold_ms(self) -> float:
        """A human hold, drawn from this instance's own stream (see ``_timing``)."""
        return sample_click_hold_ms(
            self._rng,
            median_ms=self._hold_median_ms,
            sigma=self._hold_sigma,
            min_ms=self._hold_min_ms,
            max_ms=self._hold_max_ms,
        )

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        """Where inside the box this click lands, from the same stream (see ``_aim``)."""
        return sample_aim_offset_px(
            self._rng,
            width,
            height,
            sigma_ratio=self._aim_sigma_ratio,
            margin_px=self._aim_margin_px,
        )

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
        spatial = self._spatial_path(float(start_x), float(start_y), float(end_x), float(end_y))

        # Dedup consecutive identical integer points.
        pts: list[tuple[int, int]] = []
        for p in spatial:
            if not pts or pts[-1] != p:
                pts.append(p)
        # Endpoint exactness.
        if not pts or pts[-1] != (ex, ey):
            pts.append((ex, ey))
        if len(pts) == 1:
            return [TrajectoryPoint(ex, ey, 0.0)]

        distance = math.hypot(end_x - start_x, end_y - start_y)
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

    def _spatial_path(self, sx: float, sy: float, ex: float, ey: float) -> list[tuple[int, int]]:
        rng = self._rng
        g, w, d = self._g, self._w, self._target_area
        m = self._max_step
        cur_x, cur_y = sx, sy
        v_x = v_y = 0.0
        wind_x = wind_y = 0.0

        points: list[tuple[int, int]] = [(round(cur_x), round(cur_y))]
        dist = math.hypot(ex - cur_x, ey - cur_y)

        while dist >= 1.0 and len(points) < self._max_points:
            w_mag = min(w, dist)
            if dist >= d:
                # Far from target: inject decaying random wind.
                wind_x = wind_x / _SQRT3 + (2.0 * rng.random() - 1.0) * w_mag / _SQRT5
                wind_y = wind_y / _SQRT3 + (2.0 * rng.random() - 1.0) * w_mag / _SQRT5
            else:
                # Near target: damp wind, shrink the step so we settle.
                wind_x /= _SQRT3
                wind_y /= _SQRT3
                if m < 3.0:
                    m = rng.random() * 3.0 + 3.0
                else:
                    m /= _SQRT5

            # Accelerate: wind + gravity toward the target.
            v_x += wind_x + g * (ex - cur_x) / dist
            v_y += wind_y + g * (ey - cur_y) / dist

            # Clamp speed to the (current) max step.
            v_mag = math.hypot(v_x, v_y)
            if v_mag > m:
                v_clip = m / 2.0 + rng.random() * m / 2.0
                v_x = v_x / v_mag * v_clip
                v_y = v_y / v_mag * v_clip

            cur_x += v_x
            cur_y += v_y
            points.append((round(cur_x), round(cur_y)))
            dist = math.hypot(ex - cur_x, ey - cur_y)

        return points

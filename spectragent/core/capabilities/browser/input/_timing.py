"""Shared timing math for trajectory strategies.

Spatial strategies (WindMouse, Bézier) produce a list of integer ``(x, y)``
waypoints; this module turns that into ``TrajectoryPoint``s with per-point
``dt_ms`` so the cursor accelerates then decelerates into the target.

- :func:`fitts_duration_ms` — total movement time from Fitts's law.
- :func:`assign_timing` — distribute that total across the waypoints with an
  ease-out profile (big gaps early = fast, tiny gaps near the target = slow)
  plus per-point jitter so the cadence isn't robotic.
- :func:`sample_click_hold_ms` — how long the button stays down, for *every*
  humanized strategy including ``learned`` (which does its own path timing but
  models no button state: the recordings behind it are cursor positions).
- :func:`standard_normal` — one unit normal draw, by inverse CDF, shared with
  the aim scatter in :mod:`._aim`.

The samplers take only a ``random()`` source, not a ``random.Random``, because
the strategies do not agree on an RNG type — ``windmouse`` / ``bezier`` carry a
``random.Random``, ``learned`` a ``numpy.random.Generator`` — and ``random()``
is the one draw both spell the same way.

**The numbers here are defaults, not policy.** Each ``DEFAULT_*`` is the
default of a keyword argument on ``WindMouseTrajectory`` / ``BezierTrajectory``
/ ``LearnedTrajectory``, which conscribe turns into a field of the generated
``mouse_trajectory`` config — so a run tunes the hold from YAML like every
other knob in this layer, and the constant is only the one place the default
value is written down. Nothing reads them at call time.

This module must stay numpy-free: ``learned`` imports it, and numpy is an
optional extra that ``LearnedTrajectory`` imports lazily in its ``__init__``.
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist
from typing import Protocol

from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint

#: Default median click hold (press → release), in ms, for the humanized
#: strategies. ``HumanRhythm`` models the same gesture for a key (its own
#: ``median_hold_ms``, default 85 ms) but the two are independent settings:
#: both are per-strategy config, and a run may well want a typist and a
#: clicker that disagree. Do not assert one against the other.
DEFAULT_CLICK_HOLD_MEDIAN_MS = 92.0
#: Default log-space spread. Wider than the keyboard's 0.22 — a click is a
#: deliberate act whose length depends on what is being clicked, where a
#: keystroke inside a word is a reflex.
DEFAULT_CLICK_HOLD_SIGMA = 0.32
#: Default clamps on the lognormal's tails: below is faster than a finger can
#: move, above starts to read as a press-and-hold gesture.
DEFAULT_CLICK_HOLD_MIN_MS = 35.0
DEFAULT_CLICK_HOLD_MAX_MS = 400.0


class UniformSource(Protocol):
    """Anything that draws a float in ``[0, 1)`` — ``random.Random`` or a numpy ``Generator``."""

    def random(self) -> float: ...


def standard_normal(rng: UniformSource) -> float:
    """One draw from N(0, 1), by inverse CDF of a single uniform.

    Inverse CDF rather than ``gauss`` / ``normal`` so the *caller's* RNG
    decides the stream whatever type it is (``random.Random.gauss`` and
    ``numpy.random.Generator.normal`` are not the same call, and ``gauss``
    does not even consume one uniform per draw). One uniform in, one normal
    out, so a draw costs exactly one number from a seeded sequence.
    """
    # NormalDist.inv_cdf is undefined at the closed ends of (0, 1), and
    # random() can return exactly 0.0.
    u = min(max(float(rng.random()), 1e-9), 1.0 - 1e-9)
    return NormalDist().inv_cdf(u)


def sample_click_hold_ms(
    rng: UniformSource,
    *,
    median_ms: float,
    sigma: float,
    min_ms: float,
    max_ms: float,
) -> float:
    """One draw from a human click-hold lognormal, clamped to its tails.

    The shape is shared (three strategies should not each carry their own
    distribution code); the four parameters are the caller's, and every
    caller passes its own configured values — there is no default here, so a
    strategy cannot forget to thread its config through and silently sample
    someone else's distribution.
    """
    hold = median_ms * math.exp(sigma * standard_normal(rng))
    return min(max_ms, max(min_ms, hold))


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

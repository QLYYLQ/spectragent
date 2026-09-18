"""Where inside a target box a click aims — the spatial half of humanization.

A measurement gives a *box*; a click needs a *point*. Taking the arithmetic
centre every single time is a tell no amount of path realism covers: measured
on real pages the landing error was 0.78 px, where humans scatter 3-31 px.
:func:`sample_aim_offset_px` is the offset that fixes it, drawn once per click.

It lives beside :mod:`._timing` and not in it because it is geometry, not
timing; it lives in ``input/`` and not in ``ActionWatchdog`` because the aim is
humanization, and the trajectory strategy is this layer's single humanization
switch — ``instant`` returns ``(0.0, 0.0)`` here exactly as it returns ``0.0``
from ``click_hold_ms()``, and a future strategy can aim its own way without any
consumer changing. What the *watchdog* still owns is the seam: it asks the
strategy for the offset, clips it to the visible viewport and applies it, so
one exact point reaches the trajectory, the press, the result and the trace.

Like :mod:`._timing`, this module must stay numpy-free — ``learned`` imports
it and numpy is an optional extra.
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.input._timing import UniformSource, standard_normal

#: Default spread, as a fraction of the target box's *short* side. At 1/6,
#: half of that short side is 3 sigma, so a draw is almost always well inside
#: even a thin element.
DEFAULT_AIM_SIGMA_RATIO = 1.0 / 6.0
#: Default distance kept inside the border. Clicks on the last pixel of a box
#: land on the border, on a child, or on nothing.
DEFAULT_AIM_MARGIN_PX = 2.0

#: Rejection-sampling budget before falling back to a clamp. Not a knob: it
#: trades a tiny bias (only reachable by a box the margin has nearly eaten)
#: against an unbounded loop, and no run wants either end of that.
_MAX_DRAWS = 8


def _truncated_normal(rng: UniformSource, sigma: float, limit: float) -> float:
    """One draw in ``[-limit, limit]`` from N(0, sigma), truncated by rejection.

    Rejection rather than a clamp so the shape stays normal: a clamp piles the
    rejected mass onto the two edges, which is the opposite of the point — the
    edge of a box is exactly where a human does *not* aim.
    """
    if limit <= 0.0 or sigma <= 0.0:
        return 0.0
    for _ in range(_MAX_DRAWS):
        value = sigma * standard_normal(rng)
        if -limit <= value <= limit:
            return value
    return max(-limit, min(limit, sigma * standard_normal(rng)))


def sample_aim_offset_px(
    rng: UniformSource,
    width: float,
    height: float,
    *,
    sigma_ratio: float,
    margin_px: float,
) -> tuple[float, float]:
    """Offset from the centre of a ``width`` x ``height`` box, in CSS pixels.

    ``sigma = sigma_ratio * min(width, height)`` — the *short* side sets the
    spread on **both** axes, so the spread that fits the tightest direction is
    the one used, and a long thin link is not aimed at as loosely as a big
    button. Each axis is then truncated on its own half-extent, ``margin_px``
    inside the border.

    Returns ``(0.0, 0.0)`` for a box with no size on either axis — an
    unmeasured element, or one the margin swallows. There is nothing to
    scatter inside, and guessing outward would click something else.
    """
    if width <= 0.0 or height <= 0.0:
        return (0.0, 0.0)
    sigma = sigma_ratio * min(width, height)
    dx = _truncated_normal(rng, sigma, width / 2.0 - margin_px)
    dy = _truncated_normal(rng, sigma, height / 2.0 - margin_px)
    return (dx, dy)

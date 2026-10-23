"""Render the platform-neutral capture state: the one writing of its four facts.

``docs/architecture_design.md`` §4.4, "跨平台采集状态 ``capture_state``". Every
Agent may declare the relative fragment ``capture_state`` optional; whichever
capability provides it (``browser.capture_state`` now, a desktop or Android one
later), the model reads the same four facts in the same words. The wording
names no platform: no page, tab or URL.

Reads are structural, by the relative fragment name, like
``render_browser_platform``: no capability or resolver class is imported, so a
cross-platform Agent stays free of browser types. "Not measured" is said as
such and never rendered as zero; a measured zero says none.

An Agent that does not show the capture state excludes the fragment from its
negotiation demand (:func:`capture_state_demand`) rather than dropping it from
the prompt after capture, so the fragment is neither captured nor rendered.

The pointer is the one point among the facts, written by the ``write_point``
the Agent passes (:data:`~spectragent.schemas.action.PointWriter`, shared with
the action effects' ``describe``): the framework's writer for this step's
frame (``core/loops/coordinate_frames.py::point_writer``), so its model reads
the pointer on the grid it writes points on (§4.1, "坐标系归框架"). The
default writes viewport pixels, as before.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from spectragent.core.negotiation import bounded_demand
from spectragent.schemas.action import PointWriter, write_viewport_point
from spectragent.schemas.observation.failure import FragmentFailure

#: The relative observation key every Agent declares optional.
CAPTURE_STATE_KEY = "capture_state"

#: What the model reads when the optional fragment's capture failed this step.
_NOT_MEASURED = (
    "<capture_state>\n"
    "The capture state could not be measured this step: settle, foreground change, "
    "pointer and failed visible resources are all unknown.\n"
    "</capture_state>"
)


def write_viewport_pixels(x: int, y: int) -> str:
    """The default pointer writer: the viewport point with its unit, ``(x, y) in viewport pixels``."""
    return f"{write_viewport_point(x, y)} in viewport pixels"


def render_capture_state(
    observation: Any, *, write_point: PointWriter = write_viewport_pixels
) -> str:
    """The ``<capture_state>`` block of *observation*, or ``""`` when it has none.

    ``""`` when the slot is absent (the environment provides no capture state,
    or the Agent excluded it). A :class:`FragmentFailure` in the slot is said
    as one explicit sentence: the facts are unknown this step, not absent from
    the platform. A known pointer is written by *write_point*.
    """
    fragment = getattr(observation, "capture_state", None)
    if fragment is None:
        return ""
    if isinstance(fragment, FragmentFailure):
        return _NOT_MEASURED
    lines = [
        f"Settle: {_settle(fragment.settle_state)}",
        f"Foreground change: {_foreground(fragment.foreground_change)}",
        f"Pointer: {_pointer(fragment.pointer, write_point)}",
        f"Failed visible resources: {_failed_resources(fragment.failed_visible_resources)}",
    ]
    return "<capture_state>\n" + "\n".join(lines) + "\n</capture_state>"


def capture_state_demand(
    owner: Any,
    registry: str,
    required: Iterable[str],
    optional: Iterable[str],
    *,
    shown: bool,
) -> tuple[set[str], set[str]]:
    """*owner*'s demand, without the optional ``capture_state`` when it is not shown.

    Instance narrowing inside the class's declared space
    (``docs/architecture_design.md`` §4.4, "实例在类声明空间内收窄"): an Agent
    that renders no capture state does not ask for it, so the environment does
    not capture it. Routed through ``bounded_demand``, which refuses a demand
    that left the declared space. Every other registry, and a shown capture
    state, pass through untouched.
    """
    required, optional = set(required), set(optional)
    if registry != "observation" or shown:
        return required, optional
    return bounded_demand(owner, registry, required, optional - {CAPTURE_STATE_KEY})


def _settle(settle: Any) -> str:
    if settle is None:
        return "no wait before this capture."
    waited = f"{settle.waited_ms} ms"
    if settle.outcome == "settled":
        return f"settled after waiting {waited}."
    if settle.outcome == "capped":
        pending = ", ".join(settle.pending)
        tail = f"; still pending: {pending}" if pending else ""
        return f"capped: the wait ended at its limit after {waited}{tail}."
    detail = f" ({settle.detail})" if getattr(settle, "detail", None) else ""
    return f"unknown after waiting {waited}{detail}."


def _foreground(change: Any) -> str:
    if change is None:
        return "none."
    if change.reason == "opened":
        return "the environment opened a new surface and the capture shows it."
    return "the focused surface closed itself and the capture shows another."


def _pointer(pointer: Any, write_point: PointWriter) -> str:
    if pointer.status == "known":
        return f"{write_point(pointer.x, pointer.y)}."
    if pointer.status == "unavailable":
        return "no pointer on this platform."
    return "unknown (no confirmed position)."


def _failed_resources(failed: Any) -> str:
    if failed.status == "measured":
        if failed.count == 0:
            return "none failed."
        named = list(failed.identifiers)
        if not named:
            return f"{failed.count} failed."
        listed = ", ".join(named)
        if len(named) < failed.count:
            return f"{failed.count} failed; the first {len(named)}: {listed}."
        return f"{failed.count} failed: {listed}."
    detail = f" ({failed.detail})" if failed.detail else ""
    if failed.status == "unavailable":
        return f"not measurable here{detail}."
    return f"unknown{detail}."


__all__ = [
    "CAPTURE_STATE_KEY",
    "capture_state_demand",
    "render_capture_state",
    "write_viewport_pixels",
]

"""Mouse trajectory strategy registry.

A trajectory strategy turns a straight ``start → end`` cursor jump into a
sequence of :class:`TrajectoryPoint` waypoints carrying per-point timing.
``HumanMouse`` (``input/mouse.py``) dispatches one CDP ``mouseMoved`` per
waypoint, sleeping ``dt_ms`` before each, which is what produces a curved
path with realistic acceleration / deceleration.

Registered strategies (conscribe, mirrors ``dom/cleaning``):

- ``instant``   — single point == the current instant-jump behavior (default)
- ``windmouse`` — gravity/wind physics model + Fitts's-law timing
- ``bezier``    — cubic Bézier path + Fitts's-law timing

The registry leaves room for a future ``gan`` / ``diffusion`` strategy with
zero changes to consumers — only a new class file is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from conscribe import create_registrar


@dataclass(slots=True, frozen=True)
class TrajectoryPoint:
    """One waypoint along a mouse path.

    ``x`` / ``y`` are integer viewport-CSS pixels — exactly what
    ``Input.dispatchMouseEvent`` expects. ``dt_ms`` is the delay to sleep
    **before** dispatching this point's ``mouseMoved`` (the inter-sample
    interval that encodes velocity / acceleration). The first point of a
    path carries ``dt_ms == 0.0`` (no pre-move sleep).
    """

    x: int
    y: int
    dt_ms: float = 0.0


@runtime_checkable
class MouseTrajectoryProtocol(Protocol):
    """Strategy: produce waypoints from a start point to an end point."""

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
        """Return the path waypoints.

        ``target_w`` / ``target_h`` are the destination element's size in
        CSS pixels (0 when unknown, e.g. a raw-coordinate click); strategies
        may use them for Fitts's-law timing. The returned list MUST be
        non-empty and its **last** point MUST be exactly
        ``(round(end_x), round(end_y))`` so the click lands on target.
        """
        ...


MouseTrajectoryRegistrar = create_registrar(
    "mouse_trajectory",
    MouseTrajectoryProtocol,
    discriminator_field="trajectory_type",
)


class MouseTrajectoryBase(metaclass=MouseTrajectoryRegistrar.Meta):
    """Base for trajectory strategies — subclass to auto-register.

    Each concrete strategy sets ``__registry_key__`` explicitly (don't rely
    on name inference) — that string is the YAML-facing ``mouse_trajectory``
    value used to select it.
    """

    __abstract__ = True
    __registry_key__: str

    @property
    def trajectory_type(self) -> str:
        return self.__registry_key__

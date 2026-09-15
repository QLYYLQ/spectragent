"""Mouse trajectory strategy registry.

A trajectory strategy turns a straight ``start → end`` cursor jump into a
sequence of :class:`TrajectoryPoint` waypoints carrying per-point timing.
``HumanMouse`` (``input/mouse.py``) sleeps ``dt_ms`` before each waypoint and
dispatches a CDP ``mouseMoved`` for every waypoint that moves the cursor, which
is what produces a curved path with realistic acceleration / deceleration.

The strategy is also the one humanization switch: ``instant`` reports
``humanized == False`` and the controller then keeps every legacy behaviour
(instant jump, ``(0, 0)`` cold start, single-event wheel, no cursor approach
before type/select); every other strategy reports ``True``.

Registered strategies (conscribe, mirrors ``dom/cleaning``):

- ``instant``   — single point == the legacy instant-jump behavior (default)
- ``windmouse`` — gravity/wind physics model + Fitts's-law timing
- ``bezier``    — cubic Bézier path + Fitts's-law timing
- ``learned``   — path sampled from a model trained on recorded human movements
  (numpy inference, optional ``learned-mouse`` extra), replayed at the model's
  tick rate; which model is read from the export's ``meta.json``

A new trained model (say a diffusion export) is not a new strategy: it is a
backend of ``learned``, dispatched on its export format. A new strategy of a
different kind needs zero changes to consumers — only a new class file, plus
its key in the capability wiring.
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
    **before** this waypoint (the inter-sample interval that encodes
    velocity / acceleration). The first point of a path carries
    ``dt_ms == 0.0`` (no pre-move sleep).

    A waypoint that repeats the cursor position still takes its time but
    dispatches no event, so a strategy can express stillness — a pause
    mid-path, or dwelling on the target before the click — by repeating a
    point. The one exception is a single-point path (the instant jump),
    which always dispatches.
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

    @property
    def humanized(self) -> bool:
        """Whether this strategy models human motion (``False`` only for ``instant``).

        ``HumanMouse`` keys the behaviour *around* the path off it: cold
        start at the viewport centre, wheel deltas split into eased
        increments, and a cursor approach before keyboard/DOM actions.
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

    @property
    def humanized(self) -> bool:
        """Every strategy that emits a path is humanized; ``instant`` overrides."""
        return True

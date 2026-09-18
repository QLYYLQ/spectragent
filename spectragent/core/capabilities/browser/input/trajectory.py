"""Mouse trajectory strategy registry.

A trajectory strategy turns a straight ``start → end`` cursor jump into a
sequence of :class:`TrajectoryPoint` waypoints carrying per-point timing.
``HumanMouse`` (``input/mouse.py``) plays them on an absolute schedule, waiting
until each waypoint's deadline and dispatching a CDP ``mouseMoved`` for every
waypoint that moves the cursor, which is what produces a curved path with
realistic acceleration / deceleration.

The strategy is also the one humanization switch: ``instant`` reports
``humanized == False`` and the controller then keeps every legacy behaviour
(instant jump, ``(0, 0)`` cold start, single-event wheel, no cursor approach
before type/select); every other strategy reports ``True``.

Beside the path it carries the two things a click needs that no waypoint can
express: :meth:`~MouseTrajectoryProtocol.click_hold_ms`, the time the button
stays down once the cursor has arrived, and
:meth:`~MouseTrajectoryProtocol.aim_offset_px`, where inside the target box the
click actually lands. Both are humanization, both are drawn from the strategy's
own seeded RNG, and neither is a property of any one waypoint — so they are
members of this protocol rather than fields of :class:`TrajectoryPoint`, and
the RNG never has to leave the strategy.

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
    ``Input.dispatchMouseEvent`` expects. ``dt_ms`` is the interval
    **before** this waypoint (the inter-sample interval that encodes
    velocity / acceleration). The first point of a path carries
    ``dt_ms == 0.0`` (it is due immediately).

    The intervals are a schedule, not a list of sleeps: ``HumanMouse`` gives
    waypoint ``i`` the absolute deadline ``start + sum(dt_ms[:i + 1])`` and
    waits until it, so the dispatch latency is spent inside the tick rather
    than added to it, and ``sum(dt_ms)`` is the path's wall-clock duration.
    A strategy that cannot be dispatched that fast is thinned, not slowed:
    an intermediate waypoint whose slot has already passed is dropped.

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

    def click_hold_ms(self) -> float:
        """How long the button stays down, in ms — one draw per click.

        The path ends when the cursor arrives; the *button* then goes down
        and comes back up, and the interval between those two events is its
        own behavioural signal. It is not a property of any one waypoint —
        nothing moves while the button is held — so it does not belong on
        :class:`TrajectoryPoint`; it belongs to the humanization layer, of
        which the strategy is the single switch.

        ``HumanMouse.click`` spends it on the same deadline schedule as the
        path, with the press dispatched in the slot before the release, so
        the press's own CDP round trip is spent *inside* the hold rather
        than being the whole of it (~1.4-3.4 ms measured, against tens to
        hundreds of ms for a human).

        A strategy that reports ``humanized == False`` MUST return ``0.0``:
        that is what keeps the legacy click at exactly three input events
        with no wall time between them. Every other strategy samples its
        own duration, from its own seeded RNG, so a click is never the same
        length twice.
        """
        ...

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        """Where inside a ``width`` x ``height`` target box this click aims.

        Returns ``(dx, dy)`` in CSS pixels, relative to the box's centre —
        one draw per click, like the hold.

        A box does not say where to click, it says where it is *legal* to.
        Aiming at the arithmetic centre every time is a tell: measured on
        real pages the landing error was 0.78 px, where humans scatter
        3-31 px. The offset belongs here for the same reason the hold does —
        it is humanization, and the strategy is the single switch — and
        keeping it here is what keeps every RNG inside the strategy that
        owns it. :func:`~spectragent.core.capabilities.browser.input._aim.sample_aim_offset_px`
        is the shared implementation; a strategy is free to aim its own way.

        The caller owns the *seam*, not the policy: ``ActionWatchdog``
        clips the returned offset to the visible viewport and applies it to
        the centre, so the trajectory is handed one exact point and still
        ends exactly on it (a strategy that moved its own endpoint would
        leave the trace, the overlay and the result disagreeing about where
        the click went).

        A strategy that reports ``humanized == False`` MUST return
        ``(0.0, 0.0)``, and every strategy must return ``(0.0, 0.0)`` for a
        box with no size — an unmeasured element has nothing to scatter
        inside and guessing outward clicks something else.
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

    def click_hold_ms(self) -> float:
        """No default: a hold time is a modelling decision, not a fallback.

        ``humanized`` can default to ``True`` because a strategy that emits a
        path is humanized by construction. A duration cannot: a constant here
        would be a distribution nobody chose, and ``0.0`` here is exactly the
        defect (a press and release in the same millisecond) handed silently
        to every strategy that forgot to think about it. So the base refuses,
        and each strategy says what it holds for — ``instant`` explicitly
        ``0.0``, the rest a draw from their own RNG.
        """
        raise self._unimplemented(
            "click_hold_ms",
            "sample a hold time (see input/_timing.py::sample_click_hold_ms)",
        )

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        """No default either, and for the same reason.

        ``(0.0, 0.0)`` here is the defect: a pixel-perfect landing on the
        arithmetic centre, handed silently to every strategy that did not
        think about where inside the box it aims.
        """
        raise self._unimplemented(
            "aim_offset_px",
            "sample an offset (see input/_aim.py::sample_aim_offset_px)",
        )

    def _unimplemented(self, member: str, remedy: str) -> NotImplementedError:
        """The base's refusal, naming the class that inherited it.

        Worth the indirection because conscribe does **not** check protocol
        conformance when a class registers: a strategy missing a member
        registers happily and fails at the first click. Naming the class is
        the only signpost that failure gets. (A registry-wide ``isinstance``
        test against ``MouseTrajectoryProtocol`` turns that into a test
        failure instead — see ``test_trajectory.py``.)
        """
        cls: type = type(self)  # the registrar metaclass hides __name__ from pyright
        return NotImplementedError(
            f"{cls.__name__} must implement {member}(): return the neutral "
            f"value only if this strategy is not humanized, otherwise {remedy}."
        )

"""Write the points of execution feedback on the grid a model writes points on.

``docs/architecture_design.md`` §4.1, "模型看到的每个点都写在它写点的那个网格上". The
facts fed back to a model — an executed action's params, its effect (§4.5), the
capture-state pointer (§4.4) — are in the capability's viewport units, and stay
so in the trace. An Agent whose model writes points on a grid (its
``coordinate_space``) shows every one of those points on that grid instead: the
grid of the screenshot the point belongs to, which is the step a batch was
chosen on for the batch and its effects, and this step for the pointer. One
prompt then carries one coordinate system.

A point with no place on the grid is said to be so, never written in another
unit and never clamped onto an edge: the step showed no screenshot of known size
(:data:`NOT_PLACEABLE`), or the point lies outside the area the screenshot
depicts (:data:`OUTSIDE_THE_SCREENSHOT`, where ``PointGrid.to_grid`` would clamp
it and so misstate it).

This module is the writing the Agents share. How the rest of an action reads —
UI-TARS's call syntax, BrowserUse's ``name(params)`` — and where it goes in the
prompt stay each Agent's own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from spectragent.core.action_guard import executed_descriptor
from spectragent.core.actions._registrar import registered_params
from spectragent.core.actions.params import point_params
from spectragent.schemas.action import PointWriter
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.geometry import PointGrid

#: What a point with no place on the grid is written as — never as a number in
#: another unit. The step showed no screenshot of known size (no grid at all),
#: or the point lies outside the area the screenshot depicts.
NOT_PLACEABLE = "not placeable (no screenshot of known size)"
OUTSIDE_THE_SCREENSHOT = "outside the screenshot"


def is_number(value: Any) -> TypeGuard[int | float]:
    """Whether *value* is a number a point can hold (a ``bool`` is not one)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def inside(grid: PointGrid, x: float, y: float) -> bool:
    """Whether the viewport point (x, y) lies in the area *grid*'s screenshot depicts."""
    return 0 <= x < grid.viewport_width and 0 <= y < grid.viewport_height


def placed(grid: PointGrid | None, x: float, y: float) -> tuple[int, int] | str:
    """The viewport point (x, y) on *grid*, or why it has no place there."""
    if grid is None:
        return NOT_PLACEABLE
    if not inside(grid, x, y):
        return OUTSIDE_THE_SCREENSHOT
    return grid.to_grid(x, y)


def grid_point_writer(grid: PointGrid | None) -> PointWriter:
    """Writes a viewport point on *grid* as a reply writes one, ``(x, y)``.

    A point the grid cannot hold (:func:`placed`) is said as such, ``a point
    outside the screenshot``: never clamped, never in another unit.
    """

    def write(x: int, y: int) -> str:
        point = placed(grid, x, y)
        if isinstance(point, str):
            return f"a point {point}"
        return f"({point[0]}, {point[1]})"

    return write


def action_points(space: ActionSpace, name: str) -> list[tuple[str, str]] | None:
    """The ``(x, y)`` params of the action called *name* that are a point, as its action declares.

    The run's own descriptor's ``point_params`` when *name* is an action of
    *space* (a short name through the run's bindings, ``executed_descriptor``);
    else the registered ``Params``' points by the fully-qualified *name* — a
    recorded action this run does not execute, such as a teacher's in the
    training export's ``keep_as_text`` turns. ``None`` when neither knows the
    action: the Agent then states no point for it, and never takes a param
    for a point from its name or value alone.
    """
    descriptor = executed_descriptor(space, name)
    if descriptor is not None:
        return list(descriptor.point_params)
    params = registered_params(name)
    return point_params(params) if params is not None else None


def stated_points(
    params: Mapping[str, Any], pairs: Sequence[tuple[str, str]]
) -> list[tuple[str, str]]:
    """The point params of *pairs* that *params* states: both of the pair hold a number."""
    return [
        (x_key, y_key)
        for x_key, y_key in pairs
        if is_number(params.get(x_key)) and is_number(params.get(y_key))
    ]


def params_on_grid(
    params: Mapping[str, Any], pairs: Sequence[tuple[str, str]], grid: PointGrid | None
) -> dict[str, Any]:
    """A copy of *params* with each point of *pairs* on *grid*, or the reason it has no place there.

    *pairs* are points *params* states (:func:`stated_points`); every other
    param is copied as it is. A point with no place on *grid* has both of its
    params hold the sentence (:func:`placed`).
    """
    written = dict(params)
    for x_key, y_key in pairs:
        point = placed(grid, params[x_key], params[y_key])
        written[x_key], written[y_key] = (point, point) if isinstance(point, str) else point
    return written


@dataclass
class StepGrids:
    """The grid of every step whose prompt an Agent built since ``prepare``, by step number.

    The records ``loop.history`` holds carry no screenshot geometry (the Loop
    knows no screenshot, §4.4), so an Agent that feeds an earlier step's batch
    back on the grid it was chosen on keeps that grid here: ``build_react_prompt``
    records each step's (``None``: that step showed no screenshot of known size),
    ``prepare`` clears them. Per-run state, one instance per Agent.

    Attributes:
        agent: How a refusal names the Agent.
    """

    agent: str
    _grids: dict[int, PointGrid | None] = field(default_factory=dict, init=False, repr=False)

    def clear(self) -> None:
        """Forget every step: ``prepare`` starts a new execution."""
        self._grids.clear()

    def record(self, step: int, grid: PointGrid | None) -> None:
        """Step *step*'s prompt showed the screenshot *grid* lies over (``None``: none of known size)."""
        self._grids[step] = grid

    def grid_of(self, step: int) -> PointGrid | None:
        """The grid of the screenshot step *step* showed; ``None`` when it had no known size.

        Raises:
            ValueError: The Agent did not build step *step*'s prompt since
                ``prepare``. Every rendering path builds each step's prompt
                before it feeds that step's batch back, so this is a caller
                that skipped a step, never something recorded data can cause.
        """
        if step not in self._grids:
            raise ValueError(
                f"{self.agent} cannot write the points of step {step}'s actions: it did not "
                f"build step {step}'s prompt since prepare, so it does not know the grid of "
                f"the screenshot they were chosen on (prompts are built in step order)"
            )
        return self._grids[step]

    def point_writer(self, step: int) -> PointWriter:
        """Writes a point of step *step*'s batch on that step's grid, looked up when a point is written.

        Looked up late, so a batch that states no point needs no grid.
        """

        def write(x: int, y: int) -> str:
            return grid_point_writer(self.grid_of(step))(x, y)

        return write


__all__ = [
    "NOT_PLACEABLE",
    "OUTSIDE_THE_SCREENSHOT",
    "StepGrids",
    "action_points",
    "grid_point_writer",
    "inside",
    "is_number",
    "params_on_grid",
    "placed",
    "stated_points",
]

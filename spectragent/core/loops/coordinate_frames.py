"""Coordinate frames: the framework moves every point between a model's grid and a capability's units.

``docs/architecture_design.md`` §4.1, "坐标系归框架". A capability executes
points in its own units (CSS px of the viewport for the browser) and records
them so in the trace. A model reads its points off a screenshot, on a grid of
its own: the screenshot's pixels, or 0-1000 over it
(:data:`~spectragent.schemas.observation.geometry.CoordinateSpace`). An Agent
whose model writes points on such a grid declares which, as its
``coordinate_space`` (:class:`DeclaresCoordinateFrame`). That is the Agent's
whole part; this module is the rest, and it has one per-step object, the
step's :class:`~spectragent.schemas.observation.geometry.PointFrame` — the
declared grid laid over the screenshot that step showed the model.

- **The run.** ``AgentRuntime.prepare`` frames the run's action space
  (:func:`framed_action_space`): when the space holds a point action it carries
  the declared grid, and each point param's descriptor text says what the
  model writes on it. A run without a point action never asks its model for a
  point, so it is taught no grid and keeps the capability's units. A framed
  run whose negotiated screenshot cannot report its geometry, or that
  negotiates none, could never place a point; ``prepare`` refuses it there
  (:func:`check_frame_geometry`), before a trace is opened.
- **Each step.** The Loop builds the frame of the observation a step decides
  on (:func:`step_frame`) before the Agent builds the prompt, hands it over in
  ``ReactPromptContext.frame`` and records it on ``StepRecord.frame``.
- **In.** The Loop moves a parsed reply's points into the capability's units
  (:func:`actions_in_capability_units`) before anything checks or runs them.
  At the parse boundary rather than in the guard's executor: a point with no
  place is a reply for the model to rewrite (a ``ParseError``, through the
  parse retries), which an executor answering with an ``ActionResult`` cannot
  ask for; and the ``Params`` check must see the values that will execute.
- **Out.** Every point shown to a model is written on the frame of the step it
  belongs to, read off that step's record: an action shown as a fact
  (:func:`fact_in_model_frame`), an effect line or the pointer
  (:func:`point_writer`), and a reply the training export writes for a student
  (:func:`actions_in_model_frame`).

A point with no place on a frame is said to be so, never written in another
unit and never clamped onto an edge: the step showed no screenshot of known
size (:data:`NOT_PLACEABLE`), or the point lies outside the area the
screenshot depicts (:data:`OUTSIDE_THE_SCREENSHOT`). Which params are a point
is the action's own declaration (``PointAxis``, :func:`action_points`), never a
param's name or value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeGuard, get_args, runtime_checkable

from spectragent.core.action_guard import executed_descriptor
from spectragent.core.actions._registrar import registered_params
from spectragent.core.actions.params import point_params
from spectragent.core.loops.errors import ParseError, UnrepresentableActionError
from spectragent.schemas.action import Action, ActionDescriptor, PointWriter, write_viewport_point
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation._registrar import ObservationRegistrar
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.geometry import (
    CoordinateSpace,
    PointFrame,
    ScreenshotGeometry,
    point_grid,
)
from spectragent.schemas.observation.image import read_model_image

__all__ = [
    "NOT_PLACEABLE",
    "OUTSIDE_THE_SCREENSHOT",
    "SCREENSHOT_FIELD",
    "DeclaresCoordinateFrame",
    "ScreenshotGeometryError",
    "action_points",
    "actions_in_capability_units",
    "actions_in_model_frame",
    "check_frame_geometry",
    "declared_space",
    "fact_in_model_frame",
    "framed_action_space",
    "point_writer",
    "step_frame",
]

#: What a point with no place on its frame is written as — never a number in
#: another unit. The step showed no screenshot of known size (the frame has no
#: grid), or the point lies outside the area the screenshot depicts.
NOT_PLACEABLE = "not placeable (no screenshot of known size)"
OUTSIDE_THE_SCREENSHOT = "outside the screenshot"

#: The observation field a frame lies over: the screenshot the model is shown.
#: Every capability's screenshot fragment is its ``<capability>.screenshot``
#: (``browser.screenshot``; a desktop or mobile one likewise) and reports
#: :class:`~spectragent.schemas.observation.geometry.ScreenshotGeometry`.
SCREENSHOT_FIELD = "screenshot"

#: What a point param says on each grid, after its type: ``{axis}`` is ``x`` or
#: ``y`` and ``{edge}`` the edge it is measured from.
_AXIS_PROSE: dict[CoordinateSpace, str] = {
    "screenshot_pixels": "{axis} in pixels of the screenshot you are shown, from its {edge} edge",
    "normalized_1000": (
        "{axis} on a 0-1000 grid laid over the screenshot whatever its pixel size, "
        "from its {edge} edge"
    ),
}


@runtime_checkable
class DeclaresCoordinateFrame(Protocol):
    """An Agent whose model writes points on a grid of its own, and says which.

    The declaration is the Agent's whole part in coordinates: the framework
    frames its run's action space, builds each step's frame and converts every
    point (module docstring). An Agent without it writes points in the
    capability's units, if it writes any.
    """

    coordinate_space: CoordinateSpace


def declared_space(agent: Any) -> CoordinateSpace | None:
    """The grid *agent*'s model writes points on (:class:`DeclaresCoordinateFrame`), or ``None``.

    Raises:
        TypeError: *agent* has a ``coordinate_space`` that names no grid.
    """
    space = getattr(agent, "coordinate_space", None)
    if space is None:
        return None
    if space not in get_args(CoordinateSpace):
        raise TypeError(
            f"{type(agent).__name__} declares coordinate_space={space!r}, which is not a "
            f"grid points are written on: one of {', '.join(get_args(CoordinateSpace))}"
        )
    return space


def _point_line(prose: str, space: CoordinateSpace, axis: str) -> str:
    """A point param's descriptor line, its unit the grid's: the type kept, the rest rewritten."""
    head = prose.split(" -- ", 1)[0]
    edge = "left" if axis == "x" else "top"
    return f"{head} -- {_AXIS_PROSE[space].format(axis=axis, edge=edge)}"


def _framed_descriptor(descriptor: ActionDescriptor, space: CoordinateSpace) -> ActionDescriptor:
    """*descriptor* with each point param said in *space*'s units, in the prose and the schema."""
    if not descriptor.point_params:
        return descriptor
    lines = {
        key: _point_line(descriptor.params.get(key, "int"), space, axis)
        for pair in descriptor.point_params
        for key, axis in zip(pair, ("x", "y"), strict=True)
    }
    params = {key: lines.get(key, text) for key, text in descriptor.params.items()}
    schema = descriptor.params_schema
    if schema is not None:
        properties = {
            key: {**node, "description": lines[key]} if key in lines else node
            for key, node in (schema.get("properties") or {}).items()
        }
        schema = {**schema, "properties": properties}
    return descriptor.model_copy(update={"params": params, "params_schema": schema})


def framed_action_space(
    space: ActionSpace, coordinate_space: CoordinateSpace | None
) -> ActionSpace:
    """*space* as the run's model is taught it: framed on *coordinate_space* when it has a point action.

    The space then carries the grid (``ActionSpace.coordinate_space``) and each
    point param's descriptor text — the prompt's and the schema's, one line —
    says what the model writes on it, so every Agent and every error that
    quotes a descriptor speaks the model's units. Names, bindings and which
    params are points do not change, and the guard executes the capability's
    units as before: the Loop converts on the way in. Unchanged when
    *coordinate_space* is ``None`` or the space holds no point action — a run
    whose model writes no point is taught no grid.
    """
    if coordinate_space is None or not any(d.point_params for d in space.descriptors):
        return space
    return space.model_copy(
        update={
            "descriptors": [_framed_descriptor(d, coordinate_space) for d in space.descriptors],
            "coordinate_space": coordinate_space,
        }
    )


#: What a screenshot fragment has to carry for a point on it to be executed.
_GEOMETRY_FIELDS = tuple(ScreenshotGeometry.__annotations__)

#: Said wherever a screenshot reports no geometry: what it has to carry.
_GEOMETRY_NEEDED = (
    "A screenshot fragment has to carry image_width, image_height, viewport_width and "
    "viewport_height (ScreenshotGeometry, spectragent.schemas.observation.geometry)."
)


class ScreenshotGeometryError(TypeError):
    """A run framed on a grid, and no screenshot of it can say where a point on that grid is.

    Raised by :func:`check_frame_geometry` in ``AgentRuntime.prepare``. A
    ``TypeError`` like the step-level refusal in :func:`step_frame`: both are
    an assembly error, not something the model can fix by rewriting a reply.
    """


def check_frame_geometry(
    space: ActionSpace, fragment_bindings: Mapping[str, str], *, agent: str
) -> None:
    """Refuse a run framed on a grid that none of its steps could lay over a screenshot.

    Run by ``AgentRuntime.prepare`` right after :func:`framed_action_space`
    (AGENTS.md §2.3, "what setup would refuse, ``prepare()`` refuses"), so a
    configuration that can never place a point fails before a trace is opened,
    not at the first step. Nothing to check when *space* is framed on no grid.

    Args:
        space: The run's framed action space.
        fragment_bindings: The observation negotiation's local name →
            fully-qualified key (``ObservationNegotiationResult.fragment_bindings``).
        agent: How the refusal names the agent.

    Raises:
        ScreenshotGeometryError: The run negotiates no screenshot
            (:data:`SCREENSHOT_FIELD`), or the registered fragment it negotiates
            for one does not carry :class:`ScreenshotGeometry`'s fields.
    """
    coordinate_space = space.coordinate_space
    if coordinate_space is None:
        return
    pointing = ", ".join(sorted(d.name for d in space.descriptors if d.point_params))
    head = f"{agent} writes points on the {coordinate_space} grid and this run's {pointing} take points"
    key = fragment_bindings.get(SCREENSHOT_FIELD)
    if key is None:
        raise ScreenshotGeometryError(
            f"{head}, but the run negotiates no screenshot to lay the grid over, so no point "
            f"the model writes can be placed. {_GEOMETRY_NEEDED}"
        )
    fragment = ObservationRegistrar.get_or_none(key)
    fields = getattr(fragment, "model_fields", {})
    if fragment is not None and all(name in fields for name in _GEOMETRY_FIELDS):
        return
    shown = fragment.__name__ if fragment is not None else "no registered fragment class"
    raise ScreenshotGeometryError(
        f"{head}, but the screenshot it negotiates, {key} ({shown}), reports no screenshot "
        f"geometry, so no point the model writes on it can be executed. {_GEOMETRY_NEEDED}"
    )


def step_frame(space: ActionSpace, observation: Any) -> PointFrame | None:
    """The frame the points of a step deciding on *observation* are written in.

    ``None`` when *space* is framed on no grid (its points are the
    capability's units). Otherwise the space's grid over the observation's
    screenshot (:data:`SCREENSHOT_FIELD`) — the one the model is shown, so a
    screenshot without image bytes, a failed optional one or none at all is a
    frame without a grid, on which no point has a place.

    Raises:
        TypeError: The screenshot reports no geometry. That is an assembly
            error — no point on it could ever be executed — so it stops the
            step rather than refusing every point the model writes. A run
            assembled by ``AgentRuntime.prepare`` has had its negotiated
            screenshot checked already (:func:`check_frame_geometry`).
    """
    coordinate_space = space.coordinate_space
    if coordinate_space is None:
        return None
    screenshot = getattr(observation, SCREENSHOT_FIELD, None) if observation is not None else None
    if screenshot is None or isinstance(screenshot, FragmentFailure):
        return PointFrame(space=coordinate_space)
    if not isinstance(screenshot, ScreenshotGeometry):
        raise TypeError(
            f"{type(screenshot).__name__} reports no screenshot geometry, so no point the "
            f"model writes on it can be executed. {_GEOMETRY_NEEDED}"
        )
    if read_model_image(screenshot) is None:
        return PointFrame(space=coordinate_space)
    return PointFrame(space=coordinate_space, grid=point_grid(coordinate_space, screenshot))


def action_points(space: ActionSpace, name: str) -> list[tuple[str, str]]:
    """The ``(x, y)`` params of the action called *name* that are a point, as its action declares.

    The run's own descriptor's ``point_params`` when *name* is an action of
    *space* (a short name through the run's bindings, ``executed_descriptor``);
    else the registered ``Params``' points by the fully-qualified *name* — a
    recorded action this run does not execute, such as a teacher's in the
    training export. ``[]`` when neither knows the action: no param is ever
    taken for a point from its name or value alone.
    """
    descriptor = executed_descriptor(space, name)
    if descriptor is not None:
        return list(descriptor.point_params)
    params = registered_params(name)
    return point_params(params) if params is not None else []


def _is_number(value: Any) -> TypeGuard[int | float]:
    """Whether *value* is a number a point can hold (a ``bool`` is not one)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _stated(action: Action, space: ActionSpace) -> list[tuple[str, str]]:
    """The point params of *action* that it states: both of the pair hold a number."""
    return [
        (x_key, y_key)
        for x_key, y_key in action_points(space, action.name)
        if _is_number(action.params.get(x_key)) and _is_number(action.params.get(y_key))
    ]


def actions_in_capability_units(
    actions: Sequence[Action],
    frame: PointFrame | None,
    space: ActionSpace,
    *,
    raw_output: str | None = None,
) -> list[Action]:
    """A parsed reply's *actions*, each point moved from *frame* into the capability's units.

    The way in, applied by the Loop to whatever the Agent's parser returns,
    before the params are checked or anything runs: the guard, the action and
    the trace see the capability's units only. A point off the grid is clamped
    onto the viewport (``PointGrid.to_viewport``); a param pair that is not two
    numbers is left for the params check to refuse. Unchanged when *frame* is
    ``None``: the run's model writes the capability's units.

    Raises:
        ParseError: An action states a point and the step showed no screenshot
            of known size. That goes back to the model, never a guessed point.
            It carries *raw_output*, the reply it was read from.
    """
    if frame is None:
        return list(actions)
    converted: list[Action] = []
    unplaced: list[str] = []
    for position, action in enumerate(actions, start=1):
        pairs = _stated(action, space)
        if not pairs:
            converted.append(action)
            continue
        grid = frame.grid
        if grid is None:
            unplaced.append(f"action {position}, {action.name}")
            continue
        params = dict(action.params)
        for x_key, y_key in pairs:
            params[x_key], params[y_key] = grid.to_viewport(params[x_key], params[y_key])
        converted.append(action.model_copy(update={"params": params}))
    if unplaced:
        raise ParseError(
            f"{'; '.join(unplaced)}: a point cannot be placed, because this step's "
            f"screenshot has no known size",
            raw_output=raw_output,
        )
    return converted


def actions_in_model_frame(
    actions: Sequence[Action], frame: PointFrame | None, space: ActionSpace
) -> list[Action]:
    """*actions*, recorded in the capability's units, with each point on *frame* — to write a reply.

    What the training export hands a student's ``format_react_output``: the
    reply it writes must execute as the recorded action, so a point that has
    no place on the frame is a refusal, never a written guess. Unchanged when
    *frame* is ``None``.

    Raises:
        UnrepresentableActionError: An action states a point and the step
            showed no screenshot of known size, or the point lies outside it.
    """
    if frame is None:
        return list(actions)
    written: list[Action] = []
    for action in actions:
        pairs = _stated(action, space)
        if not pairs:
            written.append(action)
            continue
        grid = frame.grid
        if grid is None:
            raise UnrepresentableActionError(
                action.name,
                [key for pair in pairs for key in pair],
                "this step showed no screenshot of known size, so no point can be written",
            )
        params = dict(action.params)
        for x_key, y_key in pairs:
            x, y = params[x_key], params[y_key]
            if not grid.holds(x, y):
                raise UnrepresentableActionError(
                    action.name,
                    (x_key, y_key),
                    f"({x}, {y}) is outside the {grid.viewport_width}x{grid.viewport_height} "
                    f"viewport the screenshot depicts",
                )
            params[x_key], params[y_key] = grid.to_grid(x, y)
        written.append(action.model_copy(update={"params": params}))
    return written


def _placed(frame: PointFrame, x: float, y: float) -> tuple[int, int] | str:
    """The viewport point (x, y) on *frame*'s grid, or why it has no place there."""
    grid = frame.grid
    if grid is None:
        return NOT_PLACEABLE
    if not grid.holds(x, y):
        return OUTSIDE_THE_SCREENSHOT
    return grid.to_grid(x, y)


def fact_in_model_frame(action: Action, frame: PointFrame | None, space: ActionSpace) -> Action:
    """*action*, recorded in the capability's units, as a fact shown to the model on *frame*.

    Each point the action states is on the frame's grid, or — where it has no
    place there — both of its params hold the sentence that says why
    (:data:`NOT_PLACEABLE`, :data:`OUTSIDE_THE_SCREENSHOT`), so a fact never
    stops a prompt from being built and is never written in pixels. Every
    other param, and the name, are as recorded. Unchanged when *frame* is
    ``None``.
    """
    if frame is None:
        return action
    pairs = _stated(action, space)
    if not pairs:
        return action
    params = dict(action.params)
    for x_key, y_key in pairs:
        point = _placed(frame, params[x_key], params[y_key])
        params[x_key], params[y_key] = (point, point) if isinstance(point, str) else point
    return action.model_copy(update={"params": params})


def point_writer(
    frame: PointFrame | None, *, viewport: PointWriter = write_viewport_point
) -> PointWriter:
    """Writes a viewport point stated by a fact of a step framed by *frame*, as the model reads it.

    On the frame's grid as a reply writes one, ``(x, y)``; a point with no
    place there is said as such, ``a point outside the screenshot``. When
    *frame* is ``None`` the run's points are the capability's units and
    *viewport* writes them (the default, ``(x, y)``; the pointer's writer adds
    the unit).
    """
    if frame is None:
        return viewport

    def write(x: int, y: int) -> str:
        point = _placed(frame, x, y)
        if isinstance(point, str):
            return f"a point {point}"
        return f"({point[0]}, {point[1]})"

    return write

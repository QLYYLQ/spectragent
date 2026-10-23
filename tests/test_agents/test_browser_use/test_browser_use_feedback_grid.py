"""BrowserUse is shown every point on the grid its model writes points on.

``docs/architecture_design.md`` §4.1, "模型看到的每个点都写在它写点的那个网格上"
and "坐标系归框架", the contract UI-TARS keeps (``test_ui_tars_feedback_grid.py``).
The facts stay in the capability's viewport units (CSS px for a browser); a
BrowserUse run with a coordinate action teaches its model a grid
(``coordinate_space``), and the framework writes each point it is shown — an
executed action's params, an effect line, the pointer — on that grid, from the
frame recorded on the step the point belongs to. Each step is driven as a Loop
drives it (``tests/_helpers/frames.py``):

- each step in the history is written on the frame its record keeps, its
  effect lines too; the pointer on this step's frame;
- ``screenshot_pixels`` writes image pixels: the old numbers at DPR 1, twice
  the viewport's at DPR 2;
- a point BrowserUse shows reads back to the viewport point it stands for,
  within the grid's quantum;
- restored history writes executed and recorded actions on the restored
  observation's frame, with no CSS-pixel disclaimer;
- an earlier step's frame comes from its record: no agent state;
- a point with no place on the grid is said to be so, never written in pixels
  and never clamped onto an edge;
- which params are points comes from the action's registered ``Params``;
- a run with no coordinate action writes no point and is taught no grid, so its
  feedback keeps viewport pixels.
"""

from __future__ import annotations

import ast
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.effects import ElementFacts, PointerEffect, ViewportPoint
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
from spectragent.core.llm.messages import BaseMessage, ContentPartTextParam
from spectragent.core.loops.coordinate_frames import (
    NOT_PLACEABLE,
    OUTSIDE_THE_SCREENSHOT,
    step_frame,
)
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.capture_state import FailedVisibleResources, PointerState
from spectragent.schemas.observation.fragments import HistoryObs, StepInfoObs, TaskObs
from spectragent.schemas.observation.geometry import CoordinateSpace, PointFrame, point_grid
from spectragent.schemas.prompt import RestoredHistory
from spectragent.schemas.restoration import RestoredAction
from spectragent.schemas.step_record import StepRecord
from tests._helpers.action_spaces import space_for
from tests._helpers.assembly import make_agent
from tests._helpers.frames import decided, step_context

VIEWPORT = (1280, 720)
AMOUNT = ElementFacts(role="textbox", name="Amount")
GUIDE = "Every point in the agent history and the browser state is on this same grid."


def _screenshot(image: tuple[int, int] = VIEWPORT, viewport: tuple[int, int] = VIEWPORT) -> Any:
    return ScreenshotObs(
        image_bytes=b"\x89PNG\r\n\x1a\n",
        image_width=image[0],
        image_height=image[1],
        viewport_width=viewport[0],
        viewport_height=viewport[1],
    )


def _click(x: int, y: int) -> Action:
    return Action(name="browser.click_coordinate", params={"coordinate_x": x, "coordinate_y": y})


def _clicked(x: int, y: int, effect_id: str = "browser.click_coordinate") -> ActionResult:
    effect = PointerEffect(
        effect_id=effect_id, point=ViewportPoint(x=x, y=y), hit=AMOUNT, focus_after=AMOUNT
    )
    return ActionResult(success=True, effect=effect)


def _batch(frame: PointFrame | None, step: int = 0, x: int = 640, y: int = 618) -> StepRecord:
    """Step *step*, framed by *frame*, clicked the viewport's (x, y), a text field, and landed there."""
    return StepRecord(
        step=step,
        frame=frame,
        llm_output="reply",
        actions=[_click(x, y)],
        action_results=[_clicked(x, y)],
    )


def _observation(
    *,
    history: list[StepRecord] | None = None,
    screenshot: Any = None,
    pointer: tuple[int, int] = (640, 618),
) -> SimpleNamespace:
    return SimpleNamespace(
        task=TaskObs(user_request="Enter the amount"),
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        history=HistoryObs(items=history or []),
        step_info=StepInfoObs(step=2, max_steps=10, today="2026-10-22"),
        dom=SimpleNamespace(element_tree="[3]<input>Amount</input>"),
        browser_meta=BrowserMetaObs(url="https://f.test/", page_title="F", tabs=[]),
        screenshot=screenshot if screenshot is not None else _screenshot(),
        capture_state=SimpleNamespace(
            settle_state=None,
            foreground_change=None,
            pointer=PointerState(status="known", x=pointer[0], y=pointer[1]),
            failed_visible_resources=FailedVisibleResources(status="measured", count=0),
        ),
    )


class _Run:
    """BrowserUse (``vision`` unless *config* says otherwise) with the actions its mode negotiates.

    Prepared on the framed space a run gives it; each step built as a Loop builds it.
    """

    def __init__(self, **config: Any) -> None:
        config.setdefault("observation_mode", "vision")
        self.agent = make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=20), **config)
        required, optional = self.agent.negotiation_demand("action")
        descriptors = [
            action.descriptor(fragments=frozenset({"browser.dom", "browser.screenshot"}))
            for action in BrowserCapability().actions
            if action.action_id.split(".")[-1] in required | optional
        ]
        self.space: ActionSpace = space_for(self.agent, descriptors)
        self.seed: list[BaseMessage] = self.agent.prepare(
            task="Enter the amount",
            action_space=self.space,
            execution_limits=ExecutionLimits(max_actions_per_step=2),
        )

    def frame(self, screenshot: Any = None) -> PointFrame | None:
        """The frame a step showing *screenshot* is decided on."""
        return step_frame(self.space, _observation(screenshot=screenshot))

    async def build(self, observation: Any, *, step: int) -> list[BaseMessage]:
        context = step_context(self.space, observation, step=step)
        return await self.agent.build_react_prompt(observation, context=context)

    async def restore(
        self, observation: Any, history: RestoredHistory, *, step: int = 0
    ) -> list[BaseMessage]:
        context = step_context(self.space, observation, step=step, phase="history")
        return await self.agent.build_restored_history(
            observation, history=history, context=context
        )


def _text(messages: list[BaseMessage]) -> str:
    texts: list[str] = []
    for message in messages:
        if isinstance(message.content, str):
            texts.append(message.content)
        elif message.content is not None:
            texts.extend(p.text for p in message.content if isinstance(p, ContentPartTextParam))
    return "\n".join(texts)


def _step_block(messages: list[BaseMessage], step: int) -> str:
    """The history's ``<step_N>`` block for 0-based step *step*."""
    tag = f"step_{step + 1}"
    return _text(messages).split(f"<{tag}>\n")[1].split(f"\n</{tag}>")[0]


def _actions(messages: list[BaseMessage], step: int = 0) -> list[tuple[str, dict[str, Any]]]:
    """Each action of step *step* as the history writes it: its name and params."""
    line = _step_block(messages, step).split("<actions>")[1].split("</actions>")[0]
    return [
        (name, ast.literal_eval(params))
        for name, params in re.findall(r"([\w.]+)\((\{[^}]*\})\)", line)
    ]


def _effects(messages: list[BaseMessage], step: int = 0) -> list[str]:
    block = _step_block(messages, step).split("<effects>\n")[1].split("\n</effects>")[0]
    return block.splitlines()


def _pointer_line(messages: list[BaseMessage]) -> str:
    return next(line for line in _text(messages).splitlines() if line.startswith("Pointer: "))


async def _second_step(
    run: _Run,
    *,
    first: Any = None,
    second: Any = None,
    pointer: tuple[int, int] = (640, 618),
    x: int = 640,
    y: int = 618,
) -> list[BaseMessage]:
    """Build step 0 on *first*, then step 1 on *second*, its history holding step 0's click."""
    await run.build(_observation(screenshot=first), step=0)
    return await run.build(
        _observation(
            history=[_batch(run.frame(first), x=x, y=y)], screenshot=second, pointer=pointer
        ),
        step=1,
    )


# ── normalized_1000: the case a teacher model got wrong in UI-TARS ─────


async def test_a_click_is_fed_back_on_the_0_1000_grid() -> None:
    run = _Run(coordinate_space="normalized_1000")
    step = await _second_step(run)
    grid = point_grid("normalized_1000", _screenshot())
    assert grid is not None
    point = grid.to_grid(640, 618)
    assert point == (500, 858)
    assert _actions(step) == [
        ("browser.click_coordinate", {"coordinate_x": 500, "coordinate_y": 858})
    ]
    assert _effects(step) == [
        '1. browser.click_coordinate: Clicked at (500, 858) on textbox "Amount"; '
        'focus is now on textbox "Amount".'
    ]
    assert _pointer_line(step) == "Pointer: (500, 858)."
    everything = _text([*run.seed, *step])
    assert "viewport pixels" not in everything
    assert "(640, 618)" not in everything
    assert "'coordinate_x': 640" not in everything


@pytest.mark.parametrize("space", ["normalized_1000", "screenshot_pixels"])
@pytest.mark.parametrize("variant", [{}, {"flash_mode": False}])
def test_the_system_prompt_puts_feedback_points_on_the_models_grid(
    space: CoordinateSpace, variant: dict[str, Any]
) -> None:
    system = _text(_Run(coordinate_space=space, **variant).seed)
    assert GUIDE in system
    assert "viewport pixels" not in system


# ── screenshot_pixels: image pixels, whatever the device scale factor ──


@pytest.mark.parametrize(
    ("image", "written"),
    [
        pytest.param(VIEWPORT, (640, 618), id="dpr1-the-numbers-as-before"),
        pytest.param((2560, 1440), (1280, 1236), id="dpr2-image-pixels"),
    ],
)
async def test_screenshot_pixels_feed_back_image_pixels(
    image: tuple[int, int], written: tuple[int, int]
) -> None:
    run = _Run(coordinate_space="screenshot_pixels")
    step = await _second_step(run, first=_screenshot(image), second=_screenshot(image))
    assert _actions(step) == [
        ("browser.click_coordinate", {"coordinate_x": written[0], "coordinate_y": written[1]})
    ]
    (effect,) = _effects(step)
    assert effect.startswith(f"1. browser.click_coordinate: Clicked at {written} on ")
    assert _pointer_line(step) == f"Pointer: {written}."


# ── What BrowserUse shows is what it would execute ────────────────────


@pytest.mark.parametrize("space", ["normalized_1000", "screenshot_pixels"])
@pytest.mark.parametrize("image", [VIEWPORT, (2560, 1440), (1000, 563)])
@pytest.mark.parametrize("point", [(0, 0), (640, 618), (1279, 719), (333, 7)])
async def test_a_fed_back_point_parses_back_to_where_it_ran(
    space: CoordinateSpace, image: tuple[int, int], point: tuple[int, int]
) -> None:
    run = _Run(coordinate_space=space)
    shot = _screenshot(image)
    step = await _second_step(run, first=shot, second=shot, x=point[0], y=point[1])
    ((name, params),) = _actions(step)
    reply = json.dumps({"memory": "m", "action": [{name: params}]})
    context = step_context(run.space, _observation(screenshot=shot), step=1)
    (action,) = decided(run.agent, run.space, reply, context)
    grid = point_grid(space, shot)
    assert grid is not None
    assert abs(action.params["coordinate_x"] - point[0]) <= grid.quantum
    assert abs(action.params["coordinate_y"] - point[1]) <= grid.quantum
    # The effect line names the same grid point the action does.
    (effect,) = _effects(step)
    written = (params["coordinate_x"], params["coordinate_y"])
    assert effect.startswith(f"1. browser.click_coordinate: Clicked at {written} on ")


async def test_a_drag_writes_both_points_on_the_grid() -> None:
    run = _Run(coordinate_space="normalized_1000")
    drag = Action(
        name="browser.drag", params={"start_x": 640, "start_y": 618, "end_x": 128, "end_y": 72}
    )
    effect = PointerEffect(
        effect_id="browser.drag",
        point=ViewportPoint(x=640, y=618),
        release_point=ViewportPoint(x=128, y=72),
        hit=AMOUNT,
        focus_after=AMOUNT,
    )
    record = StepRecord(
        step=0,
        frame=run.frame(),
        actions=[drag],
        action_results=[ActionResult(success=True, effect=effect)],
    )
    step = await run.build(_observation(history=[record]), step=1)
    assert _actions(step) == [
        ("browser.drag", {"start_x": 500, "start_y": 858, "end_x": 100, "end_y": 100})
    ]
    (line,) = _effects(step)
    assert line.startswith("1. browser.drag: Dragged from (500, 858) to (100, 100), pressing on ")


async def test_an_index_click_states_where_it_landed_on_the_grid() -> None:
    """``dom_vision``: the index is no point and stays; the measured landing point moves."""
    run = _Run(observation_mode="dom_vision", coordinate_space="normalized_1000")
    clicked = StepRecord(
        step=0,
        frame=run.frame(),
        actions=[Action(name="browser.click", params={"index": 3})],
        action_results=[_clicked(640, 618, effect_id="browser.click")],
    )
    step = await run.build(_observation(history=[clicked]), step=1)
    assert _actions(step) == [("browser.click", {"index": 3})]
    (effect,) = _effects(step)
    assert effect.startswith("1. browser.click: Clicked at (500, 858) on ")
    assert _pointer_line(step) == "Pointer: (500, 858)."


# ── Whose frame ───────────────────────────────────────────────────────


async def test_a_step_is_written_on_the_frame_it_was_chosen_on_and_the_pointer_on_this_one() -> (
    None
):
    """The viewport changed between the steps: each point uses its own screenshot's frame."""
    run = _Run(coordinate_space="normalized_1000")
    step = await _second_step(
        run,
        first=_screenshot(VIEWPORT, VIEWPORT),
        second=_screenshot((1000, 500), (1000, 500)),
        pointer=(500, 250),
    )
    assert _actions(step) == [
        ("browser.click_coordinate", {"coordinate_x": 500, "coordinate_y": 858})
    ]  # step 0's 1280x720
    assert _pointer_line(step) == "Pointer: (500, 500)."  # step 1's 1000x500


async def test_every_step_in_the_history_keeps_its_own_frame() -> None:
    """BrowserUse shows several past steps; each is written on the frame its record keeps."""
    run = _Run(coordinate_space="screenshot_pixels")
    shots = [_screenshot(VIEWPORT), _screenshot((2560, 1440)), _screenshot(VIEWPORT)]
    history: list[StepRecord] = []
    step: list[BaseMessage] = []
    for number, shot in enumerate(shots):
        step = await run.build(_observation(history=list(history), screenshot=shot), step=number)
        history.append(_batch(run.frame(shot), step=number))
    assert _actions(step, 0) == [
        ("browser.click_coordinate", {"coordinate_x": 640, "coordinate_y": 618})
    ]
    assert _actions(step, 1) == [
        ("browser.click_coordinate", {"coordinate_x": 1280, "coordinate_y": 1236})
    ]


async def test_a_past_step_is_written_from_its_record_with_no_agent_state() -> None:
    """An agent that never built step 0 renders it the same: the frame is on the record."""
    built = _Run(coordinate_space="normalized_1000")
    fresh = _Run(coordinate_space="normalized_1000")
    after_step_0 = await _second_step(built)
    alone = await fresh.build(_observation(history=[_batch(built.frame())]), step=1)
    assert _text(alone) == _text(after_step_0)


async def test_a_step_without_points_writes_the_same_on_any_frame() -> None:
    run = _Run(coordinate_space="normalized_1000")
    for frame in (run.frame(), PointFrame(space="normalized_1000")):
        scrolled = StepRecord(
            step=0,
            frame=frame,
            actions=[Action(name="browser.scroll", params={"direction": "down"})],
            action_results=[ActionResult(success=True)],
        )
        step = await run.build(_observation(history=[scrolled]), step=1)
        assert _actions(step) == [("browser.scroll", {"direction": "down"})]


# ── A point with no place on the grid ─────────────────────────────────


async def test_a_step_that_showed_no_screenshot_of_known_size_is_not_placed() -> None:
    """No grid to write on: the points say so, and the prompt still builds."""
    run = _Run(coordinate_space="normalized_1000")
    unsized = run.frame(_screenshot(viewport=(0, 0)))
    step = await run.build(_observation(history=[_batch(unsized)]), step=1)
    assert _actions(step) == [
        (
            "browser.click_coordinate",
            {"coordinate_x": NOT_PLACEABLE, "coordinate_y": NOT_PLACEABLE},
        )
    ]
    (effect,) = _effects(step)
    assert effect.startswith(f"1. browser.click_coordinate: Clicked at a point {NOT_PLACEABLE} on ")
    assert "640" not in _text(step) and "618" not in _text(step)


async def test_a_point_outside_the_screenshot_is_said_to_be_outside_never_clamped() -> None:
    """``to_grid`` would put (1500, 618) on the right edge; it is not there."""
    run = _Run(coordinate_space="normalized_1000")
    step = await run.build(
        _observation(history=[_batch(run.frame(), x=1500)], pointer=(-3, 618)), step=1
    )
    assert _actions(step) == [
        (
            "browser.click_coordinate",
            {"coordinate_x": OUTSIDE_THE_SCREENSHOT, "coordinate_y": OUTSIDE_THE_SCREENSHOT},
        )
    ]
    (effect,) = _effects(step)
    assert effect.startswith(
        f"1. browser.click_coordinate: Clicked at a point {OUTSIDE_THE_SCREENSHOT} on "
    )
    assert _pointer_line(step) == f"Pointer: a point {OUTSIDE_THE_SCREENSHOT}."
    assert "1000" not in _text(step) and "1500" not in _text(step)


async def test_the_pointer_on_a_step_without_a_grid_is_not_written_in_pixels() -> None:
    run = _Run(coordinate_space="normalized_1000")
    step = await run.build(_observation(screenshot=_screenshot(viewport=(0, 0))), step=0)
    assert _pointer_line(step) == f"Pointer: a point {NOT_PLACEABLE}."


# ── Which params are points ───────────────────────────────────────────


async def test_only_declared_point_params_move_onto_the_grid() -> None:
    """An ``_x`` / ``_y`` pair the registry does not declare a point is left as it is."""
    run = _Run(coordinate_space="normalized_1000")
    zoom = Action(name="browser.zoom", params={"scale_x": 640, "scale_y": 618})
    record = StepRecord(
        step=0, frame=run.frame(), actions=[zoom], action_results=[ActionResult(success=True)]
    )
    step = await run.build(_observation(history=[record]), step=1)
    assert _actions(step) == [("browser.zoom", {"scale_x": 640, "scale_y": 618})]


async def test_a_relative_action_name_finds_its_points_through_the_runs_binding() -> None:
    run = _Run(coordinate_space="normalized_1000")
    typed = Action(
        name="type_coordinate",
        params={"coordinate_x": 640, "coordinate_y": 618, "text": "12", "clear": True},
    )
    record = StepRecord(
        step=0, frame=run.frame(), actions=[typed], action_results=[ActionResult(success=True)]
    )
    step = await run.build(_observation(history=[record]), step=1)
    assert _actions(step) == [
        (
            "type_coordinate",
            {"coordinate_x": 500, "coordinate_y": 858, "text": "12", "clear": True},
        )
    ]


# ── Restored history ──────────────────────────────────────────────────


def _restored() -> RestoredHistory:
    recorded, executed = _click(641, 617), _click(640, 618)
    result = _clicked(640, 618)
    return RestoredHistory(
        source_run_id="source-run",
        source_step=0,
        source_reply=json.dumps(
            {
                "memory": "the amount field",
                "action": [
                    {"browser.click_coordinate": {"coordinate_x": 501, "coordinate_y": 857}}
                ],
            }
        ),
        source_actions=[recorded],
        actions=[executed],
        results=[result],
        executions=[
            RestoredAction(
                recorded=recorded,
                executed=executed,
                method="identity",
                recorded_success=True,
                live_success=True,
                result=result,
            )
        ],
    )


async def test_restored_execution_is_written_on_the_restored_observations_frame() -> None:
    run = _Run(coordinate_space="normalized_1000")
    messages = await run.restore(_observation(), _restored())
    restored = messages[-1].text
    assert restored.startswith("Restored execution history: ")
    assert "viewport pixels" not in _text(messages)
    facts = json.loads(restored.split("\n", 1)[1])
    assert facts["source_material"]["recorded_actions"] == [
        {"name": "browser.click_coordinate", "params": {"coordinate_x": 501, "coordinate_y": 857}}
    ]
    (executed,) = facts["actual_restored_execution"]
    assert executed["action"] == {
        "name": "browser.click_coordinate",
        "params": {"coordinate_x": 500, "coordinate_y": 858},
    }
    assert executed["result"]["effect"].startswith("Clicked at (500, 858) on ")
    # The next step feeds the restored step back on the frame its record keeps.
    record = StepRecord(
        step=0,
        origin="restored",
        frame=run.frame(),
        actions=list(_restored().actions),
        action_results=list(_restored().results),
    )
    step = await run.build(_observation(history=[record]), step=1)
    assert _actions(step) == [
        ("browser.click_coordinate", {"coordinate_x": 500, "coordinate_y": 858})
    ]


async def test_a_restored_observation_without_a_measurable_screenshot_does_not_end_the_branch() -> (
    None
):
    run = _Run(coordinate_space="normalized_1000")
    messages = await run.restore(_observation(screenshot=_screenshot(viewport=(0, 0))), _restored())
    facts = json.loads(messages[-1].text.split("\n", 1)[1])
    unplaced = {"coordinate_x": NOT_PLACEABLE, "coordinate_y": NOT_PLACEABLE}
    assert facts["source_material"]["recorded_actions"] == [
        {"name": "browser.click_coordinate", "params": unplaced}
    ]
    (executed,) = facts["actual_restored_execution"]
    assert executed["action"] == {"name": "browser.click_coordinate", "params": unplaced}
    assert executed["result"]["effect"].startswith(f"Clicked at a point {NOT_PLACEABLE} on ")


# ── A run with no coordinate action keeps viewport units ──────────────


async def test_a_dom_run_writes_no_point_and_keeps_viewport_pixels() -> None:
    """``dom`` negotiates no coordinate action: no grid is taught, the facts read as measured."""
    run = _Run(observation_mode="dom", coordinate_space="normalized_1000")
    assert run.space.coordinate_space is None
    assert "<coordinates>" not in _text(run.seed) and GUIDE not in _text(run.seed)
    clicked = StepRecord(
        step=0,
        frame=run.frame(),
        actions=[Action(name="browser.click", params={"index": 3})],
        action_results=[_clicked(640, 618, effect_id="browser.click")],
    )
    assert clicked.frame is None
    step = await run.build(_observation(history=[clicked]), step=1)
    (effect,) = _effects(step)
    assert effect.startswith("1. browser.click: Clicked at (640, 618) on textbox")
    assert _pointer_line(step) == "Pointer: (640, 618) in viewport pixels."
    restored = await run.restore(_observation(), _restored(), step=2)
    facts = json.loads(restored[-1].text.split("\n", 1)[1])
    (executed,) = facts["actual_restored_execution"]
    assert executed["action"] == _click(640, 618).model_dump(mode="json")
    assert executed["result"]["effect"].startswith("Clicked at (640, 618) on ")

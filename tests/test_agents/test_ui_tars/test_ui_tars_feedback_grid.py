"""UI-TARS is shown every point on the grid its model writes points on.

``docs/architecture_design.md`` §4.1, "模型看到的每个点都写在它写点的那个网格上"
and "坐标系归框架": the facts stay in the capability's viewport units (CSS px
for a browser), and every point UI-TARS shows — an executed action, an effect
line, the pointer — is written on its own grid by the framework, from the
frame recorded on the step the point belongs to. With ``normalized_1000`` a
model that read pixel numbers back wrote them onto the 0-1000 grid and missed
every target low on the screen; these tests pin the writing that replaced
them, each step driven as a Loop drives it (``tests/_helpers/frames.py``):

- the previous batch is written as the calls the model would write, on the
  frame its record keeps (the step it was chosen on); its effect lines too;
- the pointer is written on this step's frame;
- ``screenshot_pixels`` writes image pixels: the old numbers at DPR 1, twice
  the viewport's at DPR 2;
- a point UI-TARS shows reads back to the viewport point it stands for,
  within the grid's quantum;
- restored history writes executed and recorded actions on the restored
  observation's frame;
- an earlier step's frame comes from its record, so rendering it needs no
  agent state: a fresh agent renders the same text;
- a point with no place on the grid — its step showed no screenshot of known
  size, or it lies outside the screenshot — is said to be so, never written in
  pixels and never clamped onto an edge;
- which params are points comes from the action's registered ``Params``, never
  from a param's name or value alone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.effects import ElementFacts, PointerEffect, ViewportPoint
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
from spectragent.core.llm.messages import BaseMessage, ContentPartTextParam
from spectragent.core.loops.coordinate_frames import (
    NOT_PLACEABLE,
    OUTSIDE_THE_SCREENSHOT,
    fact_in_model_frame,
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


def _clicked(x: int, y: int) -> ActionResult:
    effect = PointerEffect(
        effect_id="browser.click_coordinate",
        point=ViewportPoint(x=x, y=y),
        hit=AMOUNT,
        focus_after=AMOUNT,
    )
    return ActionResult(success=True, effect=effect)


def _batch(frame: PointFrame | None, step: int = 0, x: int = 640, y: int = 618) -> StepRecord:
    """Step *step*, framed by *frame*, clicked the viewport's (x, y), a text field, and landed there."""
    return StepRecord(
        step=step, frame=frame, actions=[_click(x, y)], action_results=[_clicked(x, y)]
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
    """An agent prepared on a framed browser space, each step built as a Loop builds it."""

    def __init__(self, agent_class: type[Any] = UITarsAgent, **config: Any) -> None:
        self.agent = make_agent(agent_class, memory=SimpleMemory(max_messages=20), **config)
        keys = {
            "browser.click",
            "browser.click_coordinate",
            "browser.type_coordinate",
            "browser.scroll",
            "browser.done",
        }
        descriptors = [
            action.descriptor(fragments=frozenset({"browser.dom", "browser.screenshot"}))
            for action in BrowserCapability().actions
            if action.action_id in keys
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

    async def restore(self, observation: Any, history: RestoredHistory) -> list[BaseMessage]:
        context = step_context(self.space, observation, step=0, phase="history")
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


def _feedback(messages: list[BaseMessage]) -> dict[str, Any]:
    body = _text(messages).split("<action_results>\n")[-1].split("\n</action_results>")[0]
    return json.loads(body)


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
    """Build step 0 on *first*, then step 1 on *second*, fed back step 0's click (x, y)."""
    await run.build(_observation(screenshot=first), step=0)
    return await run.build(
        _observation(
            history=[_batch(run.frame(first), x=x, y=y)], screenshot=second, pointer=pointer
        ),
        step=1,
    )


# ── normalized_1000: the case the teacher model got wrong ─────────────


async def test_a_click_is_fed_back_on_the_0_1000_grid() -> None:
    run = _Run(coordinate_space="normalized_1000")
    step = await _second_step(run)
    grid = point_grid("normalized_1000", _screenshot())
    assert grid is not None
    point = grid.to_grid(640, 618)
    assert point == (500, 858)
    written = f"({point[0]}, {point[1]})"
    assert _feedback(step) == {
        "actions_without_returned_results": [],
        "discarded_action_count": 0,
        "error": None,
        "error_phase": None,
        "returned_results": [
            {
                "action": f"click(point='{written}')",
                "result": {
                    "effect": (
                        f'Clicked at {written} on textbox "Amount"; '
                        'focus is now on textbox "Amount".'
                    ),
                    "error": None,
                    "extracted_content": None,
                    "is_done": False,
                    "success": True,
                },
            }
        ],
        "step": 1,
    }
    assert _pointer_line(step) == f"Pointer: {written}."
    everything = _text([*run.seed, *step])
    assert "viewport pixels" not in everything
    assert "CSS" not in everything
    assert "(640, 618)" not in everything


@pytest.mark.parametrize("template", ["computer_use", "grounding", "mobile_use"])
def test_the_system_prompt_puts_feedback_points_on_the_models_grid(template: str) -> None:
    system = _text(_Run(prompt_template=template, coordinate_space="normalized_1000").seed)
    assert "viewport pixels" not in system
    assert "Every point in the feedback is on the same grid as the points you" in system


# ── screenshot_pixels: image pixels, whatever the device scale factor ──


@pytest.mark.parametrize(
    ("image", "written"),
    [
        pytest.param(VIEWPORT, "(640, 618)", id="dpr1-the-numbers-as-before"),
        pytest.param((2560, 1440), "(1280, 1236)", id="dpr2-image-pixels"),
    ],
)
async def test_screenshot_pixels_feed_back_image_pixels(
    image: tuple[int, int], written: str
) -> None:
    run = _Run(coordinate_space="screenshot_pixels")
    step = await _second_step(run, first=_screenshot(image), second=_screenshot(image))
    (returned,) = _feedback(step)["returned_results"]
    assert returned["action"] == f"click(point='{written}')"
    assert returned["result"]["effect"].startswith(f"Clicked at {written} on ")
    assert _pointer_line(step) == f"Pointer: {written}."


# ── What UI-TARS shows is what it would execute ───────────────────────


@pytest.mark.parametrize("space", ["normalized_1000", "screenshot_pixels"])
@pytest.mark.parametrize("image", [VIEWPORT, (2560, 1440), (1000, 563)])
@pytest.mark.parametrize("point", [(0, 0), (640, 618), (1279, 719), (333, 7)])
async def test_a_fed_back_point_parses_back_to_where_it_ran(
    space: CoordinateSpace, image: tuple[int, int], point: tuple[int, int]
) -> None:
    run = _Run(coordinate_space=space)
    shot = _screenshot(image)
    step = await _second_step(run, first=shot, second=shot, x=point[0], y=point[1])
    (returned,) = _feedback(step)["returned_results"]
    context = step_context(run.space, _observation(screenshot=shot), step=1)
    (action,) = decided(run.agent, run.space, f"Action: {returned['action']}", context)
    grid = point_grid(space, shot)
    assert grid is not None
    assert abs(action.params["coordinate_x"] - point[0]) <= grid.quantum
    assert abs(action.params["coordinate_y"] - point[1]) <= grid.quantum
    # The effect line names the same grid point the call does.
    call_point = returned["action"].split("point='")[1].split("'")[0]
    assert returned["result"]["effect"].startswith(f"Clicked at {call_point} on ")


# ── Whose frame ───────────────────────────────────────────────────────


async def test_a_batch_is_written_on_the_frame_it_was_chosen_on_and_the_pointer_on_this_one() -> (
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
    (returned,) = _feedback(step)["returned_results"]
    assert returned["action"] == "click(point='(500, 858)')"  # step 0's 1280x720
    assert _pointer_line(step) == "Pointer: (500, 500)."  # step 1's 1000x500


async def test_a_past_step_is_written_from_its_record_with_no_agent_state() -> None:
    """An agent that never built step 0 renders it the same: the frame is on the record."""
    built = _Run(coordinate_space="normalized_1000")
    fresh = _Run(coordinate_space="normalized_1000")
    after_step_0 = await _second_step(built)
    record = _batch(built.frame())
    alone = await fresh.build(_observation(history=[record]), step=1)
    # The observation turn (a first build also declares the platform before it).
    assert _text(alone[-1:]) == _text(after_step_0[-1:])
    # Rendering it again, in any order, writes the same turn.
    again = await fresh.build(_observation(history=[record]), step=1)
    assert _text(again[-1:]) == _text(alone[-1:])


async def test_a_batch_whose_step_showed_no_screenshot_of_known_size_is_not_placed() -> None:
    """No grid to write on: the points say so, and the prompt still builds."""
    run = _Run(coordinate_space="normalized_1000")
    unsized = run.frame(_screenshot(viewport=(0, 0)))
    assert unsized == PointFrame(space="normalized_1000")
    step = await run.build(_observation(history=[_batch(unsized)]), step=1)
    (returned,) = _feedback(step)["returned_results"]
    assert returned["action"] == {
        "name": "browser.click_coordinate",
        "params": {"coordinate_x": NOT_PLACEABLE, "coordinate_y": NOT_PLACEABLE},
    }
    assert returned["result"]["effect"].startswith(f"Clicked at a point {NOT_PLACEABLE} on ")
    assert "640" not in _text(step) and "618" not in _text(step)


async def test_a_point_outside_the_screenshot_is_said_to_be_outside_never_clamped() -> None:
    """``to_grid`` would put (1500, 618) on the right edge; it is not there."""
    run = _Run(coordinate_space="normalized_1000")
    step = await run.build(
        _observation(history=[_batch(run.frame(), x=1500)], pointer=(-3, 618)), step=1
    )
    (returned,) = _feedback(step)["returned_results"]
    assert returned["action"] == {
        "name": "browser.click_coordinate",
        "params": {"coordinate_x": OUTSIDE_THE_SCREENSHOT, "coordinate_y": OUTSIDE_THE_SCREENSHOT},
    }
    assert returned["result"]["effect"].startswith(
        f"Clicked at a point {OUTSIDE_THE_SCREENSHOT} on "
    )
    assert _pointer_line(step) == f"Pointer: a point {OUTSIDE_THE_SCREENSHOT}."
    assert "1000" not in _text(step) and "1500" not in _text(step)


async def test_only_declared_point_params_move_onto_the_grid() -> None:
    """An ``_x`` / ``_y`` pair the registry does not declare a point is left as it is."""
    run = _Run(coordinate_space="normalized_1000")
    zoom = Action(name="browser.zoom", params={"scale_x": 640, "scale_y": 618})
    record = StepRecord(
        step=0, frame=run.frame(), actions=[zoom], action_results=[ActionResult(success=True)]
    )
    step = await run.build(_observation(history=[record]), step=1)
    assert _feedback(step)["returned_results"][0]["action"] == zoom.model_dump(mode="json")


async def test_a_relative_action_name_finds_its_points_through_the_runs_binding() -> None:
    run = _Run(coordinate_space="normalized_1000")
    typed = Action(
        name="type_coordinate",
        params={"coordinate_x": 640, "coordinate_y": 618, "text": "12", "clear": True},
    )
    fact = fact_in_model_frame(typed, run.frame(), run.space)
    assert run.agent.format_action_fact(fact) == {
        "name": "type_coordinate",
        "params": {"coordinate_x": 500, "coordinate_y": 858, "text": "12", "clear": True},
    }


async def test_a_batch_without_points_writes_the_same_on_any_frame() -> None:
    run = _Run(coordinate_space="normalized_1000")
    for frame in (run.frame(), PointFrame(space="normalized_1000")):
        scrolled = StepRecord(
            step=0,
            frame=frame,
            actions=[Action(name="browser.scroll", params={"direction": "down"})],
            action_results=[ActionResult(success=True)],
        )
        step = await run.build(_observation(history=[scrolled]), step=1)
        assert _feedback(step)["returned_results"][0]["action"] == "scroll(direction='down')"


async def test_the_pointer_on_a_step_without_a_grid_is_not_written_in_pixels() -> None:
    run = _Run(coordinate_space="normalized_1000")
    step = await run.build(_observation(screenshot=_screenshot(viewport=(0, 0))), step=0)
    assert _pointer_line(step) == f"Pointer: a point {NOT_PLACEABLE}."
    assert NOT_PLACEABLE == "not placeable (no screenshot of known size)"


async def test_an_action_no_verb_writes_keeps_its_json_with_its_points_on_the_grid() -> None:
    """``type``'s ``clear`` has no spelling in UI-TARS's syntax; the point still moves."""
    run = _Run(coordinate_space="normalized_1000")
    typed = Action(
        name="browser.type_coordinate",
        params={"coordinate_x": 640, "coordinate_y": 618, "text": "12", "clear": True},
    )
    record = StepRecord(
        step=0, frame=run.frame(), actions=[typed], action_results=[ActionResult(success=True)]
    )
    step = await run.build(_observation(history=[record]), step=1)
    assert _feedback(step)["returned_results"][0]["action"] == {
        "name": "browser.type_coordinate",
        "params": {"coordinate_x": 500, "coordinate_y": 858, "text": "12", "clear": True},
    }


# ── Restored history ──────────────────────────────────────────────────


def _restored() -> RestoredHistory:
    recorded, executed = _click(641, 617), _click(640, 618)
    result = _clicked(640, 618)
    return RestoredHistory(
        source_run_id="source-run",
        source_step=0,
        source_reply="Thought: the amount field.\nAction: click(point='(501, 857)')",
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
    assert facts["source_material"]["recorded_actions"] == ["click(point='(501, 857)')"]
    (executed,) = facts["actual_restored_execution"]
    assert executed["action"] == "click(point='(500, 858)')"
    assert executed["result"]["effect"].startswith("Clicked at (500, 858) on ")
    # The next step feeds the restored batch back on the frame its record keeps.
    record = StepRecord(
        step=0,
        origin="restored",
        frame=run.frame(),
        actions=list(_restored().actions),
        action_results=list(_restored().results),
    )
    step = await run.build(_observation(history=[record]), step=1)
    assert _feedback(step)["returned_results"][0]["action"] == "click(point='(500, 858)')"


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


# ── An agent whose model writes no point keeps viewport units ─────────
# (BrowserUse writes its feedback on its own grid too, or in viewport units when
# its run has no coordinate action: tests/test_agents/test_browser_use/test_browser_use_feedback_grid.py.)


async def test_skyvern_still_reads_viewport_pixels() -> None:
    run = _Run(SkyvernAgent)
    assert run.space.coordinate_space is None
    step = await run.build(_observation(history=[_batch(None)]), step=1)
    text = _text(step)
    assert "Clicked at (640, 618) on textbox" in text
    assert "Pointer: (640, 618) in viewport pixels." in text

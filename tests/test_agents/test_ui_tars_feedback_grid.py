"""UI-TARS feeds back every point on the grid its model writes points on.

``docs/architecture_design.md`` §4.1, "模型看到的每个点都写在它写点的那个网格上":
the facts stay in the capability's viewport units (CSS px for a browser), and
UI-TARS writes each point it shows — an executed action, an effect line, the
pointer — on its own grid. With ``normalized_1000`` a model that read pixel
numbers back wrote them onto the 0-1000 grid and missed every target low on
the screen; these tests pin the writing that replaced them:

- the previous batch is written as the calls the model would write, on the
  grid of the screenshot the batch was chosen on; its effect lines too;
- the pointer is written on this step's grid;
- ``screenshot_pixels`` writes image pixels: the old numbers at DPR 1, twice
  the viewport's at DPR 2;
- a point UI-TARS shows parses back to the viewport point it stands for,
  within the grid's quantum;
- restored history writes executed and recorded actions on the restored
  observation's grid;
- an earlier step's grid comes from the prompt this agent built for it;
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

from spectragent.agents._feedback_points import NOT_PLACEABLE, OUTSIDE_THE_SCREENSHOT
from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.effects import ElementFacts, PointerEffect, ViewportPoint
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
from spectragent.core.llm.messages import BaseMessage, ContentPartTextParam
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation.capture_state import FailedVisibleResources, PointerState
from spectragent.schemas.observation.fragments import HistoryObs, StepInfoObs, TaskObs
from spectragent.schemas.observation.geometry import CoordinateSpace, point_grid
from spectragent.schemas.prompt import ReactPromptContext, RestoredHistory
from spectragent.schemas.restoration import RestoredAction
from spectragent.schemas.step_record import StepRecord
from tests._helpers.assembly import make_agent

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


def _batch(step: int = 0, x: int = 640, y: int = 618) -> StepRecord:
    """Step *step* clicked the viewport's (x, y), a text field, and the click landed there."""
    return StepRecord(step=step, actions=[_click(x, y)], action_results=[_clicked(x, y)])


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


def _prepared(agent_class: type[Any] = UITarsAgent, **config: Any) -> tuple[Any, list[BaseMessage]]:
    agent = make_agent(agent_class, memory=SimpleMemory(max_messages=20), **config)
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
    seed = agent.prepare(
        task="Enter the amount",
        action_descriptors=descriptors,
        execution_limits=ExecutionLimits(max_actions_per_step=2),
    )
    return agent, seed


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
    agent: Any,
    *,
    first: Any = None,
    second: Any = None,
    pointer: tuple[int, int] = (640, 618),
) -> list[BaseMessage]:
    """Build step 0 on *first*, then step 1 on *second*, fed back step 0's click."""
    await agent.build_react_prompt(
        _observation(screenshot=first), context=ReactPromptContext(step=0, phase="policy")
    )
    return await agent.build_react_prompt(
        _observation(history=[_batch()], screenshot=second, pointer=pointer),
        context=ReactPromptContext(step=1, phase="policy"),
    )


# ── normalized_1000: the case the teacher model got wrong ─────────────


async def test_a_click_is_fed_back_on_the_0_1000_grid() -> None:
    agent, seed = _prepared(coordinate_space="normalized_1000")
    step = await _second_step(agent)
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
    everything = _text([*seed, *step])
    assert "viewport pixels" not in everything
    assert "CSS" not in everything
    assert "(640, 618)" not in everything


@pytest.mark.parametrize("template", ["computer_use", "grounding", "mobile_use"])
def test_the_system_prompt_puts_feedback_points_on_the_models_grid(template: str) -> None:
    _, seed = _prepared(prompt_template=template, coordinate_space="normalized_1000")
    system = _text(seed)
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
    agent, _ = _prepared(coordinate_space="screenshot_pixels")
    step = await _second_step(agent, first=_screenshot(image), second=_screenshot(image))
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
    agent, _ = _prepared(coordinate_space=space)
    await agent.build_react_prompt(
        _observation(screenshot=_screenshot(image)),
        context=ReactPromptContext(step=0, phase="policy"),
    )
    step = await agent.build_react_prompt(
        _observation(history=[_batch(x=point[0], y=point[1])], screenshot=_screenshot(image)),
        context=ReactPromptContext(step=1, phase="policy"),
    )
    (returned,) = _feedback(step)["returned_results"]
    (action,) = agent.parse_react_output(f"Action: {returned['action']}")
    grid = point_grid(space, _screenshot(image))
    assert grid is not None
    assert abs(action.params["coordinate_x"] - point[0]) <= grid.quantum
    assert abs(action.params["coordinate_y"] - point[1]) <= grid.quantum
    # The effect line names the same grid point the call does.
    call_point = returned["action"].split("point='")[1].split("'")[0]
    assert returned["result"]["effect"].startswith(f"Clicked at {call_point} on ")


# ── Whose grid ────────────────────────────────────────────────────────


async def test_a_batch_is_written_on_the_grid_it_was_chosen_on_and_the_pointer_on_this_one() -> (
    None
):
    """The viewport changed between the steps: each point uses its own screenshot's grid."""
    agent, _ = _prepared(coordinate_space="normalized_1000")
    step = await _second_step(
        agent,
        first=_screenshot(VIEWPORT, VIEWPORT),
        second=_screenshot((1000, 500), (1000, 500)),
        pointer=(500, 250),
    )
    (returned,) = _feedback(step)["returned_results"]
    assert returned["action"] == "click(point='(500, 858)')"  # step 0's 1280x720
    assert _pointer_line(step) == "Pointer: (500, 500)."  # step 1's 1000x500


async def test_a_batch_whose_step_this_agent_did_not_build_is_refused() -> None:
    agent, _ = _prepared(coordinate_space="normalized_1000")
    with pytest.raises(ValueError, match="did not build step 0's prompt since prepare"):
        await agent.build_react_prompt(
            _observation(history=[_batch()]), context=ReactPromptContext(step=1, phase="policy")
        )


async def test_a_batch_whose_step_showed_no_screenshot_of_known_size_is_not_placed() -> None:
    """No grid to write on: the points say so, and the prompt still builds."""
    agent, _ = _prepared(coordinate_space="normalized_1000")
    await agent.build_react_prompt(
        _observation(screenshot=_screenshot(viewport=(0, 0))),
        context=ReactPromptContext(step=0, phase="policy"),
    )
    step = await agent.build_react_prompt(
        _observation(history=[_batch()]), context=ReactPromptContext(step=1, phase="policy")
    )
    (returned,) = _feedback(step)["returned_results"]
    assert returned["action"] == {
        "name": "browser.click_coordinate",
        "params": {"coordinate_x": NOT_PLACEABLE, "coordinate_y": NOT_PLACEABLE},
    }
    assert returned["result"]["effect"].startswith(f"Clicked at a point {NOT_PLACEABLE} on ")
    assert "640" not in _text(step) and "618" not in _text(step)


async def test_a_point_outside_the_screenshot_is_said_to_be_outside_never_clamped() -> None:
    """``to_grid`` would put (1500, 618) on the right edge; it is not there."""
    agent, _ = _prepared(coordinate_space="normalized_1000")
    await agent.build_react_prompt(
        _observation(), context=ReactPromptContext(step=0, phase="policy")
    )
    step = await agent.build_react_prompt(
        _observation(history=[_batch(x=1500)], pointer=(-3, 618)),
        context=ReactPromptContext(step=1, phase="policy"),
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
    agent, _ = _prepared(coordinate_space="normalized_1000")
    zoom = Action(name="browser.zoom", params={"scale_x": 640, "scale_y": 618})
    await agent.build_react_prompt(
        _observation(), context=ReactPromptContext(step=0, phase="policy")
    )
    step = await agent.build_react_prompt(
        _observation(
            history=[
                StepRecord(step=0, actions=[zoom], action_results=[ActionResult(success=True)])
            ]
        ),
        context=ReactPromptContext(step=1, phase="policy"),
    )
    assert _feedback(step)["returned_results"][0]["action"] == zoom.model_dump(mode="json")


async def test_a_relative_action_name_finds_its_points_through_the_runs_binding() -> None:
    agent, _ = _prepared(coordinate_space="normalized_1000")
    typed = Action(
        name="type_coordinate",
        params={"coordinate_x": 640, "coordinate_y": 618, "text": "12", "clear": True},
    )
    await agent.build_react_prompt(
        _observation(), context=ReactPromptContext(step=0, phase="policy")
    )
    assert agent.format_action_fact(typed, step=0) == {
        "name": "type_coordinate",
        "params": {"coordinate_x": 500, "coordinate_y": 858, "text": "12", "clear": True},
    }


async def test_prepare_forgets_the_grids_of_the_previous_execution() -> None:
    agent, _ = _prepared(coordinate_space="normalized_1000")
    await agent.build_react_prompt(
        _observation(), context=ReactPromptContext(step=0, phase="policy")
    )
    agent.prepare(
        task="Enter the amount",
        action_descriptors=agent.get_action_space(),
        execution_limits=ExecutionLimits(max_actions_per_step=2),
    )
    with pytest.raises(ValueError, match="did not build step 0's prompt"):
        await agent.build_react_prompt(
            _observation(history=[_batch()]), context=ReactPromptContext(step=1, phase="policy")
        )


async def test_a_batch_without_points_needs_no_grid() -> None:
    agent, _ = _prepared(coordinate_space="normalized_1000")
    scrolled = StepRecord(
        step=0,
        actions=[Action(name="browser.scroll", params={"direction": "down"})],
        action_results=[ActionResult(success=True)],
    )
    step = await agent.build_react_prompt(
        _observation(history=[scrolled]), context=ReactPromptContext(step=1, phase="policy")
    )
    assert _feedback(step)["returned_results"][0]["action"] == "scroll(direction='down')"


async def test_the_pointer_on_a_step_without_a_grid_is_not_written_in_pixels() -> None:
    agent, _ = _prepared(coordinate_space="normalized_1000")
    step = await agent.build_react_prompt(
        _observation(screenshot=_screenshot(viewport=(0, 0))),
        context=ReactPromptContext(step=0, phase="policy"),
    )
    assert _pointer_line(step) == f"Pointer: a point {NOT_PLACEABLE}."
    assert NOT_PLACEABLE == "not placeable (no screenshot of known size)"


async def test_an_action_no_verb_writes_keeps_its_json_with_its_points_on_the_grid() -> None:
    """``type``'s ``clear`` has no spelling in UI-TARS's syntax; the point still moves."""
    agent, _ = _prepared(coordinate_space="normalized_1000")
    typed = Action(
        name="browser.type_coordinate",
        params={"coordinate_x": 640, "coordinate_y": 618, "text": "12", "clear": True},
    )
    await agent.build_react_prompt(
        _observation(), context=ReactPromptContext(step=0, phase="policy")
    )
    step = await agent.build_react_prompt(
        _observation(
            history=[
                StepRecord(step=0, actions=[typed], action_results=[ActionResult(success=True)])
            ]
        ),
        context=ReactPromptContext(step=1, phase="policy"),
    )
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


async def test_restored_execution_is_written_on_the_restored_observations_grid() -> None:
    agent, _ = _prepared(coordinate_space="normalized_1000")
    messages = await agent.build_restored_history(
        _observation(), history=_restored(), context=ReactPromptContext(step=0, phase="history")
    )
    restored = messages[-1].text
    assert restored.startswith("Restored execution history: ")
    assert "viewport pixels" not in _text(messages)
    facts = json.loads(restored.split("\n", 1)[1])
    assert facts["source_material"]["recorded_actions"] == ["click(point='(501, 857)')"]
    (executed,) = facts["actual_restored_execution"]
    assert executed["action"] == "click(point='(500, 858)')"
    assert executed["result"]["effect"].startswith("Clicked at (500, 858) on ")
    # The next step feeds the restored batch back on the same grid.
    record = StepRecord(
        step=0,
        origin="restored",
        actions=list(_restored().actions),
        action_results=list(_restored().results),
    )
    step = await agent.build_react_prompt(
        _observation(history=[record]), context=ReactPromptContext(step=1, phase="policy")
    )
    assert _feedback(step)["returned_results"][0]["action"] == "click(point='(500, 858)')"


async def test_a_restored_observation_without_a_measurable_screenshot_does_not_end_the_branch() -> (
    None
):
    agent, _ = _prepared(coordinate_space="normalized_1000")
    messages = await agent.build_restored_history(
        _observation(screenshot=_screenshot(viewport=(0, 0))),
        history=_restored(),
        context=ReactPromptContext(step=0, phase="history"),
    )
    facts = json.loads(messages[-1].text.split("\n", 1)[1])
    unplaced = {"coordinate_x": NOT_PLACEABLE, "coordinate_y": NOT_PLACEABLE}
    assert facts["source_material"]["recorded_actions"] == [
        {"name": "browser.click_coordinate", "params": unplaced}
    ]
    (executed,) = facts["actual_restored_execution"]
    assert executed["action"] == {"name": "browser.click_coordinate", "params": unplaced}
    assert executed["result"]["effect"].startswith(f"Clicked at a point {NOT_PLACEABLE} on ")


# ── The other agents keep viewport units ──────────────────────────────


@pytest.mark.parametrize("agent_class", [SkyvernAgent, BrowserUseAgent])
async def test_the_browser_agents_still_read_viewport_pixels(agent_class: type[Any]) -> None:
    agent, _ = _prepared(agent_class)
    step = await agent.build_react_prompt(
        _observation(history=[_batch()]), context=ReactPromptContext(step=1, phase="policy")
    )
    text = _text(step)
    assert "Clicked at (640, 618) on textbox" in text
    assert "Pointer: (640, 618) in viewport pixels." in text

"""The framework's coordinate frames: declared by an Agent, built per step, recorded on the step.

``docs/architecture_design.md`` §4.1, "坐标系归框架". An Agent declares the
grid its model writes points on (``coordinate_space``) and does nothing else
about coordinates. These tests pin the framework's half:

- the run's action space is framed on the declared grid when it holds a point
  action — its point params say the model's units, nothing else about the
  space changes — and is left alone otherwise;
- each step's frame is the declared grid over the screenshot that step shows
  the model, with no grid when the screenshot's size is unknown or the model
  is shown none, and an assembly error when the screenshot reports no
  geometry at all;
- ReAct builds that frame before the Agent's prompt, hands it over in the
  prompt context and records it on the step;
- a framed run whose negotiated screenshot cannot report its geometry, or
  that negotiates none, is refused by ``AgentRuntime.prepare``, before a trace
  is opened or anything is set up.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, ClassVar, Literal

import pytest

from spectragent.core.action_guard import build_guarded_act
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.composer import ConfigComposer
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import UserMessage
from spectragent.core.loops.coordinate_frames import (
    NOT_PLACEABLE,
    OUTSIDE_THE_SCREENSHOT,
    ScreenshotGeometryError,
    actions_in_capability_units,
    actions_in_model_frame,
    check_frame_geometry,
    declared_space,
    fact_in_model_frame,
    framed_action_space,
    point_writer,
    step_frame,
)
from spectragent.core.loops.errors import ParseError, UnrepresentableActionError
from spectragent.core.loops.react import ReactLoop
from spectragent.core.registries import collect_registrars
from spectragent.core.runtime import AgentRuntime
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.geometry import PointFrame, PointGrid
from spectragent.schemas.step_record import StepRecord
from tests._helpers.action_spaces import browser_descriptors, browser_space, space_of
from tests._helpers.assembly import AGENT_ID, make_env, make_run

VIEWPORT = (1280, 720)


def _screenshot(
    image: tuple[int, int] = VIEWPORT,
    viewport: tuple[int, int] = VIEWPORT,
    *,
    image_bytes: bytes | None = b"\x89PNG\r\n\x1a\n",
) -> ScreenshotObs:
    return ScreenshotObs(
        image_bytes=image_bytes,
        image_width=image[0],
        image_height=image[1],
        viewport_width=viewport[0],
        viewport_height=viewport[1],
    )


def _pointing(coordinate_space: Any = "normalized_1000") -> ActionSpace:
    return browser_space("click_coordinate", "drag", "scroll", coordinate_space=coordinate_space)


# ── What an Agent declares ────────────────────────────────────────────


class TestTheDeclaration:
    def test_an_agent_declares_its_grid_with_coordinate_space(self) -> None:
        assert declared_space(SimpleNamespace(coordinate_space="normalized_1000")) == (
            "normalized_1000"
        )
        assert declared_space(SimpleNamespace()) is None

    def test_a_coordinate_space_that_names_no_grid_is_refused(self) -> None:
        with pytest.raises(TypeError, match="coordinate_space='css'"):
            declared_space(SimpleNamespace(coordinate_space="css"))


# ── The run's action space ────────────────────────────────────────────


class TestTheFramedActionSpace:
    @pytest.mark.parametrize(
        ("space", "x_line"),
        [
            (
                "normalized_1000",
                "int -- x on a 0-1000 grid laid over the screenshot whatever its pixel "
                "size, from its left edge",
            ),
            (
                "screenshot_pixels",
                "int -- x in pixels of the screenshot you are shown, from its left edge",
            ),
        ],
    )
    def test_a_point_param_says_the_models_units_in_prose_and_schema(
        self, space: Any, x_line: str
    ) -> None:
        framed = _pointing(space)
        click = next(d for d in framed.descriptors if d.name == "browser.click_coordinate")
        assert framed.coordinate_space == space
        assert click.params["coordinate_x"] == x_line
        assert click.params["coordinate_y"].endswith("from its top edge")
        assert "CSS" not in click.params["coordinate_x"]
        assert click.params_schema is not None
        assert click.params_schema["properties"]["coordinate_x"]["description"] == x_line
        drag = next(d for d in framed.descriptors if d.name == "browser.drag")
        assert {drag.params[key] for key in ("start_x", "end_x")} == {x_line}

    def test_only_the_units_change(self) -> None:
        plain, framed = _pointing(None), _pointing()
        assert plain.coordinate_space is None
        assert framed.allowed_actions == plain.allowed_actions
        assert framed.action_bindings == plain.action_bindings
        for before, after in zip(plain.descriptors, framed.descriptors, strict=True):
            assert after.name == before.name
            assert after.point_params == before.point_params
            assert after.description == before.description
            points = {key for pair in before.point_params for key in pair}
            assert {k: v for k, v in after.params.items() if k not in points} == {
                k: v for k, v in before.params.items() if k not in points
            }
        scroll = next(d for d in framed.descriptors if d.name == "browser.scroll")
        assert scroll == next(d for d in plain.descriptors if d.name == "browser.scroll")

    def test_a_space_without_a_point_action_is_taught_no_grid(self) -> None:
        plain = browser_space("click", "scroll", "done")
        assert framed_action_space(plain, "normalized_1000") is plain
        assert plain.coordinate_space is None

    def test_an_agent_that_declares_no_grid_keeps_the_capabilitys_units(self) -> None:
        plain = _pointing(None)
        assert framed_action_space(plain, None) is plain

    def test_a_grid_on_a_space_without_a_point_action_cannot_be_stated(self) -> None:
        with pytest.raises(ValueError, match="taught no coordinate frame"):
            ActionSpace(coordinate_space="normalized_1000")


# ── Each step's frame ─────────────────────────────────────────────────


class TestTheStepFrame:
    def test_an_unframed_run_has_no_frame(self) -> None:
        assert step_frame(_pointing(None), SimpleNamespace(screenshot=_screenshot())) is None

    @pytest.mark.parametrize(
        ("space", "image", "grid"),
        [
            ("normalized_1000", VIEWPORT, (1000, 1000)),
            ("normalized_1000", (2560, 1440), (1000, 1000)),
            ("screenshot_pixels", VIEWPORT, VIEWPORT),
            ("screenshot_pixels", (2560, 1440), (2560, 1440)),
        ],
    )
    def test_the_declared_grid_over_the_screenshot_the_step_shows(
        self, space: Any, image: tuple[int, int], grid: tuple[int, int]
    ) -> None:
        frame = step_frame(_pointing(space), SimpleNamespace(screenshot=_screenshot(image)))
        assert frame == PointFrame(space=space, grid=PointGrid(space, *grid, *VIEWPORT))

    @pytest.mark.parametrize(
        "observation",
        [
            pytest.param(SimpleNamespace(), id="no-screenshot"),
            pytest.param(None, id="no-observation"),
            pytest.param(SimpleNamespace(screenshot=_screenshot(image_bytes=None)), id="no-image"),
            pytest.param(SimpleNamespace(screenshot=_screenshot(viewport=(0, 0))), id="no-size"),
            pytest.param(
                SimpleNamespace(
                    screenshot=FragmentFailure(
                        fragment_id="browser.screenshot", error="Timeout", detail=""
                    )
                ),
                id="failed",
            ),
        ],
    )
    def test_a_step_without_a_screenshot_of_known_size_has_a_frame_without_a_grid(
        self, observation: Any
    ) -> None:
        assert step_frame(_pointing(), observation) == PointFrame(space="normalized_1000")

    def test_pixels_need_the_image_size_and_0_1000_does_not(self) -> None:
        unsized = SimpleNamespace(screenshot=_screenshot(image=(0, 0)))
        assert step_frame(_pointing("screenshot_pixels"), unsized) == PointFrame(
            space="screenshot_pixels"
        )
        normalized = step_frame(_pointing(), unsized)
        assert normalized is not None and normalized.grid is not None

    def test_a_screenshot_without_geometry_stops_the_step(self) -> None:
        observation = SimpleNamespace(screenshot=SimpleNamespace(image_bytes=b"\x89PNG"))
        with pytest.raises(TypeError, match="reports no screenshot geometry") as excinfo:
            step_frame(_pointing(), observation)
        assert "ScreenshotGeometry" in str(excinfo.value)

    def test_a_frame_is_recorded_with_the_step_and_read_back(self) -> None:
        frame = PointFrame(
            space="screenshot_pixels", grid=PointGrid("screenshot_pixels", 2560, 1440, *VIEWPORT)
        )
        record = StepRecord(step=3, frame=frame)
        assert StepRecord.model_validate_json(record.model_dump_json()) == record
        assert StepRecord.model_validate_json(StepRecord(step=0).model_dump_json()).frame is None

    def test_a_frame_holds_only_a_grid_of_its_own_space(self) -> None:
        with pytest.raises(ValueError, match="cannot hold a screenshot_pixels grid"):
            PointFrame(
                space="normalized_1000",
                grid=PointGrid("screenshot_pixels", *VIEWPORT, *VIEWPORT),
            )


# ── In: a reply's points into the capability's units ──────────────────


def _frame(space: Any = "normalized_1000", image: tuple[int, int] = VIEWPORT) -> PointFrame:
    frame = step_frame(_pointing(space), SimpleNamespace(screenshot=_screenshot(image)))
    assert frame is not None
    return frame


def _click(x: Any, y: Any, name: str = "click_coordinate") -> Action:
    return Action(name=name, params={"coordinate_x": x, "coordinate_y": y})


class TestIn:
    @pytest.mark.parametrize(
        ("space", "image", "written", "executed"),
        [
            pytest.param("normalized_1000", VIEWPORT, (500, 858), (640, 618), id="0-1000"),
            pytest.param("normalized_1000", (2560, 1440), (500, 500), (640, 360), id="0-1000-dpr2"),
            pytest.param("screenshot_pixels", VIEWPORT, (61, 377), (61, 377), id="pixels-dpr1"),
            pytest.param(
                "screenshot_pixels", (2560, 1440), (101, 400), (50, 200), id="pixels-dpr2"
            ),
        ],
    )
    def test_a_point_executes_in_css_px(
        self,
        space: Any,
        image: tuple[int, int],
        written: tuple[int, int],
        executed: tuple[int, int],
    ) -> None:
        (action,) = actions_in_capability_units(
            [_click(float(written[0]), float(written[1]))], _frame(space, image), _pointing(space)
        )
        assert action.params == {"coordinate_x": executed[0], "coordinate_y": executed[1]}
        assert all(type(v) is int for v in action.params.values())

    def test_every_point_of_every_point_action_and_nothing_else(self) -> None:
        space = _pointing("screenshot_pixels")
        drag = Action(
            name="drag", params={"start_x": 100, "start_y": 200, "end_x": 2000, "end_y": 1000}
        )
        scroll = Action(name="scroll", params={"direction": "down"})
        zoom = Action(name="browser.zoom", params={"scale_x": 640, "scale_y": 618})
        moved = actions_in_capability_units(
            [drag, scroll, zoom], _frame("screenshot_pixels", (2560, 1440)), space
        )
        assert [a.params for a in moved] == [
            {"start_x": 50, "start_y": 100, "end_x": 1000, "end_y": 500},
            {"direction": "down"},
            {"scale_x": 640, "scale_y": 618},  # no declared point: a number, not moved
        ]
        assert drag.params["start_x"] == 100  # the parsed action is not edited in place

    def test_a_point_off_the_screenshot_is_clamped_onto_its_edge(self) -> None:
        frame, space = _frame("screenshot_pixels", (2560, 1440)), _pointing("screenshot_pixels")
        assert actions_in_capability_units([_click(2600, 300)], frame, space)[0].params == {
            "coordinate_x": 1279,
            "coordinate_y": 150,
        }
        assert actions_in_capability_units([_click(-1, 5)], frame, space)[0].params == {
            "coordinate_x": 0,
            "coordinate_y": 2,
        }

    def test_a_point_on_a_step_without_a_screenshot_of_known_size_is_a_parse_error(self) -> None:
        unsized = PointFrame(space="normalized_1000")
        with pytest.raises(ParseError) as excinfo:
            actions_in_capability_units(
                [Action(name="scroll", params={"direction": "down"}), _click(10, 20)],
                unsized,
                _pointing(),
            )
        assert excinfo.value.detail == (
            "action 2, click_coordinate: a point cannot be placed, because this step's "
            "screenshot has no known size"
        )
        assert actions_in_capability_units(
            [Action(name="scroll", params={"direction": "down"})], unsized, _pointing()
        ) == [Action(name="scroll", params={"direction": "down"})]

    def test_a_param_pair_that_is_not_two_numbers_is_left_for_the_params_check(self) -> None:
        (action,) = actions_in_capability_units([_click("left", 2)], _frame(), _pointing())
        assert action.params == {"coordinate_x": "left", "coordinate_y": 2}

    def test_an_unframed_run_moves_nothing(self) -> None:
        assert actions_in_capability_units([_click(61, 377)], None, _pointing(None)) == [
            _click(61, 377)
        ]


# ── Out: recorded points onto the model's frame ───────────────────────


class TestOut:
    def test_a_fact_is_written_on_the_frame_it_was_chosen_on(self) -> None:
        fact = fact_in_model_frame(
            _click(640, 618, "browser.click_coordinate"), _frame(), _pointing()
        )
        assert fact.params == {"coordinate_x": 500, "coordinate_y": 858}
        assert fact.name == "browser.click_coordinate"

    @pytest.mark.parametrize(
        ("image", "written"),
        [
            pytest.param(VIEWPORT, (640, 618), id="dpr1"),
            pytest.param((2560, 1440), (1280, 1236), id="dpr2"),
        ],
    )
    def test_screenshot_pixels_write_image_pixels(
        self, image: tuple[int, int], written: tuple[int, int]
    ) -> None:
        frame, space = _frame("screenshot_pixels", image), _pointing("screenshot_pixels")
        assert fact_in_model_frame(_click(640, 618), frame, space).params == {
            "coordinate_x": written[0],
            "coordinate_y": written[1],
        }
        assert point_writer(frame)(640, 618) == f"({written[0]}, {written[1]})"

    def test_a_point_with_no_place_is_said_to_be_so_never_clamped_nor_in_pixels(self) -> None:
        outside = fact_in_model_frame(_click(1500, 618), _frame(), _pointing())
        assert outside.params == {
            "coordinate_x": OUTSIDE_THE_SCREENSHOT,
            "coordinate_y": OUTSIDE_THE_SCREENSHOT,
        }
        unsized = fact_in_model_frame(
            _click(640, 618), PointFrame(space="normalized_1000"), _pointing()
        )
        assert unsized.params == {"coordinate_x": NOT_PLACEABLE, "coordinate_y": NOT_PLACEABLE}
        assert point_writer(_frame())(1500, 618) == f"a point {OUTSIDE_THE_SCREENSHOT}"
        assert point_writer(PointFrame(space="normalized_1000"))(1, 2) == f"a point {NOT_PLACEABLE}"
        assert NOT_PLACEABLE == "not placeable (no screenshot of known size)"

    def test_a_recorded_action_this_run_does_not_execute_finds_its_points_in_the_registry(
        self,
    ) -> None:
        space = browser_space("click_coordinate", coordinate_space="normalized_1000")
        typed = Action(
            name="browser.type_coordinate",
            params={"coordinate_x": 640, "coordinate_y": 618, "text": "12", "clear": True},
        )
        assert fact_in_model_frame(typed, _frame(), space).params == {
            "coordinate_x": 500,
            "coordinate_y": 858,
            "text": "12",
            "clear": True,
        }

    def test_an_unframed_run_writes_the_capabilitys_units(self) -> None:
        click = _click(640, 618)
        assert fact_in_model_frame(click, None, _pointing(None)) is click
        assert point_writer(None)(640, 618) == "(640, 618)"
        assert point_writer(None, viewport=lambda x, y: f"{x}/{y}")(640, 618) == "640/618"

    def test_a_reply_written_for_a_student_refuses_a_point_with_no_place(self) -> None:
        (written,) = actions_in_model_frame([_click(640, 618)], _frame(), _pointing())
        assert written.params == {"coordinate_x": 500, "coordinate_y": 858}
        with pytest.raises(UnrepresentableActionError, match="outside the 1280x720 viewport"):
            actions_in_model_frame([_click(1500, 618)], _frame(), _pointing())
        with pytest.raises(
            UnrepresentableActionError, match="no screenshot of known size"
        ) as excinfo:
            actions_in_model_frame(
                [_click(640, 618)], PointFrame(space="normalized_1000"), _pointing()
            )
        assert excinfo.value.params == ("coordinate_x", "coordinate_y")

    @pytest.mark.parametrize("space", ["normalized_1000", "screenshot_pixels"])
    @pytest.mark.parametrize("image", [VIEWPORT, (2560, 1440), (1000, 563)])
    @pytest.mark.parametrize("point", [(0, 0), (640, 618), (1279, 719), (333, 7)])
    def test_a_point_shown_reads_back_within_the_grids_quantum(
        self, space: Any, image: tuple[int, int], point: tuple[int, int]
    ) -> None:
        frame, actions = _frame(space, image), _pointing(space)
        shown = fact_in_model_frame(_click(*point), frame, actions)
        (back,) = actions_in_capability_units([shown], frame, actions)
        assert frame.grid is not None
        for key, value in zip(("coordinate_x", "coordinate_y"), point, strict=True):
            assert abs(back.params[key] - value) <= frame.grid.quantum


# ── ReAct builds, hands over and records it ───────────────────────────


class _Completion:
    def __init__(self, text: str) -> None:
        self.completion = text
        self.usage = None


class _LLM:
    inference_kind = "chat"
    model = "test-model"

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls = 0

    async def ainvoke(self, messages: Any, **kw: Any) -> Any:
        text = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _Completion(text)


class _Memory:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, m: Any) -> None:
        self.messages.append(m)

    def add_messages(self, ms: Any) -> None:
        self.messages.extend(ms)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


class _Agent:
    """Reads ``[{"name": ..., "params": {...}}]``; keeps every prompt context it is handed."""

    __execution_limit_fields__ = frozenset()

    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def build_react_prompt(self, obs: Any, *, context: Any) -> list:
        self.contexts.append(context)
        return [UserMessage(content="the page")]

    def parse_react_output(self, out: Any) -> list[Action]:
        return [Action(name=item["name"], params=item["params"]) for item in json.loads(out)]

    def get_react_output_model(self) -> None:
        return None


class _Env:
    def __init__(self, screenshots: list[ScreenshotObs]) -> None:
        self._screenshots = screenshots

    async def observe(self, negotiated: Any) -> Any:
        return SimpleNamespace(screenshot=self._screenshots.pop(0))


class _Negotiated:
    loop_sourced = frozenset()


async def _react(
    space: ActionSpace, screenshots: list[ScreenshotObs], replies: list[str]
) -> tuple[_Agent, StepRecorder, list[Action]]:
    ran: list[Action] = []

    async def execute(action: Action, **_kw: Any) -> ActionResult:
        ran.append(action)
        return ActionResult(success=True, is_done=action.name == "browser.done")

    _, act_fn = build_guarded_act(execute, list(space.descriptors), bindings=space.action_bindings)
    agent, recorder = _Agent(), StepRecorder()
    await ReactLoop(max_steps=len(replies))._execute(
        agent,
        "t",
        ctx={
            "env": _Env(screenshots),
            "llm": _LLM(replies),
            "memory": _Memory(),
            "act_fn": act_fn,
            "action_space": space,
            "negotiated_obs": _Negotiated(),
            "step_recorder": recorder,
            "task_id": "t-1",
        },
    )
    return agent, recorder, ran


async def test_react_hands_the_agent_each_steps_frame_and_records_it() -> None:
    space = space_of(
        browser_descriptors("click_coordinate", "wait", "done"), coordinate_space="normalized_1000"
    )
    wait = json.dumps([{"name": "wait", "params": {}}])
    agent, recorder, _ran = await _react(
        space, [_screenshot(), _screenshot((1000, 500), (1000, 500))], [wait, wait]
    )
    frames = [
        PointFrame(
            space="normalized_1000", grid=PointGrid("normalized_1000", 1000, 1000, *VIEWPORT)
        ),
        PointFrame(
            space="normalized_1000", grid=PointGrid("normalized_1000", 1000, 1000, 1000, 500)
        ),
    ]
    assert [context.frame for context in agent.contexts] == frames
    assert [record.frame for record in recorder.records] == frames


async def test_an_unframed_run_records_no_frame() -> None:
    space = space_of(browser_descriptors("click", "wait", "done"))
    agent, recorder, _ran = await _react(
        space, [_screenshot()], [json.dumps([{"name": "wait", "params": {}}])]
    )
    assert [context.frame for context in agent.contexts] == [None]
    assert [record.frame for record in recorder.records] == [None]


# ── AgentRuntime frames the run it assembles ──────────────────────────

_BROWSER = {
    "capability_id": "browser",
    "profile": "standard",
    "mouse_trajectory": {"trajectory_type": "instant"},
    "typing_rhythm": {"rhythm_type": "instant"},
    "replayability": {"replayability_id": "none"},
    "network_source": {"source_id": "live"},
    "dom_cleaner": {"cleaner_type": "browser_use"},
    "dom_serializer": {"serializer_type": "text"},
    "backend": {"provider_id": "local_chrome", "headless": True},
}


def _assembled(agent: dict[str, Any]) -> Any:
    root = {
        "agent": [
            {
                "id": "a",
                "agent_loop": {"loop_id": "react"},
                "llm": {"model_type": "openai", "model": "gpt-4o", "provider": {"name": "direct"}},
                "memory": {"memory_id": "simple", "max_messages": 20},
                **agent,
            }
        ],
        "environment": [{"env_type": "browser", "id": "env", "capabilities": [_BROWSER]}],
    }
    runtime = AgentRuntime.for_agent(
        ConfigComposer(registrars=collect_registrars()).compose(root), "a"
    )
    runtime.prepare()
    return runtime


@pytest.mark.parametrize(
    ("agent", "framed"),
    [
        pytest.param(
            {"agent_id": "ui_tars", "coordinate_space": "normalized_1000"},
            "normalized_1000",
            id="ui_tars",
        ),
        pytest.param(
            {"agent_id": "browser_use.dom_vision", "coordinate_space": "normalized_1000"},
            "normalized_1000",
            id="browser_use-with-a-point-action",
        ),
        pytest.param(
            {"agent_id": "browser_use.dom"},
            None,
            id="browser_use-without-one",
        ),
        pytest.param({"agent_id": "skyvern"}, None, id="skyvern-declares-none"),
    ],
)
def test_the_runtime_frames_the_space_on_the_agents_declared_grid(
    agent: dict[str, Any], framed: Any
) -> None:
    space = _assembled(agent).action_space
    assert space.coordinate_space == framed
    pointing = [d for d in space.descriptors if d.point_params]
    for descriptor in pointing:
        (x_key, _y_key), *_ = descriptor.point_params
        assert ("0-1000 grid" in descriptor.params[x_key]) is (framed is not None)


async def test_react_executes_a_replys_points_in_css_px_and_records_them_so() -> None:
    """The Agent parses the numbers it wrote; the Loop moves them off the frame before running."""
    space = space_of(
        browser_descriptors("click_coordinate", "done"), coordinate_space="screenshot_pixels"
    )
    click = json.dumps(
        [{"name": "click_coordinate", "params": {"coordinate_x": 101, "coordinate_y": 400}}]
    )
    done = json.dumps([{"name": "done", "params": {"extracted_content": "ok"}}])
    _agent, recorder, ran = await _react(
        space, [_screenshot((2560, 1440)), _screenshot((2560, 1440))], [click, done]
    )
    assert ran[0].params == {"coordinate_x": 50, "coordinate_y": 200}  # DPR 2: halved
    first = recorder.records[0]
    assert first.actions[0].params == {"coordinate_x": 50, "coordinate_y": 200}
    assert first.llm_output == click  # the reply keeps the model's grid
    assert first.frame is not None and first.frame.grid is not None
    assert (first.frame.grid.width, first.frame.grid.viewport_width) == (2560, 1280)


async def test_react_sends_a_point_it_cannot_place_back_to_the_model() -> None:
    """No screenshot of known size: a parse error, recorded as one; nothing runs."""
    space = space_of(
        browser_descriptors("click_coordinate", "done"), coordinate_space="normalized_1000"
    )
    click = json.dumps(
        [{"name": "click_coordinate", "params": {"coordinate_x": 1, "coordinate_y": 2}}]
    )
    _agent, recorder, ran = await _react(space, [_screenshot(viewport=(0, 0))], [click])
    assert ran == []
    (record,) = recorder.records
    assert record.error_phase == "parse"
    assert record.error == (
        "action 1, click_coordinate: a point cannot be placed, because this step's "
        "screenshot has no known size"
    )
    assert record.frame == PointFrame(space="normalized_1000")


# ── AgentRuntime refuses a frame no screenshot can carry ──────────────

_SHOT = "frame_check.screenshot"
_PAGE = "frame_check.page"
_CLICK = "frame_check.click_coordinate"


def _register_bare_screenshot() -> None:
    """``frame_check.screenshot``, an image without the sizes a point needs, and a text page.

    Registered per test, so the registry rolls them back.
    """

    class _BareShot(FragmentBase):
        __registry_key__ = _SHOT
        __source__: ClassVar[Literal["env", "loop"]] = "env"

        image_bytes: bytes | None = None
        viewport_width: int = 0
        viewport_height: int = 0

    class _Page(FragmentBase):
        __registry_key__ = _PAGE
        __source__: ClassVar[Literal["env", "loop"]] = "env"

        text: str = ""


class TestTheRunsScreenshotCanCarryItsFrame:
    def test_a_screenshot_that_reports_its_geometry_passes(self) -> None:
        check_frame_geometry(_pointing(), {"screenshot": "browser.screenshot"}, agent="ui_tars")

    def test_an_unframed_run_is_not_asked(self) -> None:
        check_frame_geometry(_pointing(None), {}, agent="ui_tars")

    def test_a_screenshot_without_geometry_is_refused_by_name(self) -> None:
        _register_bare_screenshot()
        with pytest.raises(ScreenshotGeometryError) as excinfo:
            check_frame_geometry(_pointing(), {"screenshot": _SHOT}, agent="ui_tars")
        message = str(excinfo.value)
        assert message.startswith(
            "ui_tars writes points on the normalized_1000 grid and this run's "
            "browser.click_coordinate, browser.drag take points, but the screenshot it "
            "negotiates, frame_check.screenshot (_BareShot), reports no screenshot geometry"
        )
        assert "ScreenshotGeometry" in message
        assert isinstance(excinfo.value, TypeError)

    def test_a_run_with_no_screenshot_is_refused(self) -> None:
        with pytest.raises(ScreenshotGeometryError, match="negotiates no screenshot"):
            check_frame_geometry(_pointing(), {"dom": "browser.dom"}, agent="ui_tars")


class _ClickCap(CapabilityBase):
    """A point action and a screenshot that carries no geometry."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.

    __registry_key__ = "frame_check"
    __wiring__ = {
        "observation": ("observation", [_SHOT, _PAGE]),
        "action": ("action", [_CLICK]),
    }

    setups = 0

    async def setup(self, *, negotiation: Any = None) -> None:
        self.setups += 1

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return [
            ActionDescriptor(
                name=_CLICK,
                description="Click a point.",
                params={"coordinate_x": "int", "coordinate_y": "int"},
                point_params=(("coordinate_x", "coordinate_y"),),
            )
        ]

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        return ActionResult(success=True)


class _FrameCheckEnv(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "frame_check_env"


class _PointingAgent(_Agent):
    """Writes points on 0-1000 over the screenshot it asks for."""

    coordinate_space = "normalized_1000"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["screenshot"]),
        "action": ("action", ["click_coordinate"]),
    }


class _BlindPointingAgent(_PointingAgent):
    """Writes points on 0-1000 and asks for a text page, no screenshot."""

    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["page"]),
        "action": ("action", ["click_coordinate"]),
    }


class _RunMemory(_Memory):
    supports_protocols = frozenset({"user_assistant"})


def _framed_runtime(
    *, wants_screenshot: bool = True, hooks: list[Any] | None = None
) -> tuple[AgentRuntime, _ClickCap]:
    _register_bare_screenshot()
    cap = _ClickCap()
    env = make_env(_FrameCheckEnv, {"frame_check": cap})
    run = make_run(
        agent=_PointingAgent() if wants_screenshot else _BlindPointingAgent(),
        loop=ReactLoop(),
        env=env,
        llm=_LLM(["[]"]),
        memory=_RunMemory(),
        hooks=hooks,
    )
    return AgentRuntime.for_agent(run, AGENT_ID), cap


class TestPrepareRefusesAFrameNoScreenshotCanCarry:
    def test_a_screenshot_without_geometry_is_refused_in_prepare(self) -> None:
        runtime, cap = _framed_runtime()
        with pytest.raises(ScreenshotGeometryError, match=r"frame_check\.screenshot \(_BareShot\)"):
            runtime.prepare()
        assert cap.setups == 0

    def test_a_run_that_negotiates_no_screenshot_is_refused_in_prepare(self) -> None:
        runtime, _cap = _framed_runtime(wants_screenshot=False)
        with pytest.raises(ScreenshotGeometryError, match="negotiates no screenshot"):
            runtime.prepare()

    async def test_refused_before_a_trace_is_opened_or_anything_is_set_up(self, tmp_path) -> None:  # noqa: ANN001
        traces = tmp_path / "traces"
        runtime, cap = _framed_runtime(hooks=[TraceWriter(trace_dir=str(traces))])
        with pytest.raises(ScreenshotGeometryError):
            await runtime.execute("t", task_id="t-1")
        assert cap.setups == 0
        assert not traces.exists()

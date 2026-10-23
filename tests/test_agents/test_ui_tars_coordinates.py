"""UI-TARS points: taught on the model's grid, executed in the capability's units.

Every ``browser.*`` coordinate action executes CSS pixels of the viewport
(``Input.dispatchMouseEvent`` takes CSS px). The model answers on another
grid: the pixels of the screenshot it was shown — twice the viewport in
each direction at device scale factor 2 — or a 0-1000 grid over that
screenshot, which some models use whatever size they are told. The agent
used to forward whatever the model wrote as CSS pixels, which is right
only for pixels at DPR 1; the harness that ran it had to pin a 1000x1000
viewport at DPR 1 so the three grids happened to coincide.

The conversion is the framework's, never this agent's
(``spectragent/core/loops/coordinate_frames.py``, tested in
``tests/test_core/test_loops/test_coordinate_frames.py``): UI-TARS declares its
grid and parses the numbers a reply writes. What is pinned here is UI-TARS
under that contract, each step driven as a Loop drives it
(``tests/_helpers/frames.py``: the step's frame in the prompt context, the
parsed reply moved off it):

- the prompt says which grid, and a pixel step says the screenshot's size;
- the ``coordinate_space`` field offers exactly the shared grids, and the
  prompt has words for each;
- every point-carrying verb (click, type, drag) executes in CSS px, with the
  geometry of the screenshot the same step showed;
- a size the conversion needs but the fragment reports as unknown is a
  ``ParseError`` — never a DPR-1 guess; a point off the grid is clamped,
  like every screenshot point in the framework; a screenshot fragment that
  reports no geometry at all stops the run;
- the parser itself returns the numbers as written, on the model's grid;
- the seam AGENTS.md §2.5 asks for: real negotiation against
  ``BrowserCapability`` → rendered prompt → a multi-action reply →
  ``parse_react_output`` → the framework's conversion → ``guarded_act`` →
  the real action classes → the emitted events, with the params each action
  declares.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, get_args, get_type_hints

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import _COORDINATE_PROMPTS, UITarsAgent
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.capabilities.browser.actions.wait import WaitAction
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.capabilities.browser.watchdogs.events import (
    ClickCoordinateEvent,
    DragEvent,
    ScrollEvent,
    SendKeysEvent,
    TypeAtCoordinateEvent,
)
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.llm.messages import ContentPartTextParam
from spectragent.core.loops.coordinate_frames import step_frame
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.negotiation import extract_wiring
from spectragent.core.negotiation.action import _action_rules
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.geometry import CoordinateSpace
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.action_spaces import space_for, space_of, wired_space
from tests._helpers.assembly import make_agent, make_env
from tests._helpers.frames import decided, step_context
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
    FakeScope,
)


def _observation(
    *,
    image: tuple[int, int] = (1280, 720),
    viewport: tuple[int, int] = (1280, 720),
) -> Any:
    return SimpleNamespace(
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        task=SimpleNamespace(user_request="tick the checkbox"),
        history=SimpleNamespace(items=[]),
        screenshot=ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            viewport_width=viewport[0],
            viewport_height=viewport[1],
            image_width=image[0],
            image_height=image[1],
        ),
    )


class _Run:
    """UI-TARS prepared with every action its wiring names, driven step by step as a Loop drives it."""

    def __init__(self, **config: Any) -> None:
        self.agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10), **config)
        self.space: ActionSpace = wired_space(
            UITarsAgent, coordinate_space=self.agent.coordinate_space
        )
        self.agent.prepare(
            task="t",
            action_space=self.space,
            execution_limits=ExecutionLimits(max_actions_per_step=6),
        )
        self.context = ReactPromptContext(step=0, phase="policy")

    async def show(self, observation: Any) -> str:
        """Build this step's prompt on *observation*; its text."""
        self.context = step_context(self.space, observation)
        content = (await self.agent.build_react_prompt(observation, context=self.context))[
            -1
        ].content
        assert isinstance(content, list)
        return next(p.text for p in content if isinstance(p, ContentPartTextParam))

    def points(self, reply: str) -> list[tuple[str, dict[str, Any]]]:
        """*reply*, as the step executes it: parsed, then off the step's frame."""
        return [(a.name, a.params) for a in decided(self.agent, self.space, reply, self.context)]


def _agent(**config: Any) -> UITarsAgent:
    return _Run(**config).agent


# ── The prompt names the grid ─────────────────────────────────────────


class TestThePromptNamesTheGrid:
    def test_pixels_are_taught_as_pixels_of_the_screenshot(self) -> None:
        agent = _agent()
        prompt = agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )[0].content
        assert isinstance(prompt, str)
        assert "# Coordinates" in prompt
        assert "pixels of the screenshot you are shown" in prompt
        assert "0-1000" not in prompt

    async def test_a_pixel_step_states_the_size_of_its_screenshot(self) -> None:
        text = await _Run().show(_observation(image=(2560, 1440)))
        assert "Screenshot: 2560x1440 pixels." in text

    async def test_normalized_is_taught_as_a_0_1000_grid_and_no_pixel_size(self) -> None:
        agent = _agent(coordinate_space="normalized_1000")
        prompt = agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )[0].content
        assert isinstance(prompt, str)
        assert "0-1000 grid" in prompt
        assert "pixels of the screenshot you are shown" not in prompt
        # A pixel size next to a 0-1000 instruction invites mixing the two.
        run = _Run(coordinate_space="normalized_1000")
        assert "Screenshot:" not in (await run.show(_observation(image=(2560, 1440))))

    @pytest.mark.parametrize("space", ["screenshot_pixels", "normalized_1000"])
    def test_every_grid_also_says_a_point_is_numbers_and_nothing_else(self, space: str) -> None:
        """What the parser enforces, said once in the prompt on either grid.

        ``_point_coordinates`` refuses a point that names its target
        (``point='btn2'``) instead of locating it. A model that has not been
        told spends a step finding out.
        """
        agent = _agent(coordinate_space=space)
        prompt = agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )[0].content
        assert isinstance(prompt, str)
        assert "the two numbers alone" in prompt
        assert "Never put an element name, a selector or a description" in prompt

    def test_the_recorded_snapshot_says_which_grid_the_run_taught(self) -> None:
        """A trace reader has the prompt snapshot, not the config."""
        agent = _agent(coordinate_space="normalized_1000")
        agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        snapshot = agent.system_prompt
        assert snapshot is not None
        assert "0-1000 grid" in snapshot.params["coordinate_space"]  # pyright: ignore[reportAttributeAccessIssue]


class TestTheFieldIsTheSharedGrid:
    def test_the_field_offers_exactly_the_shared_grids_and_the_prompt_has_words_for_each(
        self,
    ) -> None:
        """The Literal is written inline so the generated stub can type it.

        An alias would render as ``Any`` in ``ui_tars.pyi``, and a type
        checker reading the stub would accept ``coordinate_space='bogus'``.
        Inline means three copies of two names; this keeps them one set.
        """
        annotated = get_type_hints(UITarsAgent, include_extras=True)["coordinate_space"]
        field_options = get_args(get_args(annotated)[0])
        assert field_options == get_args(CoordinateSpace)
        assert set(_COORDINATE_PROMPTS) == set(field_options)

    async def test_a_screenshot_fragment_without_geometry_stops_the_run(self) -> None:
        """An assembly error, not something the model can fix one step at a time.

        The framework refuses it while it builds the step's frame, before the
        prompt: the Loop records the step's ``error_phase="prompt"``.
        """
        run = _Run()
        observation = SimpleNamespace(
            platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
            history=SimpleNamespace(items=[]),
            task=SimpleNamespace(user_request="t"),
            screenshot=SimpleNamespace(image_bytes=b"\x89PNG\r\n\x1a\n"),
        )
        with pytest.raises(TypeError, match="reports no screenshot geometry") as excinfo:
            step_frame(run.space, observation)
        assert "ScreenshotGeometry" in str(excinfo.value)


# ── Conversion ────────────────────────────────────────────────────────


class TestScreenshotPixels:
    async def test_dpr_one_is_the_identity(self) -> None:
        run = _Run()
        await run.show(_observation(image=(1280, 720)))
        assert run.points("Action: click(point='(61, 377)')") == [
            ("click_coordinate", {"coordinate_x": 61, "coordinate_y": 377})
        ]

    async def test_dpr_two_halves_every_point_of_every_point_verb(self) -> None:
        run = _Run()
        await run.show(_observation(image=(2560, 1440)))
        reply = (
            "Action: click(point='(101, 400)')\n"
            "Action: type(content='hi', point='(640, 360)')\n"
            "Action: drag(start_point='(100, 200)', end_point='(2000, 1000)')"
        )
        assert run.points(reply) == [
            ("click_coordinate", {"coordinate_x": 50, "coordinate_y": 200}),
            ("type_coordinate", {"coordinate_x": 320, "coordinate_y": 180, "text": "hi"}),
            ("drag", {"start_x": 50, "start_y": 100, "end_x": 1000, "end_y": 500}),
        ]

    async def test_the_far_edge_lands_on_the_last_css_pixel(self) -> None:
        run = _Run()
        await run.show(_observation(image=(2560, 1440)))
        assert run.points("Action: click(point='(2560, 1440)')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 719})
        ]

    async def test_an_unknown_image_size_is_refused_not_guessed(self) -> None:
        run = _Run()
        # No size to state, so the step states none.
        assert "Screenshot:" not in (await run.show(_observation(image=(0, 0))))
        with pytest.raises(ParseError) as excinfo:
            run.points("Action: click(point='(10, 20)')")
        assert excinfo.value.detail == (
            "action 1, click_coordinate: a point cannot be placed, "
            "because this step's screenshot has no known size"
        )

    async def test_a_point_off_the_screenshot_is_clamped_onto_its_edge(self) -> None:
        """The framework's one rule for a point off the grid, which browser_use shares."""
        run = _Run()
        await run.show(_observation(image=(2560, 1440)))
        assert run.points("Action: click(point='(2600, 300)')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 150})
        ]
        assert run.points("Action: click(point='(-1, 5)')") == [
            ("click_coordinate", {"coordinate_x": 0, "coordinate_y": 2})
        ]

    async def test_a_ui_tars_1_5_corner_on_its_resized_input_is_not_a_parse_failure(self) -> None:
        """Qwen2.5-VL sees 1280x720 as 1288x728; its bottom-right answer is a real one."""
        run = _Run()
        await run.show(_observation(image=(1280, 720)))
        assert run.points("Action: click(point='<point>1287 727</point>')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 719})
        ]


class TestNormalized1000:
    async def test_the_grid_maps_onto_the_viewport_whatever_the_image_size(self) -> None:
        run = _Run(coordinate_space="normalized_1000")
        await run.show(_observation(image=(2560, 1440), viewport=(1280, 720)))
        assert run.points("Action: click(point='(500, 500)')") == [
            ("click_coordinate", {"coordinate_x": 640, "coordinate_y": 360})
        ]
        assert run.points("Action: click(point='(1000, 1000)')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 719})
        ]

    async def test_the_image_size_is_not_needed_but_the_viewport_is(self) -> None:
        run = _Run(coordinate_space="normalized_1000")
        await run.show(_observation(image=(0, 0), viewport=(1000, 800)))
        assert run.points("Action: click(point='<point>250 500</point>')") == [
            ("click_coordinate", {"coordinate_x": 250, "coordinate_y": 400})
        ]
        await run.show(_observation(image=(1000, 800), viewport=(0, 0)))
        with pytest.raises(ParseError, match="screenshot has no known size"):
            run.points("Action: click(point='(250, 500)')")

    async def test_an_answer_past_1000_is_clamped_like_any_point_off_the_grid(self) -> None:
        run = _Run(coordinate_space="normalized_1000")
        await run.show(_observation(image=(2560, 1440)))
        assert run.points("Action: click(point='(1500, 300)')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 216})
        ]


class TestTheFrameIsPerStep:
    async def test_each_reply_is_placed_with_the_screenshot_its_step_showed(self) -> None:
        run = _Run()
        await run.show(_observation(image=(2560, 1440)))
        assert run.points("Action: click(point='(200, 200)')")[0][1] == {
            "coordinate_x": 100,
            "coordinate_y": 100,
        }
        await run.show(_observation(image=(1280, 720)))
        assert run.points("Action: click(point='(200, 200)')")[0][1] == {
            "coordinate_x": 200,
            "coordinate_y": 200,
        }

    async def test_a_step_without_a_screenshot_places_no_point(self) -> None:
        run = _Run()
        await run.show(_observation(image=(1280, 720)))
        no_screenshot = SimpleNamespace(
            platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
            history=SimpleNamespace(items=[]),
            task=SimpleNamespace(user_request="t"),
        )
        run.context = step_context(run.space, no_screenshot)
        await run.agent.build_react_prompt(no_screenshot, context=run.context)
        with pytest.raises(ParseError, match="screenshot has no known size"):
            run.points("Action: click(point='(10, 20)')")

    def test_verbs_without_a_point_need_no_screenshot(self) -> None:
        run = _Run()
        reply = (
            "Action: scroll(direction='down')\n"
            "Action: hotkey(key='enter')\n"
            "Action: wait()\n"
            "Action: finished(content='ok')"
        )
        run.context = step_context(run.space, None)
        assert [name for name, _ in run.points(reply)] == [
            "scroll",
            "send_keys",
            "wait",
            "done",
        ]


class TestTheParserReadsTheModelsGrid:
    """UI-TARS converts nothing: what it parses is what the model wrote."""

    def test_a_point_comes_back_as_written(self) -> None:
        agent = _agent(coordinate_space="normalized_1000")
        (action,) = agent.parse_react_output("Action: click(point='(500, 858)')")
        assert action.params == {"coordinate_x": 500, "coordinate_y": 858}

    def test_a_box_comes_back_as_its_centre_on_the_same_grid(self) -> None:
        agent = _agent()
        (action,) = agent.parse_react_output("Action: click(point='(10, 20, 30, 41)')")
        assert action.params == {"coordinate_x": 20, "coordinate_y": 30.5}

    def test_it_needs_no_screenshot_and_keeps_no_grid(self) -> None:
        agent = _agent()
        assert not any("grid" in name for name in vars(agent))
        assert agent.parse_react_output("Action: click(point='(1, 2)')")[0].params == {
            "coordinate_x": 1,
            "coordinate_y": 2,
        }


# ── The negotiated seam, end to end ───────────────────────────────────


@pytest.fixture
def seam() -> dict[str, Any]:
    """Real negotiation, a guard over the real action classes, a recording scope."""
    capability = BrowserCapability()
    capability._actions_by_name["browser.wait"] = WaitAction(default_seconds=0.0)
    env = make_env(BrowserEnvironment, {"browser": capability})
    negotiated = _action_rules(
        agent=extract_wiring(UITarsAgent, "action"),
        env_provides=set(capability.provided_actions),
    )
    descriptors = env.get_action_space(active_actions=negotiated.active_actions)

    fake = FakeCapability()
    fake._scopes["sid_main"] = FakeScope()
    fake._selector_maps["sid_main"] = {}
    fake._default_session_id = "sid_main"

    async def _execute(action: Action, **_kw: Any) -> ActionResult:
        impl = capability._actions_by_name.get(action.name)
        assert impl is not None, f"{action.name} did not route to a capability action"
        return await impl.execute(action.params, capability=fake)  # type: ignore[arg-type]

    _kept, guard = build_guarded_act(_execute, descriptors, bindings=negotiated.action_bindings)
    return {"descriptors": descriptors, "guard": guard, "scope": fake._scopes["sid_main"]}


#: One Action line per point-carrying and point-free verb the run negotiates,
#: written in screenshot pixels of a DPR-2 capture of a 1280x720 viewport.
_EVERY_VERB = (
    "Thought: fill the field, drag the slider, then finish.\n"
    "Action: click(point='(101, 400)')\n"
    "Action: type(content='hello', point='(640, 360)')\n"
    "Action: drag(start_point='(100, 200)', end_point='(300, 200)')\n"
    "Action: scroll(direction='down')\n"
    "Action: hotkey(key='ctrl a')\n"
    "Action: wait()\n"
    "Action: finished(content='done')"
)


async def test_every_emitted_action_meets_its_descriptor_and_executes_in_css_px(
    seam: dict[str, Any],
) -> None:
    """Negotiation matches names, not parameter contracts — so this checks the contract.

    For each action the agent emits: the params the descriptor requires are
    present, nothing it does not declare is sent, point params are ints once
    the framework has moved them off the step's frame, and the real action
    runs through the guard. The events it emits carry CSS pixels — half of
    what the model wrote on the DPR-2 screenshot.
    """
    agent = _agent()
    space = space_for(agent, seam["descriptors"])
    prompt = agent.prepare(
        task="t",
        action_space=space,
        execution_limits=ExecutionLimits(max_actions_per_step=9),
    )[0].content
    assert isinstance(prompt, str)
    assert "maximum of 9 actions" in prompt
    observation = _observation(image=(2560, 1440), viewport=(1280, 720))
    context = step_context(space, observation)
    await agent.build_react_prompt(observation, context=context)

    actions = decided(agent, space, _EVERY_VERB, context)
    by_local_id = {d.name.rsplit(".", 1)[-1]: d for d in seam["descriptors"]}
    assert [a.name for a in actions] == [
        "click_coordinate",
        "type_coordinate",
        "drag",
        "scroll",
        "send_keys",
        "wait",
        "done",
    ]
    for action in actions:
        declared = by_local_id[action.name].params
        required = {k for k, spec in declared.items() if "optional" not in spec.lower()}
        assert required <= set(action.params) <= set(declared), action
        for key in ("coordinate_x", "coordinate_y", "start_x", "start_y", "end_x", "end_y"):
            if key in action.params:
                assert type(action.params[key]) is int, (action.name, key)

        result = await seam["guard"](action)
        assert result.success, f"{action.name}: {result.error}"

    emitted = seam["scope"].emitted
    click = next(e for e in emitted if isinstance(e, ClickCoordinateEvent))
    assert (click.coordinate_x, click.coordinate_y) == (50, 200)
    typed = next(e for e in emitted if isinstance(e, TypeAtCoordinateEvent))
    assert (typed.coordinate_x, typed.coordinate_y, typed.text) == (320, 180, "hello")
    drag = next(e for e in emitted if isinstance(e, DragEvent))
    assert (drag.start_x, drag.start_y, drag.end_x, drag.end_y) == (50, 100, 150, 100)
    assert next(e for e in emitted if isinstance(e, ScrollEvent)).direction == "down"
    assert next(e for e in emitted if isinstance(e, SendKeysEvent)).keys == "ctrl+a"

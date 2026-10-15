"""UI-TARS points: taught on the model's grid, executed in the capability's units.

Every ``browser.*`` coordinate action executes CSS pixels of the viewport
(``Input.dispatchMouseEvent`` takes CSS px). The model answers on another
grid: the pixels of the screenshot it was shown — twice the viewport in
each direction at device scale factor 2 — or a 0-1000 grid over that
screenshot, which some models use whatever size they are told. The agent
used to forward whatever the model wrote as CSS pixels, which is right
only for pixels at DPR 1; the harness that ran it had to pin a 1000x1000
viewport at DPR 1 so the three grids happened to coincide.

The mapping itself is the framework's
(``spectragent.schemas.observation.geometry``, tested in
``tests/test_schemas/test_observation/test_geometry.py``). What is pinned
here is how this agent uses it:

- the prompt says which grid, and a pixel step says the screenshot's size;
- the ``coordinate_space`` field offers exactly the shared grids, and the
  prompt has words for each;
- conversion happens once, with the geometry of the screenshot the same
  step showed, for every point-carrying verb (click, type, drag);
- a size the conversion needs but the fragment reports as unknown is a
  ``ParseError`` — never a DPR-1 guess; a point off the grid is clamped,
  like every screenshot point in the framework; a screenshot fragment that
  reports no geometry at all stops the run;
- the seam AGENTS.md §2.5 asks for: real negotiation against
  ``BrowserCapability`` → rendered prompt → a multi-action reply →
  ``parse_react_output`` → ``guarded_act`` → the real action classes → the
  emitted events, with the params each action declares.
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
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.negotiation import extract_wiring
from spectragent.core.negotiation.action import _action_rules
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation.geometry import CoordinateSpace
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.assembly import make_agent, make_env
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
        platform=SimpleNamespace(operating_system="macos"),
        task=SimpleNamespace(user_request="tick the checkbox"),
        screenshot=ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            viewport_width=viewport[0],
            viewport_height=viewport[1],
            image_width=image[0],
            image_height=image[1],
        ),
    )


def _agent(**config: Any) -> UITarsAgent:
    return make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10), **config)


async def _step_text(agent: UITarsAgent, observation: Any) -> str:
    content = (
        await agent.build_react_prompt(
            observation, context=ReactPromptContext(step=0, phase="policy")
        )
    )[0].content
    assert isinstance(content, list)
    return next(p.text for p in content if isinstance(p, ContentPartTextParam))


def _points(agent: UITarsAgent, reply: str) -> list[tuple[str, dict[str, Any]]]:
    return [(a.name, a.params) for a in agent.parse_react_output(reply)]


# ── The prompt names the grid ─────────────────────────────────────────


class TestThePromptNamesTheGrid:
    def test_pixels_are_taught_as_pixels_of_the_screenshot(self) -> None:
        agent = _agent()
        prompt = agent.prepare(
            task="t",
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            action_descriptors=[],
        )[0].content
        assert isinstance(prompt, str)
        assert "# Coordinates" in prompt
        assert "pixels of the screenshot you are shown" in prompt
        assert "0-1000" not in prompt

    async def test_a_pixel_step_states_the_size_of_its_screenshot(self) -> None:
        agent = _agent()
        text = await _step_text(agent, _observation(image=(2560, 1440)))
        assert "Screenshot: 2560x1440 pixels." in text

    async def test_normalized_is_taught_as_a_0_1000_grid_and_no_pixel_size(self) -> None:
        agent = _agent(coordinate_space="normalized_1000")
        prompt = agent.prepare(
            task="t",
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            action_descriptors=[],
        )[0].content
        assert isinstance(prompt, str)
        assert "0-1000 grid" in prompt
        assert "pixels of the screenshot you are shown" not in prompt
        # A pixel size next to a 0-1000 instruction invites mixing the two.
        assert "Screenshot:" not in (await _step_text(agent, _observation(image=(2560, 1440))))

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
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            action_descriptors=[],
        )[0].content
        assert isinstance(prompt, str)
        assert "the two numbers alone" in prompt
        assert "Never put an element name, a selector or a description" in prompt

    def test_the_recorded_snapshot_says_which_grid_the_run_taught(self) -> None:
        """A trace reader has the prompt snapshot, not the config."""
        agent = _agent(coordinate_space="normalized_1000")
        agent.prepare(
            task="t",
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            action_descriptors=[],
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
        """An assembly error, not something the model can fix one step at a time."""
        agent = _agent()
        observation = SimpleNamespace(
            platform=SimpleNamespace(operating_system="macos"),
            task=SimpleNamespace(user_request="t"),
            screenshot=SimpleNamespace(image_bytes=b"\x89PNG\r\n\x1a\n"),
        )
        with pytest.raises(TypeError, match="reports no screenshot geometry") as excinfo:
            (
                await agent.build_react_prompt(
                    observation, context=ReactPromptContext(step=0, phase="policy")
                )
            )
        assert "ScreenshotGeometry" in str(excinfo.value)


# ── Conversion ────────────────────────────────────────────────────────


class TestScreenshotPixels:
    async def test_dpr_one_is_the_identity(self) -> None:
        agent = _agent()
        (
            await agent.build_react_prompt(
                _observation(image=(1280, 720)), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        assert _points(agent, "Action: click(point='(61, 377)')") == [
            ("click_coordinate", {"coordinate_x": 61, "coordinate_y": 377})
        ]

    async def test_dpr_two_halves_every_point_of_every_point_verb(self) -> None:
        agent = _agent()
        (
            await agent.build_react_prompt(
                _observation(image=(2560, 1440)), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        reply = (
            "Action: click(point='(101, 400)')\n"
            "Action: type(content='hi', point='(640, 360)')\n"
            "Action: drag(start_point='(100, 200)', end_point='(2000, 1000)')"
        )
        assert _points(agent, reply) == [
            ("click_coordinate", {"coordinate_x": 50, "coordinate_y": 200}),
            ("type_coordinate", {"coordinate_x": 320, "coordinate_y": 180, "text": "hi"}),
            ("drag", {"start_x": 50, "start_y": 100, "end_x": 1000, "end_y": 500}),
        ]

    async def test_the_far_edge_lands_on_the_last_css_pixel(self) -> None:
        agent = _agent()
        (
            await agent.build_react_prompt(
                _observation(image=(2560, 1440)), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        assert _points(agent, "Action: click(point='(2560, 1440)')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 719})
        ]

    async def test_an_unknown_image_size_is_refused_not_guessed(self) -> None:
        agent = _agent()
        # No size to state, so the step states none.
        assert "Screenshot:" not in (await _step_text(agent, _observation(image=(0, 0))))
        with pytest.raises(ParseError) as excinfo:
            agent.parse_react_output("Action: click(point='(10, 20)')")
        assert excinfo.value.detail == (
            "Action line 1, click(point='(10, 20)'): a point cannot be placed, "
            "because this step's screenshot has no known size"
        )

    async def test_a_point_off_the_screenshot_is_clamped_onto_its_edge(self) -> None:
        """The framework's one rule for a point off the grid, which browser_use shares."""
        agent = _agent()
        (
            await agent.build_react_prompt(
                _observation(image=(2560, 1440)), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        assert _points(agent, "Action: click(point='(2600, 300)')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 150})
        ]
        assert _points(agent, "Action: click(point='(-1, 5)')") == [
            ("click_coordinate", {"coordinate_x": 0, "coordinate_y": 2})
        ]

    async def test_a_ui_tars_1_5_corner_on_its_resized_input_is_not_a_parse_failure(self) -> None:
        """Qwen2.5-VL sees 1280x720 as 1288x728; its bottom-right answer is a real one."""
        agent = _agent()
        (
            await agent.build_react_prompt(
                _observation(image=(1280, 720)), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        assert _points(agent, "Action: click(point='<point>1287 727</point>')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 719})
        ]


class TestNormalized1000:
    async def test_the_grid_maps_onto_the_viewport_whatever_the_image_size(self) -> None:
        agent = _agent(coordinate_space="normalized_1000")
        (
            await agent.build_react_prompt(
                _observation(image=(2560, 1440), viewport=(1280, 720)),
                context=ReactPromptContext(step=0, phase="policy"),
            )
        )
        assert _points(agent, "Action: click(point='(500, 500)')") == [
            ("click_coordinate", {"coordinate_x": 640, "coordinate_y": 360})
        ]
        assert _points(agent, "Action: click(point='(1000, 1000)')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 719})
        ]

    async def test_the_image_size_is_not_needed_but_the_viewport_is(self) -> None:
        agent = _agent(coordinate_space="normalized_1000")
        (
            await agent.build_react_prompt(
                _observation(image=(0, 0), viewport=(1000, 800)),
                context=ReactPromptContext(step=0, phase="policy"),
            )
        )
        assert _points(agent, "Action: click(point='<point>250 500</point>')") == [
            ("click_coordinate", {"coordinate_x": 250, "coordinate_y": 400})
        ]
        (
            await agent.build_react_prompt(
                _observation(image=(1000, 800), viewport=(0, 0)),
                context=ReactPromptContext(step=0, phase="policy"),
            )
        )
        with pytest.raises(ParseError, match="screenshot has no known size"):
            agent.parse_react_output("Action: click(point='(250, 500)')")

    async def test_an_answer_past_1000_is_clamped_like_any_point_off_the_grid(self) -> None:
        agent = _agent(coordinate_space="normalized_1000")
        (
            await agent.build_react_prompt(
                _observation(image=(2560, 1440)), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        assert _points(agent, "Action: click(point='(1500, 300)')") == [
            ("click_coordinate", {"coordinate_x": 1279, "coordinate_y": 216})
        ]


class TestTheFrameIsPerStep:
    async def test_each_reply_is_placed_with_the_screenshot_its_step_showed(self) -> None:
        agent = _agent()
        (
            await agent.build_react_prompt(
                _observation(image=(2560, 1440)), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        assert _points(agent, "Action: click(point='(200, 200)')")[0][1] == {
            "coordinate_x": 100,
            "coordinate_y": 100,
        }
        (
            await agent.build_react_prompt(
                _observation(image=(1280, 720)), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        assert _points(agent, "Action: click(point='(200, 200)')")[0][1] == {
            "coordinate_x": 200,
            "coordinate_y": 200,
        }

    async def test_a_step_without_a_screenshot_places_no_point(self) -> None:
        agent = _agent()
        (
            await agent.build_react_prompt(
                _observation(image=(1280, 720)), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        (
            await agent.build_react_prompt(
                SimpleNamespace(
                    platform=SimpleNamespace(operating_system="macos"),
                    task=SimpleNamespace(user_request="t"),
                ),
                context=ReactPromptContext(step=0, phase="policy"),
            )
        )
        with pytest.raises(ParseError, match="screenshot has no known size"):
            agent.parse_react_output("Action: click(point='(10, 20)')")

    def test_verbs_without_a_point_need_no_screenshot(self) -> None:
        agent = _agent()
        reply = (
            "Action: scroll(direction='down')\n"
            "Action: hotkey(key='enter')\n"
            "Action: wait()\n"
            "Action: finished(content='ok')"
        )
        assert [name for name, _ in _points(agent, reply)] == [
            "scroll",
            "send_keys",
            "wait",
            "done",
        ]


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
    present, nothing it does not declare is sent, point params are ints,
    and the real action runs through the guard. The events it emits carry
    CSS pixels — half of what the model wrote on the DPR-2 screenshot.
    """
    agent = _agent()
    prompt = agent.prepare(
        task="t",
        action_descriptors=seam["descriptors"],
        execution_limits=ExecutionLimits(max_actions_per_step=9),
    )[0].content
    assert isinstance(prompt, str)
    assert "maximum of 9 actions" in prompt
    (
        await agent.build_react_prompt(
            _observation(image=(2560, 1440), viewport=(1280, 720)),
            context=ReactPromptContext(step=0, phase="policy"),
        )
    )

    actions = agent.parse_react_output(_EVERY_VERB)
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

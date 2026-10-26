"""``browser_use.dom_vision``'s second click verb: ``click_coordinate``, a point on the screenshot.

Two distinct verbs in ``browser_use.dom_vision`` — ``click`` (selector-map
index) and ``click_coordinate`` (point) — never one overloaded ``click``. The
point is taught on the grid ``coordinate_space`` names and executed by
``browser.click_coordinate`` in **CSS viewport pixels**; the two differ by the
device scale factor, and the framework converts in exactly one place
(``core/loops/coordinate_frames.py``), each step here driven as a Loop drives
it (``tests/_helpers/frames.py``).

The seam AGENTS.md §2.5 asks for is pinned end to end: negotiation against the
real ``BrowserCapability`` → the rendered prompt → a reply → ``parse_react_output``
→ the framework's conversion → ``guarded_act`` → the real
``ClickCoordinateAction`` → the emitted event.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.browser_use import (
    BrowserUseAgent,
    BrowserUseDomAgent,
    BrowserUseDomVisionAgent,
)
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.capabilities.browser.watchdogs.events import ClickCoordinateEvent
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.loops.coordinate_frames import step_frame
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.negotiation import negotiate_action
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation.image import image_size
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.action_spaces import space_for
from tests._helpers.assembly import make_agent, make_env
from tests._helpers.fragment_formats import dom_formats
from tests._helpers.frames import decided, step_context
from tests._helpers.replies import read_reply
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
    FakeScope,
)


def _png(width: int, height: int) -> bytes:
    """The first bytes of a real PNG: signature + IHDR, enough to be measured."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def _observation(*, image: tuple[int, int], viewport: tuple[int, int] = (1280, 720)) -> Any:
    shot = ScreenshotObs(
        image_bytes=_png(*image),
        viewport_width=viewport[0],
        viewport_height=viewport[1],
        image_width=image[0],
        image_height=image[1],
    )
    return SimpleNamespace(
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        task=SimpleNamespace(user_request="tick the checkbox", follow_ups=[]),
        screenshot=shot,
    )


def _harness(agent: BrowserUseAgent) -> dict[str, Any]:
    """Negotiate *this agent instance* against a real BrowserCapability."""
    capability = BrowserCapability()
    env = make_env(BrowserEnvironment, {"browser": capability})
    negotiated = negotiate_action(agent, env)
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
    space = space_for(agent, descriptors)
    agent.prepare(
        task="t",
        action_space=space,
        execution_limits=ExecutionLimits(max_actions_per_step=1),
        fragment_formats=dom_formats(),
    )
    return {
        "space": space,
        "negotiated": negotiated,
        "descriptors": descriptors,
        "guard": guard,
        "scope": fake._scopes["sid_main"],
        "prompt": None,
    }


def _agent(member: type[BrowserUseAgent] = BrowserUseDomVisionAgent, **kwargs: Any) -> Any:
    return make_agent(member, memory=SimpleMemory(max_messages=10), **kwargs)


@pytest.fixture
def harness() -> dict[str, Any]:
    agent = _agent()
    prepared = _harness(agent)
    prepared["agent"] = agent
    return prepared


def _prompt(agent: BrowserUseAgent, descriptors: list[Any]) -> str:
    content = agent.prepare(
        task="t",
        action_space=space_for(agent, descriptors),
        execution_limits=ExecutionLimits(max_actions_per_step=1),
        fragment_formats=dom_formats(),
    )[0].content
    assert isinstance(content, str)
    return content


def _reply(*items: dict[str, Any]) -> str:
    return json.dumps({"memory": "m", "action": list(items)})


class _Step:
    """One step of *harness*'s agent, built and decided as a Loop does it."""

    def __init__(self, harness: dict[str, Any], observation: Any) -> None:
        self.agent: BrowserUseAgent = harness["agent"]
        self.space = harness["space"]
        self.context: ReactPromptContext = step_context(self.space, observation)
        self.observation = observation

    async def build(self) -> list[Any]:
        return await self.agent.build_react_prompt(self.observation, context=self.context)

    def decide(self, reply: str) -> list[Action]:
        return decided(self.agent, self.space, reply, self.context)


async def _step(harness: dict[str, Any], observation: Any) -> _Step:
    step = _Step(harness, observation)
    await step.build()
    return step


class TestNegotiation:
    def test_both_click_verbs_are_active_and_distinct(self, harness) -> None:
        active = harness["negotiated"].active_actions
        assert {"browser.click", "browser.click_coordinate"} <= active

    def test_the_dom_member_activates_only_the_index_click(self) -> None:
        """It does not declare the coordinate verb, so no run of it can execute one."""
        active = _harness(_agent(BrowserUseDomAgent))["negotiated"].active_actions
        assert "browser.click" in active
        assert "browser.click_coordinate" not in active

    def test_prompt_teaches_both_under_their_own_names(self, harness) -> None:
        prompt = _prompt(harness["agent"], harness["descriptors"])
        assert "`browser.click(index:" in prompt
        assert "`browser.click_coordinate(coordinate_x:" in prompt
        # Taught in the model's unit, not the executor's.
        assert "x in pixels of the screenshot" in prompt
        assert "CSS px" not in prompt

    def test_normalized_grid_changes_what_the_prompt_teaches(self) -> None:
        agent = _agent(coordinate_space="normalized_1000")
        prepared = _harness(agent)
        prompt = _prompt(agent, prepared["descriptors"])
        assert "0-1000 grid" in prompt
        assert "pixels of the screenshot" not in prompt


class TestParamSeam:
    """What the parser emits is what ``descriptor().params`` declares."""

    async def test_emitted_params_are_exactly_the_declared_ones(self, harness) -> None:
        step = await _step(harness, _observation(image=(1280, 720)))
        (action,) = step.decide(
            _reply({"browser.click_coordinate": {"coordinate_x": 10, "coordinate_y": 20}})
        )
        descriptor = next(d for d in harness["descriptors"] if d.name == "browser.click_coordinate")
        assert descriptor.params_schema is not None
        required = set(descriptor.params_schema["required"])
        assert required <= set(action.params) <= set(descriptor.params)
        assert all(isinstance(action.params[k], int) for k in required)

    async def test_reply_parses_guards_and_executes_at_the_converted_point(self, harness) -> None:
        """DPR 2: a 2560x1440 image of a 1280x720 viewport halves every point."""
        step = _Step(harness, _observation(image=(2560, 1440)))
        messages = await step.build()
        content = messages[0].content
        assert isinstance(content, list)
        body = getattr(content[0], "text", "")
        assert "Screenshot: 2560x1440 pixels" in body

        (action,) = step.decide(
            _reply({"browser.click_coordinate": {"coordinate_x": 101, "coordinate_y": 400}})
        )
        result = await harness["guard"](action)
        assert result.success, result.error
        event = harness["scope"].emitted[-1]
        assert isinstance(event, ClickCoordinateEvent)
        assert (event.coordinate_x, event.coordinate_y) == (50, 200)

    async def test_dpr_one_is_the_identity(self, harness) -> None:
        step = await _step(harness, _observation(image=(1280, 720)))
        (action,) = step.decide(
            _reply({"browser.click_coordinate": {"coordinate_x": 61, "coordinate_y": 377}})
        )
        assert action.params == {"coordinate_x": 61, "coordinate_y": 377}

    async def test_a_normalized_point_lands_on_the_viewport(self) -> None:
        agent = _agent(coordinate_space="normalized_1000")
        harness = {**_harness(agent), "agent": agent}
        step = await _step(harness, _observation(image=(2560, 1440)))
        (action,) = step.decide(
            _reply({"browser.click_coordinate": {"coordinate_x": 500, "coordinate_y": 500}})
        )
        # 0-1000 is laid over the image whatever its pixel size, so the
        # image size does not enter the conversion at all.
        assert action.params == {"coordinate_x": 640, "coordinate_y": 360}

    async def test_index_click_is_untouched(self, harness) -> None:
        step = await _step(harness, _observation(image=(2560, 1440)))
        (action,) = step.decide(_reply({"browser.click": {"index": 42}}))
        assert action == Action(name="browser.click", params={"index": 42})


class TestRefusals:
    async def test_no_screenshot_this_step_is_a_parse_error(self, harness) -> None:
        step = await _step(
            harness,
            SimpleNamespace(
                platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
                task=SimpleNamespace(user_request="t", follow_ups=[]),
            ),
        )
        with pytest.raises(ParseError, match="screenshot has no known size") as excinfo:
            step.decide(
                _reply({"browser.click_coordinate": {"coordinate_x": 1, "coordinate_y": 2}})
            )
        assert excinfo.value.detail == (
            "action 1, browser.click_coordinate: a point cannot be placed, because this "
            "step's screenshot has no known size"
        )

    async def test_non_numeric_coordinate_is_refused_by_the_schema(self, harness) -> None:
        """Now caught one layer earlier, by the closed union's own types."""
        step = await _step(harness, _observation(image=(1280, 720)))
        with pytest.raises(ParseError, match="browser.click_coordinate: coordinate_x"):
            step.decide(
                _reply({"browser.click_coordinate": {"coordinate_x": "left", "coordinate_y": 2}})
            )

    def test_in_the_dom_member_it_is_neither_taught_nor_accepted(self) -> None:
        agent = _agent(BrowserUseDomAgent)
        prepared = _harness(agent)
        prompt = _prompt(agent, prepared["descriptors"])
        assert "click_coordinate" not in prompt
        assert "`browser.click(index:" in prompt
        with pytest.raises(
            ParseError, match="action 1, browser.click_coordinate is not an action this run"
        ):
            read_reply(
                agent,
                _reply({"browser.click_coordinate": {"coordinate_x": 1, "coordinate_y": 2}}),
                prepared["space"],
            )


class TestScreenshotGeometry:
    def test_image_size_reads_the_header(self) -> None:
        assert image_size(_png(2560, 1440)) == (2560, 1440)
        assert image_size(b"not an image") == (0, 0)

    async def test_fragment_without_image_size_refuses_to_guess(self, harness) -> None:
        obs = SimpleNamespace(
            platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
            task=SimpleNamespace(user_request="t", follow_ups=[]),
            screenshot=ScreenshotObs(
                image_bytes=_png(1280, 720), viewport_width=1280, viewport_height=720
            ),
        )
        step = await _step(harness, obs)
        with pytest.raises(ParseError):
            step.decide(
                _reply({"browser.click_coordinate": {"coordinate_x": 1, "coordinate_y": 2}})
            )

    def test_a_screenshot_without_geometry_stops_the_step(self, harness) -> None:
        """An assembly error the framework raises while it builds the step's frame."""
        obs = SimpleNamespace(screenshot=SimpleNamespace(image_bytes=_png(1280, 720)))
        with pytest.raises(TypeError, match="reports no screenshot geometry"):
            step_frame(harness["space"], obs)

"""browser_use's second click verb: ``click_coordinate``, a point on the screenshot.

Two distinct verbs with DOM on — ``click`` (selector-map index) and
``click_coordinate`` (point) — never one overloaded ``click``. The point is
taught and parsed in **screenshot pixels** and executed by
``browser.click_coordinate`` in **CSS viewport pixels**; the two grids differ
by the device scale factor, so the agent converts in exactly one place.

The seam CLAUDE.md §2.5 asks for is pinned end to end: negotiation against the
real ``BrowserCapability`` → the rendered prompt → a reply → ``parse_react_output``
→ ``guarded_act`` → the real ``ClickCoordinateAction`` → the emitted event.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.capabilities.browser.watchdogs.events import ClickCoordinateEvent
from spectragent.core.capabilities.browser.watchdogs.screenshot_watchdog import png_size
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.loops.errors import ParseError
from spectragent.core.negotiation import extract_wiring
from spectragent.core.negotiation.action import _action_rules
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.assembly import make_agent, make_env
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
        task=SimpleNamespace(user_request="tick the checkbox", follow_ups=[]),
        screenshot=shot,
    )


@pytest.fixture
def harness() -> dict[str, Any]:
    capability = BrowserCapability()
    env = make_env(BrowserEnvironment, {"browser": capability})
    negotiated = _action_rules(
        agent=extract_wiring(BrowserUseAgent, "action"),
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
    return {
        "negotiated": negotiated,
        "descriptors": descriptors,
        "guard": guard,
        "scope": fake._scopes["sid_main"],
    }


def _vision_agent() -> BrowserUseAgent:
    return make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=10), use_vision=True)


def _prompt(agent: BrowserUseAgent, descriptors: list[Any]) -> str:
    content = agent.prepare(task="t", action_descriptors=descriptors)[0].content
    assert isinstance(content, str)
    return content


def _reply(*items: dict[str, Any]) -> str:
    return json.dumps({"memory": "m", "action": list(items)})


class TestNegotiation:
    def test_both_click_verbs_are_active_and_distinct(self, harness) -> None:
        active = harness["negotiated"].active_actions
        assert {"browser.click", "browser.click_coordinate"} <= active
        assert "click_coordinate" in extract_wiring(BrowserUseAgent, "action")[1]
        assert "click" in extract_wiring(BrowserUseAgent, "action")[0]

    def test_prompt_teaches_both_under_their_own_names(self, harness) -> None:
        agent = _vision_agent()
        prompt = _prompt(agent, harness["descriptors"])
        assert "`browser.click(index:" in prompt
        assert "`browser.click_coordinate(coordinate_x:" in prompt
        # Taught in the model's unit, not the executor's.
        assert "x in screenshot pixels" in prompt
        assert "CSS px" not in prompt


class TestParamSeam:
    """What the parser emits is what ``descriptor().params`` declares."""

    def test_emitted_params_are_exactly_the_declared_ones(self, harness) -> None:
        agent = _vision_agent()
        agent.prepare(task="t", action_descriptors=harness["descriptors"])
        agent.build_react_prompt(_observation(image=(1280, 720)))
        (action,) = agent.parse_react_output(
            _reply({"click_coordinate": {"coordinate_x": 10, "coordinate_y": 20}})
        )
        descriptor = next(
            d for d in harness["descriptors"] if d.name == "browser.click_coordinate"
        )
        required = {k for k, v in descriptor.params.items() if "optional" not in v.lower()}
        assert required <= set(action.params) <= set(descriptor.params)
        assert all(isinstance(action.params[k], int) for k in required)

    async def test_reply_parses_guards_and_executes_at_the_converted_point(
        self, harness
    ) -> None:
        """DPR 2: a 2560x1440 image of a 1280x720 viewport halves every point."""
        agent = _vision_agent()
        agent.prepare(task="t", action_descriptors=harness["descriptors"])
        messages = agent.build_react_prompt(_observation(image=(2560, 1440)))
        content = messages[0].content
        assert isinstance(content, list)
        body = getattr(content[0], "text", "")
        assert "Screenshot: 2560x1440 pixels" in body

        (action,) = agent.parse_react_output(
            _reply({"click_coordinate": {"coordinate_x": 101, "coordinate_y": 400}})
        )
        result = await harness["guard"](action)
        assert result.success, result.error
        event = harness["scope"].emitted[-1]
        assert isinstance(event, ClickCoordinateEvent)
        assert (event.coordinate_x, event.coordinate_y) == (50, 200)

    def test_dpr_one_is_the_identity(self, harness) -> None:
        agent = _vision_agent()
        agent.prepare(task="t", action_descriptors=harness["descriptors"])
        agent.build_react_prompt(_observation(image=(1280, 720)))
        (action,) = agent.parse_react_output(
            _reply({"browser.click_coordinate": {"coordinate_x": 61, "coordinate_y": 377}})
        )
        assert action.params == {"coordinate_x": 61, "coordinate_y": 377}

    def test_index_click_is_untouched(self, harness) -> None:
        agent = _vision_agent()
        agent.prepare(task="t", action_descriptors=harness["descriptors"])
        agent.build_react_prompt(_observation(image=(2560, 1440)))
        (action,) = agent.parse_react_output(_reply({"click": {"index": 42}}))
        assert action == Action(name="click", params={"index": 42})


class TestRefusals:
    def test_no_screenshot_this_step_is_a_parse_error(self, harness) -> None:
        agent = _vision_agent()
        agent.prepare(task="t", action_descriptors=harness["descriptors"])
        agent.build_react_prompt(
            SimpleNamespace(task=SimpleNamespace(user_request="t", follow_ups=[]))
        )
        with pytest.raises(ParseError, match="no screenshot"):
            agent.parse_react_output(
                _reply({"click_coordinate": {"coordinate_x": 1, "coordinate_y": 2}})
            )

    def test_non_numeric_coordinate_is_a_parse_error(self, harness) -> None:
        agent = _vision_agent()
        agent.prepare(task="t", action_descriptors=harness["descriptors"])
        agent.build_react_prompt(_observation(image=(1280, 720)))
        with pytest.raises(ParseError, match="must be numbers"):
            agent.parse_react_output(
                _reply({"click_coordinate": {"coordinate_x": "left", "coordinate_y": 2}})
            )

    def test_without_vision_it_is_neither_taught_nor_accepted(self, harness) -> None:
        agent = make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=10))
        prompt = _prompt(agent, harness["descriptors"])
        assert "click_coordinate" not in prompt
        assert "`browser.click(index:" in prompt
        with pytest.raises(ParseError, match="unknown action"):
            agent.parse_react_output(
                _reply({"click_coordinate": {"coordinate_x": 1, "coordinate_y": 2}})
            )


class TestScreenshotGeometry:
    def test_png_size_reads_the_header(self) -> None:
        assert png_size(_png(2560, 1440)) == (2560, 1440)
        assert png_size(b"not a png") == (0, 0)

    def test_fragment_without_image_size_refuses_to_guess(self, harness) -> None:
        agent = _vision_agent()
        agent.prepare(task="t", action_descriptors=harness["descriptors"])
        obs = SimpleNamespace(
            task=SimpleNamespace(user_request="t", follow_ups=[]),
            screenshot=ScreenshotObs(image_bytes=_png(1280, 720), viewport_width=1280, viewport_height=720),
        )
        agent.build_react_prompt(obs)
        with pytest.raises(ParseError):
            agent.parse_react_output(
                _reply({"click_coordinate": {"coordinate_x": 1, "coordinate_y": 2}})
            )

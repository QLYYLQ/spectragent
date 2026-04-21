"""Tests for the M14 UITarsAgent ReactLoop slot adaptation.

UITars is the most special case: pure-vision (no DOM observation),
free-form text output, AST coordinate parsing, and an action vocabulary
that uses ``click_coordinate`` rather than DOM-index ``click``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.llm.messages import (
    BaseMessage,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops import ReactLoop
from spectragent.schemas.action import Action


@dataclass
class _FakeScreenshot:
    image_bytes: bytes | None = None
    image_path: str | None = None
    viewport_width: int = 0
    viewport_height: int = 0


class _FakeObservation:
    """UITars only ever consumes ``screenshot`` — no DOM, no browser_meta."""

    def __init__(self, *, screenshot: _FakeScreenshot | None = None) -> None:
        self.screenshot = screenshot


def _make_agent(**overrides: Any) -> UITarsAgent:
    from tests._helpers.assembly import make_agent
    return make_agent(
        UITarsAgent, memory=SimpleMemory(max_messages=100), **overrides,
    )


class TestSlotConformance:
    def test_has_required_react_slots(self) -> None:
        agent = _make_agent()
        ReactLoop()._validate_slots(agent)  # Should not raise

    def test_legacy_methods_removed(self) -> None:
        agent = _make_agent()
        assert not hasattr(agent, "run")
        assert not hasattr(agent, "step")
        assert not hasattr(agent, "build_messages")

    def test_agent_id_and_domain(self) -> None:
        agent = _make_agent()
        assert agent.agent_id == "ui_tars"
        assert agent.domain == "web_navigation_vision"


class TestWiring:
    def test_pure_vision_observation_wiring(self) -> None:
        # UI-TARS requires only screenshot, optional list is empty.
        wiring = UITarsAgent.__wiring__
        assert wiring["observation"] == ("observation", ["screenshot"], [])

    def test_does_not_require_dom(self) -> None:
        wiring = UITarsAgent.__wiring__
        registry, required, optional = wiring["observation"]
        assert "dom" not in required
        assert "dom" not in optional

    def test_agent_loop_wiring(self) -> None:
        wiring = UITarsAgent.__wiring__
        assert wiring["agent_loop"] == ("agent_loop", ["react"])


class TestBuildReactPrompt:
    def test_attaches_screenshot_when_present(self) -> None:
        agent = _make_agent()
        agent.prepare(task="click the search box", action_descriptors=[])
        obs = _FakeObservation(
            screenshot=_FakeScreenshot(image_bytes=b"\x89PNG\r\n\x1a\nFAKE")
        )
        messages = agent.build_react_prompt(obs)
        assert isinstance(messages, list)
        assert all(isinstance(m, BaseMessage) for m in messages)
        # Find the user message with multi-modal content
        user_msgs = [m for m in messages if isinstance(m, UserMessage)]
        assert len(user_msgs) == 1
        content = user_msgs[0].content
        # Multi-modal: list with text + image
        assert isinstance(content, list)
        assert len(content) == 2

    def test_falls_back_to_text_when_no_screenshot(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation()  # no screenshot
        agent.build_react_prompt(obs)
        user_msgs = [
            m for m in agent.memory.get_messages() if isinstance(m, UserMessage)
        ]
        assert len(user_msgs) == 1
        assert user_msgs[0].content == "(no screenshot available)"

    def test_first_call_includes_system_prompt(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            screenshot=_FakeScreenshot(image_bytes=b"\x89PNG\r\n\x1a\n")
        )
        messages = agent.build_react_prompt(obs)
        assert any(isinstance(m, SystemMessage) for m in messages)


class TestParseReactOutput:
    def test_parses_click_with_point_to_click_coordinate(self) -> None:
        agent = _make_agent()
        text = "Thought: I need to click the search box.\nAction: click(point='(640, 360)')"
        actions = agent.parse_react_output(text)
        assert len(actions) == 1
        action = actions[0]
        assert isinstance(action, Action)
        # M14: UITars MUST emit click_coordinate (not click + index)
        assert action.name == "click_coordinate"
        assert action.params["coordinate_x"] == 640
        assert action.params["coordinate_y"] == 360

    def test_parses_click_with_alternative_coordinate_format(self) -> None:
        agent = _make_agent()
        text = "Action: click(point='100,200')"
        actions = agent.parse_react_output(text)
        assert len(actions) == 1
        assert actions[0].name == "click_coordinate"
        assert actions[0].params == {"coordinate_x": 100, "coordinate_y": 200}

    def test_parses_finished_to_done_with_extracted_content(self) -> None:
        # C2 regression: the UITars ``finished`` action carries its
        # answer text under ``extracted_content`` (not ``text``) so
        # BrowserCapability/DoneEvent can read it.
        agent = _make_agent()
        text = "Action: finished(content='task complete')"
        actions = agent.parse_react_output(text)
        assert len(actions) == 1
        assert actions[0].name == "done"
        assert actions[0].params["extracted_content"] == "task complete"
        assert actions[0].params["success"] is True

    def test_parses_type_action(self) -> None:
        agent = _make_agent()
        text = "Action: type(content='hello world', point='(100, 100)')"
        actions = agent.parse_react_output(text)
        assert len(actions) == 1
        assert actions[0].name == "type"
        assert actions[0].params.get("content") == "hello world"

    def test_no_action_pattern_raises_parse_error(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        text = "Thought: I should wait for the page to settle."
        with pytest.raises(ParseError, match="No 'Action:"):
            agent.parse_react_output(text)

    def test_invalid_click_coordinate_raises_parse_error(self) -> None:
        # If the coordinate parser cannot extract x/y, the click is dropped
        # and no valid actions remain → ParseError.
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        text = "Action: click(point='invalid')"
        with pytest.raises(ParseError, match="none were parseable"):
            agent.parse_react_output(text)


class TestOutputModel:
    def test_returns_none_for_freeform_text(self) -> None:
        agent = _make_agent()
        assert agent.get_react_output_model() is None

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
from spectragent.agents.ui_tars import _VERB_TABLE, UITarsAgent
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartTextParam,
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


@dataclass
class _FakeTask:
    user_request: str = "click the search box"
    follow_ups: list[str] | None = None

    def __post_init__(self) -> None:
        if self.follow_ups is None:
            self.follow_ups = []


class _FakeObservation:
    """UITars only ever consumes ``screenshot`` + the loop ``task`` fragment."""

    def __init__(
        self,
        *,
        screenshot: _FakeScreenshot | None = None,
        task: _FakeTask | None = None,
    ) -> None:
        self.screenshot = screenshot
        self.task = task or _FakeTask()


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
        # Capability-relative: a screenshot is a screenshot whether a
        # browser, a desktop VM or a phone produced it.
        wiring = UITarsAgent.__wiring__
        assert wiring["observation"] == (
            "observation",
            ["screenshot", "task"],
            ["step_info"],
        )

    def test_does_not_require_dom(self) -> None:
        wiring = UITarsAgent.__wiring__
        registry, required, optional = wiring["observation"]
        assert "dom" not in required
        assert "dom" not in optional

    def test_agent_loop_wiring(self) -> None:
        wiring = UITarsAgent.__wiring__
        assert wiring["agent_loop"] == ("agent_loop", ["react"])

    def test_no_key_names_a_capability(self) -> None:
        """The whole point: nothing in the wiring says ``browser``."""
        for entry in UITarsAgent.__wiring__.values():
            if isinstance(entry, str):
                continue
            for key_list in entry[1:]:
                assert all("." not in k for k in key_list), key_list

    def test_no_capability_entry(self) -> None:
        assert "capability" not in UITarsAgent.__wiring__


class TestVerbTableAndWiringAgree:
    """The structural guard whose absence let ``type`` ship unexecutable.

    The prompt is generated from the verb table intersected with the
    negotiated descriptors, and the parser emits from the same table. So
    a verb whose action is not wired can never run, and a wired action
    with no verb can never be advertised — both are drift, and both are
    invisible until a live run.
    """

    def test_every_verb_maps_to_a_wired_action(self) -> None:
        _registry, required, optional = UITarsAgent.__wiring__["action"]
        wired = set(required) | set(optional)
        assert {v.action for v in _VERB_TABLE.values()} == wired

    def test_every_verb_names_no_capability(self) -> None:
        assert all("." not in v.action for v in _VERB_TABLE.values())


class TestPrepare:
    def test_prepare_returns_system_prompt_seed(self) -> None:
        agent = _make_agent()
        seed = agent.prepare(task="t", action_descriptors=[])
        assert isinstance(seed, list)
        assert len(seed) == 1
        assert isinstance(seed[0], SystemMessage)
        # Pure builder — agent memory remains untouched.
        assert agent.memory.get_messages() == []


class TestBuildReactPrompt:
    def test_attaches_screenshot_when_present(self) -> None:
        agent = _make_agent()
        agent.prepare(task="click the search box", action_descriptors=[])
        obs = _FakeObservation(
            screenshot=_FakeScreenshot(image_bytes=b"\x89PNG\r\n\x1a\nFAKE"),
            task=_FakeTask(user_request="click the search box"),
        )
        messages = agent.build_react_prompt(obs)
        assert isinstance(messages, list)
        assert all(isinstance(m, BaseMessage) for m in messages)
        # Pure builder: a single new user message this step.
        assert len(messages) == 1
        assert isinstance(messages[0], UserMessage)
        content = messages[0].content
        # Multi-modal: list with text + image
        assert isinstance(content, list)
        assert len(content) == 2

    def test_task_text_pulled_from_observation(self) -> None:
        agent = _make_agent()
        agent.prepare(task="ignored-old-value", action_descriptors=[])
        obs = _FakeObservation(
            screenshot=_FakeScreenshot(image_bytes=b"\x89PNG"),
            task=_FakeTask(user_request="open settings menu"),
        )
        messages = agent.build_react_prompt(obs)
        content = messages[0].content
        assert isinstance(content, list)
        text_parts = [p for p in content if isinstance(p, ContentPartTextParam)]
        assert any("open settings menu" in p.text for p in text_parts)
        # Confirms task does NOT come from agent state (agent has no _task).
        assert not hasattr(agent, "_task")

    def test_falls_back_to_text_when_no_screenshot(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(task=_FakeTask(user_request="some task"))
        messages = agent.build_react_prompt(obs)
        assert len(messages) == 1
        assert isinstance(messages[0], UserMessage)
        content = messages[0].content
        assert isinstance(content, str)
        assert "some task" in content
        assert "no screenshot available" in content


class TestParseReactOutput:
    def test_parses_click_with_point_to_click_coordinate(self) -> None:
        agent = _make_agent()
        text = "Thought: I need to click the search box.\nAction: click(point='(640, 360)')"
        actions = agent.parse_react_output(text)
        assert len(actions) == 1
        action = actions[0]
        assert isinstance(action, Action)
        # UITars MUST emit click_coordinate (not click + index), and it
        # must name no capability — the same coordinate click exists on
        # a desktop VM and an Android emulator.
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

    def test_parses_type_to_the_executable_shape(self) -> None:
        """R2: ``type`` used to emit ``{content, point}`` and be 100% dead.

        ``TypeAction`` reads ``index`` (which a vision-only run has no
        selector map for) and ``text`` (which the agent spelled
        ``content``), so every typed step returned ``Missing param:
        'index'`` — and had an index been supplied it would have typed
        the empty string.
        """
        agent = _make_agent()
        text = "Action: type(content='hello world', point='(100, 100)')"
        actions = agent.parse_react_output(text)
        assert len(actions) == 1
        assert actions[0].name == "type_coordinate"
        assert actions[0].params == {
            "coordinate_x": 100,
            "coordinate_y": 100,
            "text": "hello world",
        }

    def test_type_without_a_point_is_a_loud_parse_error(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        with pytest.raises(ParseError, match="not a coordinate"):
            agent.parse_react_output("Action: type(content='hello')")

    def test_hotkey_is_renamed_onto_send_keys(self) -> None:
        agent = _make_agent()
        actions = agent.parse_react_output("Action: hotkey(key='ctrl+a')")
        assert actions[0].name == "send_keys"
        assert actions[0].params == {"keys": "ctrl+a"}

    def test_drag_carries_both_endpoints(self) -> None:
        agent = _make_agent()
        actions = agent.parse_react_output(
            "Action: drag(start_point='(1, 2)', end_point='(3, 4)')"
        )
        assert actions[0].name == "drag"
        assert actions[0].params == {
            "start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4,
        }

    def test_wait_takes_no_arguments(self) -> None:
        agent = _make_agent()
        actions = agent.parse_react_output("Action: wait()")
        assert actions[0].name == "wait"
        assert actions[0].params == {}

    def test_scroll_needs_a_direction(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        actions = agent.parse_react_output(
            "Action: scroll(point='(5, 5)', direction='down')"
        )
        assert actions[0].name == "scroll"
        # ``point`` is dropped: ScrollAction has nowhere to put it, and
        # forwarding it would advertise a parameter nothing reads.
        assert actions[0].params == {"direction": "down"}
        with pytest.raises(ParseError, match="needs direction"):
            agent.parse_react_output("Action: scroll(point='(5, 5)')")

    def test_an_unknown_verb_fails_loudly_once(self) -> None:
        """Was six silent per-step guard rejections; now one parse error.

        ``long_press`` / ``open_app`` / ``press_home`` / ``press_back``
        are mobile-only and have no browser action, so they are gone from
        the prompt too — nothing advertises them any more.
        """
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        for verb in ("long_press", "open_app", "press_home", "press_back"):
            with pytest.raises(ParseError, match="Unknown UI-TARS verb"):
                agent.parse_react_output(f"Action: {verb}(point='(1, 2)')")

    def test_no_action_pattern_raises_parse_error(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        text = "Thought: I should wait for the page to settle."
        with pytest.raises(ParseError, match="No 'Action:"):
            agent.parse_react_output(text)

    def test_invalid_click_coordinate_raises_parse_error(self) -> None:
        # A click whose point does not parse is unexecutable, so it fails
        # at parse time naming the argument rather than reaching the
        # action and coming back as ``Missing param``.
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        text = "Action: click(point='invalid')"
        with pytest.raises(ParseError, match="not a coordinate"):
            agent.parse_react_output(text)


class TestOutputModel:
    def test_returns_none_for_freeform_text(self) -> None:
        agent = _make_agent()
        assert agent.get_react_output_model() is None

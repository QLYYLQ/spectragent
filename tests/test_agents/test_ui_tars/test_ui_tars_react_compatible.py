"""Tests for the M14 UITarsAgent ReactLoop slot adaptation.

UITars is the most special case: pure-vision (no DOM observation),
free-form text output, AST coordinate parsing, and an action vocabulary
that uses ``click_coordinate`` rather than DOM-index ``click``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.agents.ui_tars.codec import VERBS
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartTextParam,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops import ReactLoop
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.action_spaces import space_of, wired_space


@dataclass
class _FakeScreenshot:
    def model_image_bytes(self) -> bytes | None:
        if self.image_bytes is not None:
            return self.image_bytes
        if self.image_path:
            from pathlib import Path

            return Path(self.image_path).read_bytes()
        return None

    image_bytes: bytes | None = None
    image_path: str | None = None
    viewport_width: int = 0
    viewport_height: int = 0
    image_width: int = 0
    image_height: int = 0
    source_url: str | None = None


@dataclass
class _FakeTask:
    user_request: str = "click the search box"
    follow_ups: list[str] | None = None

    def __post_init__(self) -> None:
        if self.follow_ups is None:
            self.follow_ups = []


class _FakeObservation:
    """Visual page content plus measured platform and the loop task."""

    def __init__(
        self,
        *,
        screenshot: _FakeScreenshot | None = None,
        task: _FakeTask | None = None,
    ) -> None:
        self.screenshot = screenshot
        self.task = task or _FakeTask()
        self.platform = SimpleNamespace(operating_system="macos", fragment_id="browser.platform")
        self.history = SimpleNamespace(items=[])


def _make_agent(**overrides: Any) -> UITarsAgent:
    """An agent prepared with every action its wiring names, as a browser run gives it."""
    from tests._helpers.assembly import make_agent

    agent = make_agent(
        UITarsAgent,
        memory=SimpleMemory(max_messages=100),
        **overrides,
    )
    agent.prepare(
        task="t",
        action_space=wired_space(UITarsAgent),
        execution_limits=ExecutionLimits(max_actions_per_step=6),
    )
    return agent


async def _looking_at_screen(**overrides: Any) -> UITarsAgent:
    """An agent that has been shown a 1280x720 screenshot of a 1280x720 viewport.

    A point in a reply is placed with the screenshot the same step showed,
    so every verb test that emits a point starts here. DPR 1, so the
    numbers a test writes are the numbers the action receives.
    """
    agent = _make_agent(**overrides)
    (
        await agent.build_react_prompt(
            _FakeObservation(
                screenshot=_FakeScreenshot(
                    image_bytes=b"\x89PNG",
                    viewport_width=1280,
                    viewport_height=720,
                    image_width=1280,
                    image_height=720,
                )
            ),
            context=ReactPromptContext(step=0, phase="policy"),
        )
    )
    return agent


class TestSlotConformance:
    async def test_screenshot_provenance_does_not_reach_the_model(self) -> None:
        before = _FakeObservation(screenshot=_FakeScreenshot(image_bytes=b"\x89PNG"))
        after = _FakeObservation(
            screenshot=_FakeScreenshot(
                image_bytes=b"\x89PNG", source_url="https://private.test/only-for-restoration"
            )
        )
        assert (
            await _make_agent().build_react_prompt(
                before, context=ReactPromptContext(step=0, phase="policy")
            )
        ) == (
            await _make_agent().build_react_prompt(
                after, context=ReactPromptContext(step=0, phase="policy")
            )
        )

    def test_has_required_react_slots(self) -> None:
        agent = _make_agent()
        ReactLoop().validate_slots(agent)  # Should not raise

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
        # browser, a desktop VM or a phone produced it. ``capture_state`` is
        # the platform-neutral capture fact, optional; browser page state
        # (``browser_meta``) is for browser-specific agents and is not here
        # (docs/architecture_design.md §4.4, "观测增强按消费方分层").
        wiring = UITarsAgent.__wiring__
        assert wiring["observation"] == {
            "registry": "observation",
            "keys": ["screenshot", "platform", "history"],
            "optional_keys": ["step_info", "capture_state"],
            "declare_only": True,
        }

    def test_does_not_require_dom(self) -> None:
        wiring = UITarsAgent.__wiring__
        entry = wiring["observation"]
        required, optional = entry["keys"], entry["optional_keys"]
        assert "dom" not in required
        assert "dom" not in optional

    def test_agent_loop_wiring(self) -> None:
        wiring = UITarsAgent.__wiring__
        assert wiring["agent_loop"] == ("agent_loop", ["react", "branch"])

    def test_no_key_names_a_capability(self) -> None:
        """The whole point: nothing in the wiring says ``browser``."""
        for entry in UITarsAgent.__wiring__.values():
            if isinstance(entry, str):
                continue
            for key_list in (
                [entry["keys"], entry.get("optional_keys", [])]
                if isinstance(entry, dict)
                else entry[1:]
            ):
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
        entry = UITarsAgent.__wiring__["action"]
        required, optional = entry["keys"], entry["optional_keys"]
        wired = set(required) | set(optional)
        assert {v.action for v in VERBS.values()} == wired

    def test_every_verb_names_no_capability(self) -> None:
        assert all("." not in v.action for v in VERBS.values())

    def test_every_argument_a_signature_teaches_is_one_the_verb_accepts(self) -> None:
        """A taught argument the verb refused would make its own prompt unparseable."""
        for name, verb in VERBS.items():
            taught = set(re.findall(r"(\w+)=", verb.signature))
            assert taught <= set(verb.accepts), (name, taught - set(verb.accepts))

    def test_every_ui_tars_spelling_of_a_taught_point_is_accepted(self) -> None:
        """``start_box`` / ``end_box`` are what fine-tuned UI-TARS weights write."""
        written_by_ui_tars = {
            "point": {"start_box"},
            "start_point": {"start_box"},
            "end_point": {"end_box"},
        }
        for name, verb in VERBS.items():
            for argument in verb.arguments:
                if argument.kind == "point":
                    assert written_by_ui_tars[argument.name] <= set(verb.accepts), (
                        name,
                        argument.name,
                    )


class TestPrepare:
    def test_prepare_returns_system_prompt_seed(self) -> None:
        agent = _make_agent()
        seed = agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        assert isinstance(seed, list)
        assert len(seed) == 1
        assert isinstance(seed[0], SystemMessage)
        # Pure builder — agent memory remains untouched.
        assert agent.memory.get_messages() == []


class TestBuildReactPrompt:
    async def test_attaches_screenshot_when_present(self) -> None:
        agent = _make_agent()
        agent.prepare(
            task="click the search box",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        obs = _FakeObservation(
            screenshot=_FakeScreenshot(image_bytes=b"\x89PNG\r\n\x1a\nFAKE"),
            task=_FakeTask(user_request="click the search box"),
        )
        messages = await agent.build_react_prompt(
            obs, context=ReactPromptContext(step=0, phase="policy")
        )
        assert isinstance(messages, list)
        assert all(isinstance(m, BaseMessage) for m in messages)
        # The first observation declares its measured platform, then its frame.
        assert len(messages) == 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], UserMessage)
        content = messages[1].content
        # Multi-modal: list with text + image
        assert isinstance(content, list)
        assert len(content) == 2

    async def test_the_image_is_labelled_from_its_own_bytes(self) -> None:
        """A JPEG capture must not be shipped under a ``data:image/png`` prefix.

        ``screenshot_format: jpeg`` on the capability hands this agent JPEG
        bytes; the prefix is what Anthropic validates the payload against.
        """
        import io

        from PIL import Image

        from spectragent.core.llm.messages import ContentPartImageParam

        buffer = io.BytesIO()
        Image.new("RGB", (1280, 720)).save(buffer, format="JPEG", quality=70)

        agent = _make_agent()
        agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        obs = _FakeObservation(
            screenshot=_FakeScreenshot(image_bytes=buffer.getvalue()),
            task=_FakeTask(user_request="t"),
        )
        content = (
            await agent.build_react_prompt(obs, context=ReactPromptContext(step=0, phase="policy"))
        )[-1].content
        assert isinstance(content, list)
        image = next(p for p in content if isinstance(p, ContentPartImageParam))
        assert image.image_url.url.startswith("data:image/jpeg;base64,")
        assert image.image_url.media_type == "image/jpeg"

    async def test_task_is_declared_only_in_the_seed(self) -> None:
        agent = _make_agent()
        seed = agent.prepare(
            task="open settings menu",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        obs = _FakeObservation(
            screenshot=_FakeScreenshot(image_bytes=b"\x89PNG"),
            task=_FakeTask(user_request="open settings menu"),
        )
        messages = await agent.build_react_prompt(
            obs, context=ReactPromptContext(step=0, phase="policy")
        )
        content = messages[-1].content
        assert isinstance(content, list)
        text_parts = [p for p in content if isinstance(p, ContentPartTextParam)]
        assert "open settings menu" in str(seed[0].content)
        assert all("open settings menu" not in p.text for p in text_parts)
        assert all("Task:" not in p.text for p in text_parts)
        assert not hasattr(agent, "_task")

    async def test_falls_back_to_text_when_no_screenshot(self) -> None:
        agent = _make_agent()
        agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        obs = _FakeObservation(task=_FakeTask(user_request="some task"))
        messages = await agent.build_react_prompt(
            obs, context=ReactPromptContext(step=0, phase="policy")
        )
        assert len(messages) == 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], UserMessage)
        content = messages[1].content
        assert isinstance(content, str)
        assert "some task" not in content
        assert "no screenshot available" in content


class TestParseReactOutput:
    """Verb → action translation. Reply grammar lives in ``test_ui_tars_parser``."""

    async def test_parses_click_with_point_to_click_coordinate(self) -> None:
        agent = await _looking_at_screen()
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

    async def test_parses_click_with_alternative_coordinate_format(self) -> None:
        agent = await _looking_at_screen()
        text = "Action: click(point='100,200')"
        actions = agent.parse_react_output(text)
        assert len(actions) == 1
        assert actions[0].name == "click_coordinate"
        assert actions[0].params == {"coordinate_x": 100, "coordinate_y": 200}

    def test_parses_finished_to_done_with_extracted_content(self) -> None:
        # Regression: the UITars ``finished`` action carries its
        # answer text under ``extracted_content`` (not ``text``) so
        # BrowserCapability/DoneEvent can read it.
        agent = _make_agent()
        text = "Action: finished(content='task complete')"
        actions = agent.parse_react_output(text)
        assert len(actions) == 1
        assert actions[0].name == "done"
        assert actions[0].params["extracted_content"] == "task complete"
        # ``finished`` says nothing about whether the task was accomplished, so
        # it claims nothing: no ``success`` for the done result to carry.
        assert "success" not in actions[0].params

    async def test_parses_type_to_the_executable_shape(self) -> None:
        """``type`` used to emit ``{content, point}`` and be 100% dead.

        ``TypeAction`` reads ``index`` (which a vision-only run has no
        selector map for) and ``text`` (which the agent spelled
        ``content``), so every typed step returned ``Missing param:
        'index'`` — and had an index been supplied it would have typed
        the empty string.
        """
        agent = await _looking_at_screen()
        text = "Action: type(content='hello world', point='(100, 100)')"
        actions = agent.parse_react_output(text)
        assert len(actions) == 1
        assert actions[0].name == "type_coordinate"
        assert actions[0].params == {
            "coordinate_x": 100,
            "coordinate_y": 100,
            "text": "hello world",
        }

    async def test_type_without_a_point_is_a_loud_parse_error(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = await _looking_at_screen()
        with pytest.raises(ParseError, match=r"a point is missing; write point='\(x, y\)'"):
            agent.parse_react_output("Action: type(content='hello')")

    async def test_type_without_content_is_a_loud_parse_error(self) -> None:
        """``clear_by_default`` would wipe the field and report success."""
        from spectragent.core.loops.errors import ParseError

        agent = await _looking_at_screen()
        with pytest.raises(ParseError, match="type needs content="):
            agent.parse_react_output("Action: type(point='(1, 2)')")

    def test_hotkey_is_renamed_onto_send_keys(self) -> None:
        agent = _make_agent()
        actions = agent.parse_react_output("Action: hotkey(key='ctrl+a')")
        assert actions[0].name == "send_keys"
        assert actions[0].params == {"keys": "ctrl+a"}

    def test_a_ui_tars_1_5_space_separated_chord_becomes_a_plus_chord(self) -> None:
        """The executor splits a chord on ``+``; ``'ctrl c'`` would be one key."""
        agent = _make_agent()
        actions = agent.parse_react_output("Action: hotkey(key='ctrl shift t')")
        assert actions[0].params == {"keys": "ctrl+shift+t"}
        single = agent.parse_react_output("Action: hotkey(key='enter')")
        assert single[0].params == {"keys": "enter"}

    def test_a_list_of_key_names_is_a_chord(self) -> None:
        agent = _make_agent()
        actions = agent.parse_react_output("Action: hotkey(key=['ctrl', 'shift t'])")
        assert actions[0].params == {"keys": "ctrl+shift+t"}

    def test_key_and_keys_together_are_refused(self) -> None:
        """Both are accepted spellings; two of them in one call need not agree."""
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        with pytest.raises(ParseError, match="key and keys say the same thing; write only key"):
            agent.parse_react_output("Action: hotkey(key='ctrl+a', keys='ctrl+c')")

    async def test_drag_carries_both_endpoints(self) -> None:
        agent = await _looking_at_screen()
        actions = agent.parse_react_output("Action: drag(start_point='(1, 2)', end_point='(3, 4)')")
        assert actions[0].name == "drag"
        assert actions[0].params == {
            "start_x": 1,
            "start_y": 2,
            "end_x": 3,
            "end_y": 4,
        }

    def test_wait_takes_no_arguments(self) -> None:
        agent = _make_agent()
        actions = agent.parse_react_output("Action: wait()")
        assert actions[0].name == "wait"
        assert actions[0].params == {}

    def test_scroll_takes_a_direction_and_nothing_else(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        actions = agent.parse_react_output("Action: scroll(direction='Down')")
        assert actions[0].name == "scroll"
        assert actions[0].params == {"direction": "down"}
        with pytest.raises(ParseError, match="needs direction"):
            agent.parse_react_output("Action: scroll()")
        with pytest.raises(ParseError, match="needs direction"):
            agent.parse_react_output("Action: scroll(direction='sideways')")

    def test_scroll_refuses_the_point_ui_tars_weights_write(self) -> None:
        """Was silently dropped, so the model believed it scrolled at its point.

        ``ScrollAction`` has nowhere to put a point: it scrolls under the mouse
        pointer or a selector-map element, and a vision-only run has no
        selector map. The refusal names the argument and the taught form.
        """
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        with pytest.raises(ParseError) as excinfo:
            agent.parse_react_output("Action: scroll(point='(5, 5)', direction='down')")
        assert excinfo.value.detail.endswith(
            "scroll() does not take point; write it as scroll(direction='up|down|left|right')"
        )
        with pytest.raises(ParseError, match="does not take amount"):
            agent.parse_react_output("Action: scroll(direction='down', amount=600)")

    def test_an_unknown_verb_fails_loudly_once(self) -> None:
        """Was six silent per-step guard rejections; now one parse error.

        ``long_press`` / ``open_app`` / ``press_home`` / ``press_back``
        are mobile-only and have no browser action, so they are gone from
        the prompt too — nothing advertises them any more.
        """
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        for verb in ("long_press", "open_app", "press_home", "press_back"):
            with pytest.raises(ParseError, match=f"unknown verb '{verb}'"):
                agent.parse_react_output(f"Action: {verb}(point='(1, 2)')")

    def test_no_action_pattern_raises_parse_error(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        text = "Thought: I should wait for the page to settle."
        with pytest.raises(ParseError, match="No line starts with 'Action:'"):
            agent.parse_react_output(text)

    async def test_invalid_click_coordinate_raises_parse_error(self) -> None:
        # A click whose point does not parse is unexecutable, so it fails
        # at parse time naming the argument rather than reaching the
        # action and coming back as ``Missing param``.
        from spectragent.core.loops.errors import ParseError

        agent = await _looking_at_screen()
        text = "Action: click(point='invalid')"
        with pytest.raises(ParseError, match="not a coordinate"):
            agent.parse_react_output(text)


class TestOutputModel:
    def test_returns_none_for_freeform_text(self) -> None:
        agent = _make_agent()
        assert agent.get_react_output_model() is None

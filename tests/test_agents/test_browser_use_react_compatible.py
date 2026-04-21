"""Tests for the M14 BrowserUseAgent ReactLoop slot adaptation.

These tests verify:
- BrowserUseAgent has the required ReactLoop slots
- ``__wiring__`` declares the correct cross-registry constraints
- ``prepare()`` builds the system prompt and seeds memory
- ``build_react_prompt(observation)`` formats fragment-based observations
- ``parse_react_output()`` produces the expected action list
- Memory is touched only by ``prepare`` and ``build_react_prompt``
- The legacy ``BaseReactAgent`` execution methods are gone

Tests do not exercise live LLM/CDP paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from spectragent.agents._output_models import (
    BrowserUseActionItem,
    BrowserUseOutput,
    BrowserUseStateAssessment,
)
from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.llm.messages import (
    BaseMessage,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops import ReactLoop
from spectragent.schemas.action import Action, ActionDescriptor


# ── Test fixtures ─────────────────────────────────────────────────────


@dataclass
class _FakeBrowserMeta:
    url: str = ""
    page_title: str = ""
    tabs: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tabs is None:
            self.tabs = []


@dataclass
class _FakeDOM:
    element_tree: Any = None
    selector_map: dict | None = None


@dataclass
class _FakeScreenshot:
    image_bytes: bytes | None = None
    image_path: str | None = None
    viewport_width: int = 0
    viewport_height: int = 0


class _FakeObservation:
    """Light fragment-shaped observation used by tests."""

    def __init__(
        self,
        *,
        dom: _FakeDOM | None = None,
        screenshot: _FakeScreenshot | None = None,
        browser_meta: _FakeBrowserMeta | None = None,
    ) -> None:
        self.dom = dom
        self.screenshot = screenshot
        self.browser_meta = browser_meta


def _make_agent(**overrides: Any) -> BrowserUseAgent:
    from tests._helpers.assembly import make_agent
    return make_agent(
        BrowserUseAgent, memory=SimpleMemory(max_messages=100), **overrides,
    )


# ── Protocol conformance ──────────────────────────────────────────────


class TestSlotConformance:
    def test_has_required_react_slots(self) -> None:
        agent = _make_agent()
        loop = ReactLoop()
        loop._validate_slots(agent)  # Should not raise

    def test_legacy_execution_methods_removed(self) -> None:
        agent = _make_agent()
        # M14: BrowserUseAgent no longer inherits from BaseReactAgent
        # so it must not expose run/step/build_messages.
        assert not hasattr(agent, "run")
        assert not hasattr(agent, "step")
        assert not hasattr(agent, "build_messages")

    def test_agent_id_and_domain(self) -> None:
        agent = _make_agent()
        assert agent.agent_id == "browser_use"
        assert agent.domain == "web_navigation"


# ── Wiring ────────────────────────────────────────────────────────────


class TestWiring:
    def test_agent_loop_wiring(self) -> None:
        wiring = BrowserUseAgent.__wiring__
        assert wiring["agent_loop"] == ("agent_loop", ["react"])

    def test_capability_wiring(self) -> None:
        wiring = BrowserUseAgent.__wiring__
        assert wiring["capability"] == ("capability", ["browser"])

    def test_observation_wiring(self) -> None:
        wiring = BrowserUseAgent.__wiring__
        # Required + optional split for browser_meta
        assert wiring["observation"] == (
            "observation",
            ["dom", "screenshot"],
            ["browser_meta"],
        )


# ── prepare() ─────────────────────────────────────────────────────────


class TestPrepare:
    def test_prepare_seeds_system_prompt_into_memory(self) -> None:
        agent = _make_agent()
        descriptors = [ActionDescriptor(name="click", description="click", params={})]
        agent.prepare(task="navigate to example.com", action_descriptors=descriptors)

        msgs = agent.memory.get_messages()
        assert len(msgs) == 1
        assert isinstance(msgs[0], SystemMessage)
        assert agent._system_prompt_text  # populated
        assert agent._task == "navigate to example.com"
        assert agent._action_descriptors == descriptors

    def test_prepare_clears_existing_memory(self) -> None:
        agent = _make_agent()
        agent.memory.add_message(UserMessage(content="stale"))
        agent.prepare(task="t", action_descriptors=[])
        # Only the new system message should remain.
        msgs = agent.memory.get_messages()
        assert len(msgs) == 1
        assert isinstance(msgs[0], SystemMessage)

    def test_prepare_builds_prompt_snapshot(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        snapshot = agent.system_prompt
        assert snapshot is not None


# ── build_react_prompt() ──────────────────────────────────────────────


class TestBuildReactPrompt:
    def test_returns_messages_after_prepare(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])

        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<html><body>hi</body></html>"),
            browser_meta=_FakeBrowserMeta(
                url="https://example.com", page_title="Example"
            ),
        )
        messages = agent.build_react_prompt(obs)

        assert isinstance(messages, list)
        assert all(isinstance(m, BaseMessage) for m in messages)
        # System + at least one user message
        assert any(isinstance(m, SystemMessage) for m in messages)
        assert any(isinstance(m, UserMessage) for m in messages)

    def test_observation_contains_url_and_dom(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<DOM>tree</DOM>"),
            browser_meta=_FakeBrowserMeta(
                url="https://example.com", page_title="Example"
            ),
        )
        agent.build_react_prompt(obs)
        # Locate the user message that was just added
        user_msgs = [
            m for m in agent.memory.get_messages() if isinstance(m, UserMessage)
        ]
        assert len(user_msgs) == 1
        content = user_msgs[0].content
        assert isinstance(content, str)
        assert "https://example.com" in content
        assert "Example" in content
        assert "<DOM>tree</DOM>" in content

    def test_handles_empty_observation(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation()
        agent.build_react_prompt(obs)
        user_msgs = [
            m for m in agent.memory.get_messages() if isinstance(m, UserMessage)
        ]
        assert len(user_msgs) == 1
        assert user_msgs[0].content == "(empty observation)"

    def test_use_vision_attaches_screenshot_when_present(self) -> None:
        agent = _make_agent(use_vision=True)
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            screenshot=_FakeScreenshot(image_bytes=b"\x89PNG\r\n\x1a\nfake"),
        )
        agent.build_react_prompt(obs)
        user_msgs = [
            m for m in agent.memory.get_messages() if isinstance(m, UserMessage)
        ]
        assert len(user_msgs) == 1
        # Multi-modal content should be a list (text + image)
        content = user_msgs[0].content
        assert isinstance(content, list)
        assert len(content) == 2

    def test_use_vision_no_screenshot_falls_back_to_text(self) -> None:
        agent = _make_agent(use_vision=True)
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation()  # no screenshot
        agent.build_react_prompt(obs)
        user_msgs = [
            m for m in agent.memory.get_messages() if isinstance(m, UserMessage)
        ]
        assert len(user_msgs) == 1
        # Text content (the fallback path)
        assert isinstance(user_msgs[0].content, str)


# ── parse_react_output() ──────────────────────────────────────────────


class TestParseReactOutput:
    def test_translates_click_element_to_canonical_click(self) -> None:
        # C1 regression: browser-use LLM vocabulary uses verbose names
        # (click_element, input_text, go_to_url) that must be aliased
        # to the BrowserCapability's canonical names (click, type,
        # navigate) before reaching execute_action.
        agent = _make_agent()
        out = BrowserUseOutput(
            current_state=BrowserUseStateAssessment(
                evaluation_previous_goal="success",
                memory="mem",
                next_goal="click button",
            ),
            actions=[
                BrowserUseActionItem(action_name="click_element", index=42),
            ],
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 1
        assert isinstance(actions[0], Action)
        assert actions[0].name == "click"
        assert actions[0].params == {"index": 42}

    def test_translates_input_text_and_go_to_url(self) -> None:
        agent = _make_agent()
        out = BrowserUseOutput(
            current_state=BrowserUseStateAssessment(),
            actions=[
                BrowserUseActionItem(
                    action_name="input_text", index=2, text="hello"
                ),
                BrowserUseActionItem(
                    action_name="go_to_url", url="https://x.example"
                ),
            ],
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 2
        assert actions[0].name == "type"
        assert actions[0].params == {"index": 2, "text": "hello"}
        assert actions[1].name == "navigate"
        assert actions[1].params == {"url": "https://x.example"}

    def test_multi_action_output(self) -> None:
        agent = _make_agent()
        out = BrowserUseOutput(
            current_state=BrowserUseStateAssessment(),
            actions=[
                BrowserUseActionItem(action_name="click_element", index=1),
                BrowserUseActionItem(
                    action_name="input_text", index=2, text="hello"
                ),
                BrowserUseActionItem(action_name="go_to_url", url="https://x.example"),
            ],
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 3
        assert actions[0].name == "click"
        assert actions[1].name == "type"
        assert actions[2].name == "navigate"

    def test_empty_actions_raises_parse_error(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        out = BrowserUseOutput(
            current_state=BrowserUseStateAssessment(), actions=[]
        )
        with pytest.raises(ParseError, match="empty actions"):
            agent.parse_react_output(out)

    def test_unparseable_text_raises_parse_error(self) -> None:
        # H1 regression: parse failures used to return a "done" action
        # that silently classified the task as successful. They now
        # raise ParseError so ReactLoop handles retry/termination.
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        with pytest.raises(ParseError, match="Failed to parse"):
            agent.parse_react_output("not valid json at all")

    def test_done_uses_extracted_content_key(self) -> None:
        # C2 regression: the ``done`` action's answer text must be
        # passed under ``extracted_content`` (the key BrowserCapability
        # and DoneEvent read), not ``text``.
        agent = _make_agent()
        out = BrowserUseOutput(
            current_state=BrowserUseStateAssessment(),
            actions=[
                BrowserUseActionItem(
                    action_name="done",
                    text="the final answer",
                    success=True,
                )
            ],
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 1
        assert actions[0].name == "done"
        assert actions[0].params.get("extracted_content") == "the final answer"
        assert "text" not in actions[0].params


class TestOutputModel:
    def test_returns_browser_use_output(self) -> None:
        agent = _make_agent()
        assert agent.get_react_output_model() is BrowserUseOutput

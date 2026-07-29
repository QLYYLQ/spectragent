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

from spectragent.agents._output_models import BrowserUseOutput
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


@dataclass
class _FakeTask:
    user_request: str = "navigate to example.com"
    follow_ups: list[str] | None = None

    def __post_init__(self) -> None:
        if self.follow_ups is None:
            self.follow_ups = []


@dataclass
class _FakeStepInfo:
    step: int = 1
    max_steps: int = 100
    today: str = "2026-05-01"


@dataclass
class _FakeHistory:
    items: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.items is None:
            self.items = []


class _FakeObservation:
    """Light fragment-shaped observation used by tests.

    ``task`` defaults to a non-empty TaskObs-shape because BrowserUseAgent
    requires it (loop-source fragment in __wiring__). Tests opt out of
    step_info / history / browser_meta / etc by passing None.
    """

    def __init__(
        self,
        *,
        dom: _FakeDOM | None = None,
        screenshot: _FakeScreenshot | None = None,
        browser_meta: _FakeBrowserMeta | None = None,
        task: _FakeTask | None = None,
        step_info: _FakeStepInfo | None = None,
        history: _FakeHistory | None = None,
    ) -> None:
        self.dom = dom
        self.screenshot = screenshot
        self.browser_meta = browser_meta
        self.task = task or _FakeTask()
        self.step_info = step_info
        self.history = history


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
        # Required: env-source DOM/screenshot + loop-source task.
        # Optional: browser_meta + loop-source step_info/history.
        assert wiring["observation"] == (
            "observation",
            ["browser.dom", "browser.screenshot", "loop.task"],
            ["browser.browser_meta", "loop.step_info", "loop.history"],
        )


# ── prepare() ─────────────────────────────────────────────────────────


class TestPrepare:
    def test_prepare_returns_system_prompt_seed(self) -> None:
        agent = _make_agent()
        descriptors = [ActionDescriptor(name="click", description="click", params={})]
        seed = agent.prepare(
            task="navigate to example.com", action_descriptors=descriptors
        )

        assert isinstance(seed, list)
        assert len(seed) == 1
        assert isinstance(seed[0], SystemMessage)
        assert agent._system_prompt_text  # populated
        assert not hasattr(agent, "_task")  # task lives on obs.task now
        assert agent._action_descriptors == descriptors
        # prepare is a pure builder — agent memory remains untouched.
        assert agent.memory.get_messages() == []

    def test_prepare_builds_prompt_snapshot(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        snapshot = agent.system_prompt
        assert snapshot is not None


# ── build_react_prompt() ──────────────────────────────────────────────


class TestBuildReactPrompt:
    def test_returns_only_new_step_messages(self) -> None:
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
        # Pure builder: only the new step's user message.
        assert len(messages) == 1
        assert isinstance(messages[0], UserMessage)

    def test_observation_contains_url_and_dom(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<DOM>tree</DOM>"),
            browser_meta=_FakeBrowserMeta(
                url="https://example.com", page_title="Example"
            ),
            task=_FakeTask(user_request="navigate to example.com"),
        )
        messages = agent.build_react_prompt(obs)
        assert len(messages) == 1
        content = messages[0].content
        assert isinstance(content, str)
        assert "https://example.com" in content
        assert "Example" in content
        assert "<DOM>tree</DOM>" in content

    def test_user_request_present_in_user_message(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            task=_FakeTask(user_request="book a flight to Tokyo"),
            browser_meta=_FakeBrowserMeta(url="https://airline.example"),
        )
        messages = agent.build_react_prompt(obs)
        content = messages[0].content
        assert isinstance(content, str)
        assert "<user_request>" in content
        assert "book a flight to Tokyo" in content
        assert messages[0].cache is True

    def test_step_info_block_when_present(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            task=_FakeTask(user_request="x"),
            step_info=_FakeStepInfo(step=4, max_steps=20, today="2026-05-01"),
        )
        content = agent.build_react_prompt(obs)[0].content
        assert isinstance(content, str)
        assert "<step_info>" in content
        assert "step 4/20" in content

    def test_history_renders_step_blocks(self) -> None:
        from spectragent.schemas.action import ActionResult
        from spectragent.schemas.step_record import StepRecord

        rec = StepRecord(
            step=0,
            llm_output="thinking",
            actions=[Action(name="browser.click", params={"index": 1})],
            action_results=[ActionResult(success=True)],
        )
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            task=_FakeTask(user_request="x"),
            history=_FakeHistory(items=[rec]),
        )
        content = agent.build_react_prompt(obs)[0].content
        assert isinstance(content, str)
        assert "<step_1>" in content
        assert "browser.click" in content

    def test_handles_minimal_observation(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        # Only the required task fragment; no DOM / browser_meta / etc.
        obs = _FakeObservation(task=_FakeTask(user_request="hello"))
        messages = agent.build_react_prompt(obs)
        assert len(messages) == 1
        content = messages[0].content
        assert isinstance(content, str)
        assert "<user_request>" in content
        assert "hello" in content
        assert "(empty browser state)" in content

    def test_use_vision_attaches_screenshot_when_present(self) -> None:
        agent = _make_agent(use_vision=True)
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            screenshot=_FakeScreenshot(image_bytes=b"\x89PNG\r\n\x1a\nfake"),
            task=_FakeTask(user_request="vision task"),
        )
        messages = agent.build_react_prompt(obs)
        assert len(messages) == 1
        # Multi-modal content should be a list (text + image)
        content = messages[0].content
        assert isinstance(content, list)
        assert len(content) == 2

    def test_use_vision_no_screenshot_falls_back_to_text(self) -> None:
        agent = _make_agent(use_vision=True)
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(task=_FakeTask(user_request="x"))  # no screenshot
        messages = agent.build_react_prompt(obs)
        assert len(messages) == 1
        # Text content (the fallback path)
        assert isinstance(messages[0].content, str)


# ── parse_react_output() ──────────────────────────────────────────────


class TestParseReactOutput:
    def test_translates_click_element_to_canonical_click(self) -> None:
        # C1 regression: browser-use LLM vocabulary uses verbose names
        # (click_element, input_text, go_to_url) that must be aliased
        # to the BrowserCapability's canonical names (click, type,
        # navigate) before reaching execute_action.
        agent = _make_agent()
        out = BrowserUseOutput(
            evaluation_previous_goal="success",
            memory="mem",
            next_goal="click button",
            action=[{"click_element": {"index": 42}}],
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 1
        assert isinstance(actions[0], Action)
        assert actions[0].name == "click"
        assert actions[0].params == {"index": 42}

    def test_translates_input_text_and_go_to_url(self) -> None:
        agent = _make_agent()
        out = BrowserUseOutput(
            action=[
                {"input_text": {"index": 2, "text": "hello"}},
                {"go_to_url": {"url": "https://x.example"}},
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
            action=[
                {"click_element": {"index": 1}},
                {"input_text": {"index": 2, "text": "hello"}},
                {"go_to_url": {"url": "https://x.example"}},
            ],
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 3
        assert actions[0].name == "click"
        assert actions[1].name == "type"
        assert actions[2].name == "navigate"

    def test_short_verbs_pass_through_unqualified(self) -> None:
        # Upstream prompt sometimes emits the action id directly
        # ("navigate" instead of "go_to_url"). It reaches the guard as
        # written — the parser names no capability, because it has no way
        # to know which one this run holds.
        agent = _make_agent()
        out = BrowserUseOutput(
            action=[
                {"navigate": {"url": "https://example.com"}},
                {"scroll": {"direction": "down"}},
            ],
        )
        actions = agent.parse_react_output(out)
        assert actions[0].name == "navigate"
        assert actions[0].params == {"url": "https://example.com"}
        assert actions[1].name == "scroll"

    def test_parses_raw_json_string_matching_prompt_template(self) -> None:
        # End-to-end regression: when the LLM returns a JSON string
        # exactly matching the prompt template (the upstream
        # browser-use shape), parse_react_output must fall through to
        # JSON load → model_validate → action translation without
        # requiring a pre-built BrowserUseOutput instance.
        import json as _json

        agent = _make_agent()
        payload = _json.dumps(
            {
                "thinking": "I need to navigate first.",
                "evaluation_previous_goal": "n/a",
                "memory": "starting on about:blank",
                "next_goal": "navigate to example.com",
                "action": [
                    {"navigate": {"url": "https://www.example.com"}}
                ],
            }
        )
        actions = agent.parse_react_output(payload)
        assert len(actions) == 1
        assert actions[0].name == "navigate"
        assert actions[0].params == {"url": "https://www.example.com"}

    def test_empty_action_list_raises_parse_error(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        out = BrowserUseOutput(action=[])
        with pytest.raises(ParseError, match="empty 'action' list"):
            agent.parse_react_output(out)

    def test_unparseable_text_raises_parse_error(self) -> None:
        # H1 regression: parse failures used to return a "done" action
        # that silently classified the task as successful. They now
        # raise ParseError so ReactLoop handles retry/termination.
        from spectragent.core.loops.errors import ParseError

        agent = _make_agent()
        with pytest.raises(ParseError, match="not valid JSON"):
            agent.parse_react_output("not valid json at all")

    def test_done_uses_extracted_content_key(self) -> None:
        # C2 regression: the ``done`` action's answer text must be
        # passed under ``extracted_content`` (the key BrowserCapability
        # and DoneEvent read), not ``text``.
        agent = _make_agent()
        out = BrowserUseOutput(
            action=[{"done": {"text": "the final answer", "success": True}}],
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

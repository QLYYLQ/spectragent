"""Tests for the M14 SkyvernAgent ReactLoop slot adaptation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spectragent.agents._output_models import SkyvernActionItem, SkyvernOutput
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.core.llm.messages import (
    BaseMessage,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops import ReactLoop


@dataclass
class _FakeBrowserMeta:
    url: str = ""
    page_title: str = ""
    tabs: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tabs is None:
            self.tabs = []


@dataclass
class _FakeDOM:
    element_tree: Any = None
    selector_map: dict | None = None


@dataclass
class _FakeTask:
    user_request: str = "find the login button"
    follow_ups: list[str] | None = None

    def __post_init__(self) -> None:
        if self.follow_ups is None:
            self.follow_ups = []


@dataclass
class _FakeStepInfo:
    step: int = 1
    max_steps: int = 50
    today: str = "2026-05-01"


class _FakeObservation:
    def __init__(
        self,
        *,
        dom: _FakeDOM | None = None,
        browser_meta: _FakeBrowserMeta | None = None,
        task: _FakeTask | None = None,
        step_info: _FakeStepInfo | None = None,
    ) -> None:
        self.dom = dom
        self.browser_meta = browser_meta
        self.task = task or _FakeTask()
        self.step_info = step_info


def _make_agent(**overrides: Any) -> SkyvernAgent:
    from tests._helpers.assembly import make_agent
    return make_agent(
        SkyvernAgent, memory=SimpleMemory(max_messages=100), **overrides,
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
        assert agent.agent_id == "skyvern"
        assert agent.domain == "web_navigation"


class TestWiring:
    def test_agent_loop_wiring(self) -> None:
        wiring = SkyvernAgent.__wiring__
        assert wiring["agent_loop"] == ("agent_loop", ["react"])

    def test_capability_wiring(self) -> None:
        wiring = SkyvernAgent.__wiring__
        assert wiring["capability"] == ("capability", ["browser"])

    def test_observation_wiring_dom_required_screenshot_optional(self) -> None:
        # Skyvern is element-tree based: DOM is required + loop.task.
        # Screenshot, browser_meta, loop.step_info, loop.history are optional.
        wiring = SkyvernAgent.__wiring__
        assert wiring["observation"] == (
            "observation",
            ["browser.dom", "loop.task"],
            [
                "browser.screenshot",
                "browser.browser_meta",
                "loop.step_info",
                "loop.history",
            ],
        )


class TestPrepare:
    def test_returns_jinja_rendered_system_prompt_seed(self) -> None:
        agent = _make_agent()
        seed = agent.prepare(
            task="Find the cheapest flight", action_descriptors=[]
        )
        assert isinstance(seed, list)
        assert len(seed) == 1
        assert isinstance(seed[0], SystemMessage)
        # The Jinja template renders navigation_goal into the prompt
        assert "Find the cheapest flight" in seed[0].content
        # Pure builder — agent memory remains untouched.
        assert agent.memory.get_messages() == []

    def test_resets_action_history(self) -> None:
        agent = _make_agent()
        agent._action_history.append("stale")
        agent.prepare(task="t", action_descriptors=[])
        assert agent._action_history == []


class TestBuildReactPrompt:
    def test_includes_dom_text(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<element>x</element>"),
            browser_meta=_FakeBrowserMeta(
                url="https://www.example.com", page_title="Ex"
            ),
            task=_FakeTask(user_request="find the cheapest flight"),
        )
        messages = agent.build_react_prompt(obs)
        assert len(messages) == 1
        assert isinstance(messages[0], UserMessage)
        content = messages[0].content
        assert isinstance(content, str)
        assert "https://www.example.com" in content
        assert "<element>x</element>" in content

    def test_user_request_prefix(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<x/>"),
            task=_FakeTask(user_request="reset the user password"),
        )
        content = agent.build_react_prompt(obs)[0].content
        assert isinstance(content, str)
        assert "<user_request>" in content
        assert "reset the user password" in content

    def test_step_info_prefix_when_present(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<x/>"),
            task=_FakeTask(user_request="x"),
            step_info=_FakeStepInfo(step=2, max_steps=10, today="2026-05-01"),
        )
        content = agent.build_react_prompt(obs)[0].content
        assert isinstance(content, str)
        assert "<step_info>step 2/10" in content
        assert "today=2026-05-01" in content

    def test_returns_only_new_user_message(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=[])
        obs = _FakeObservation(dom=_FakeDOM(element_tree="<x/>"))
        messages = agent.build_react_prompt(obs)
        assert isinstance(messages, list)
        assert all(isinstance(m, BaseMessage) for m in messages)
        # Pure builder: agent emits only the new step's user message.
        assert len(messages) == 1
        assert isinstance(messages[0], UserMessage)


class TestParseReactOutput:
    def test_parses_structured_output(self) -> None:
        agent = _make_agent()
        out = SkyvernOutput(
            actions=[
                SkyvernActionItem(
                    action_type="click",
                    element_id="btn-submit",
                    reasoning="submit form",
                ),
            ]
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 1
        assert actions[0].name == "browser.click"
        assert actions[0].params["element_id"] == "btn-submit"
        assert actions[0].params["reasoning"] == "submit form"

    def test_is_done_emits_only_done_action_not_mixed(self) -> None:
        # H2 regression: when ``is_done`` is set, Skyvern previously
        # returned ``[click, done]`` which ReactLoop's default
        # max_actions_per_step=1 would truncate back to ``[click]``,
        # preventing task completion. The fixed behaviour returns
        # ``[done]`` alone so the task terminates correctly.
        agent = _make_agent()
        out = SkyvernOutput(
            actions=[
                SkyvernActionItem(
                    action_type="click",
                    element_id="btn",
                    reasoning="x",
                )
            ],
            is_done=True,
            extracted_content="answer text",
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 1
        assert actions[0].name == "browser.done"
        # C2 regression: ``extracted_content`` is the canonical key
        # (not ``text``).
        assert actions[0].params["extracted_content"] == "answer text"
        assert "text" not in actions[0].params

    def test_action_history_populated_for_prompt_window(self) -> None:
        # M13-Rev: Skyvern's ``input_text`` is translated to the canonical
        # ``type`` action name before it lands in the history window.
        agent = _make_agent()
        out = SkyvernOutput(
            actions=[
                SkyvernActionItem(
                    action_type="input_text",
                    element_id="search",
                    text="hello",
                    reasoning="search",
                )
            ]
        )
        agent.parse_react_output(out)
        assert any(entry.startswith("browser.type(") for entry in agent._action_history)


class TestOutputModel:
    def test_returns_skyvern_output(self) -> None:
        agent = _make_agent()
        assert agent.get_react_output_model() is SkyvernOutput

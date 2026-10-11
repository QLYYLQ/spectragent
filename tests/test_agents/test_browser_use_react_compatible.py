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

import spectragent.core.capabilities.browser.actions  # noqa: F401 — registers them
from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.browser.dom.models import DOMState
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


#: The verbs most of this file needs. Real descriptors, because
#: ``prepare()`` compiles them into the run's closed output schema — a
#: fixture of empty ones would describe a run that can emit nothing.
_DEFAULT_ACTIONS = (
    "browser.click",
    "browser.type",
    "browser.navigate",
    "browser.scroll",
    "browser.done",
)


def _descriptors(*keys: str) -> list[ActionDescriptor]:
    return [ActionRegistrar.get(key)().descriptor() for key in (keys or _DEFAULT_ACTIONS)]


def _make_agent(**overrides: Any) -> BrowserUseAgent:
    from tests._helpers.assembly import make_agent

    return make_agent(
        BrowserUseAgent,
        memory=SimpleMemory(max_messages=100),
        **overrides,
    )


def _prepared(**overrides: Any) -> BrowserUseAgent:
    """An agent that has been through ``prepare()`` with a real action set."""
    agent = _make_agent(**overrides)
    agent.prepare(task="t", action_descriptors=_descriptors())
    return agent


def _reply(*items: dict[str, Any]) -> str:
    import json as _json

    return _json.dumps({"memory": "m", "action": list(items)})


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
        assert wiring["agent_loop"] == ("agent_loop", ["react", "branch"])

    def test_no_capability_entry(self) -> None:
        """Naming a capability constrained codegen and nothing else."""
        assert "capability" not in BrowserUseAgent.__wiring__

    def test_observation_wiring(self) -> None:
        wiring = BrowserUseAgent.__wiring__
        # Required: DOM/screenshot + the loop-source task.
        # Optional: browser_meta + loop-source step_info/history.
        # All capability-relative — which capability's ``dom`` this is
        # gets decided at negotiation.
        # The class declaration is the *superset across modes*: what every
        # mode needs is required, what a mode promotes or excludes is
        # optional. ``dom`` is optional here and promoted to required by
        # both DOM modes — see ``_MODE_DEMAND``.
        assert wiring["observation"] == (
            "observation",
            ["screenshot", "task"],
            ["dom", "browser_meta", "step_info", "history"],
        )

    def test_no_wiring_key_names_a_capability(self) -> None:
        for entry in BrowserUseAgent.__wiring__.values():
            if isinstance(entry, str):
                continue
            for key_list in entry[1:]:
                assert all("." not in k for k in key_list), key_list


# ── prepare() ─────────────────────────────────────────────────────────


class TestPrepare:
    def test_prepare_returns_system_prompt_seed(self) -> None:
        agent = _make_agent()
        descriptors = _descriptors("browser.click")
        seed = agent.prepare(task="navigate to example.com", action_descriptors=descriptors)

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
        agent.prepare(task="t", action_descriptors=_descriptors())
        snapshot = agent.system_prompt
        assert snapshot is not None


# ── build_react_prompt() ──────────────────────────────────────────────


class TestBuildReactPrompt:
    def test_returns_only_new_step_messages(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=_descriptors())

        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<html><body>hi</body></html>"),
            browser_meta=_FakeBrowserMeta(url="https://example.com", page_title="Example"),
        )
        messages = agent.build_react_prompt(obs)

        assert isinstance(messages, list)
        assert all(isinstance(m, BaseMessage) for m in messages)
        # Pure builder: only the new step's user message.
        assert len(messages) == 1
        assert isinstance(messages[0], UserMessage)

    def test_observation_contains_url_and_dom(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=_descriptors())
        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<DOM>tree</DOM>"),
            browser_meta=_FakeBrowserMeta(url="https://example.com", page_title="Example"),
            task=_FakeTask(user_request="navigate to example.com"),
        )
        messages = agent.build_react_prompt(obs)
        assert len(messages) == 1
        content = messages[0].content
        assert isinstance(content, str)
        assert "https://example.com" in content
        assert "Example" in content
        assert "<DOM>tree</DOM>" in content

    def test_dom_text_reaches_the_model_after_the_old_agent_cutoff(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=_descriptors())
        tail = "[173]<button>Reachable at the end</button>"
        state = DOMState(serialized_dom="x" * 10_100 + tail)
        content = agent.build_react_prompt(_FakeObservation(dom=_FakeDOM(element_tree=state)))[
            0
        ].content

        assert isinstance(content, str)
        assert tail in content
        assert "... (truncated)" not in content

    def test_user_request_present_in_user_message(self) -> None:
        agent = _make_agent()
        agent.prepare(task="t", action_descriptors=_descriptors())
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
        agent.prepare(task="t", action_descriptors=_descriptors())
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
        agent.prepare(task="t", action_descriptors=_descriptors())
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
        agent.prepare(task="t", action_descriptors=_descriptors())
        # Only the required task fragment; no DOM / browser_meta / etc.
        obs = _FakeObservation(task=_FakeTask(user_request="hello"))
        messages = agent.build_react_prompt(obs)
        assert len(messages) == 1
        content = messages[0].content
        assert isinstance(content, str)
        assert "<user_request>" in content
        assert "hello" in content
        assert "(empty browser state)" in content

    def test_a_vision_mode_attaches_the_screenshot_when_present(self) -> None:
        agent = _make_agent(observation_mode="dom_vision")
        agent.prepare(task="t", action_descriptors=_descriptors())
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

    def test_a_vision_mode_with_no_screenshot_falls_back_to_text(self) -> None:
        agent = _make_agent(observation_mode="dom_vision")
        agent.prepare(task="t", action_descriptors=_descriptors())
        obs = _FakeObservation(task=_FakeTask(user_request="x"))  # no screenshot
        messages = agent.build_react_prompt(obs)
        assert len(messages) == 1
        # Text content (the fallback path)
        assert isinstance(messages[0].content, str)


# ── parse_react_output() ──────────────────────────────────────────────


class TestParseReactOutput:
    """One spelling per verb: the fully-qualified key, and nothing else.

    The three upstream aliases (``click_element`` / ``input_text`` /
    ``go_to_url``) and the bare local ids are gone. No template ever taught
    an alias, and the local id made the run carry two names for one action
    — one advertised, one routable — with ``build_guarded_act`` silently
    reconciling them after the fact.
    """

    def test_reads_a_validated_instance(self) -> None:
        agent = _prepared()
        out = agent.get_react_output_model().model_validate(
            {
                "memory": "mem",
                "action": [{"browser.click": {"index": 42}}],
            }
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 1
        assert isinstance(actions[0], Action)
        assert actions[0].name == "browser.click"
        assert actions[0].params == {"index": 42}

    def test_multi_action_output(self) -> None:
        agent = _prepared()
        actions = agent.parse_react_output(
            _reply(
                {"browser.click": {"index": 1}},
                {"browser.type": {"index": 2, "text": "hello"}},
                {"browser.navigate": {"url": "https://x.example"}},
            )
        )
        assert [a.name for a in actions] == [
            "browser.click",
            "browser.type",
            "browser.navigate",
        ]
        assert actions[1].params == {"index": 2, "text": "hello"}

    def test_omitted_optional_params_are_not_manufactured(self) -> None:
        """``clear: null`` used to arrive as ``False`` and override the knob.

        ``TypeAction.clear_by_default`` is capability-side config; a model
        that says nothing about ``clear`` must leave it alone, and a model
        that writes ``null`` is saying exactly that.
        """
        agent = _prepared()
        (action,) = agent.parse_react_output(
            _reply({"browser.type": {"index": 2, "text": "x", "clear": None}})
        )
        assert "clear" not in action.params

    def test_parses_raw_json_string_matching_prompt_template(self) -> None:
        import json as _json

        agent = _prepared(flash_mode=False, use_thinking=True)
        payload = _json.dumps(
            {
                "thinking": "I need to navigate first.",
                "evaluation_previous_goal": "n/a",
                "memory": "starting on about:blank",
                "next_goal": "navigate to example.com",
                "action": [{"browser.navigate": {"url": "https://www.example.com"}}],
            }
        )
        actions = agent.parse_react_output(payload)
        assert len(actions) == 1
        assert actions[0].name == "browser.navigate"
        assert actions[0].params == {"url": "https://www.example.com"}

    def test_empty_action_list_raises_parse_error(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _prepared()
        with pytest.raises(ParseError):
            agent.parse_react_output(_reply())

    def test_unparseable_text_raises_parse_error(self) -> None:
        # H1 regression: parse failures used to return a "done" action
        # that silently classified the task as successful. They now
        # raise ParseError so ReactLoop handles retry/termination.
        from spectragent.core.loops.errors import ParseError

        agent = _prepared()
        with pytest.raises(ParseError, match="not valid JSON"):
            agent.parse_react_output("not valid json at all")

    def test_done_uses_extracted_content_key(self) -> None:
        """No rename on the way through: the action names its own field.

        ``done.text`` → ``extracted_content`` used to happen in the parser,
        so the prompt taught one name, the schema permitted it, and the
        executor read another.
        """
        agent = _prepared()
        (action,) = agent.parse_react_output(
            _reply({"browser.done": {"extracted_content": "the final answer", "success": True}})
        )
        assert action.name == "browser.done"
        assert action.params.get("extracted_content") == "the final answer"
        assert "text" not in action.params

    def test_the_old_done_text_spelling_is_refused(self) -> None:
        from spectragent.core.loops.errors import ParseError

        agent = _prepared()
        with pytest.raises(ParseError):
            agent.parse_react_output(_reply({"browser.done": {"text": "answer"}}))


class TestOutputModel:
    def test_returns_this_run_s_closed_model(self) -> None:
        agent = _prepared()
        model = agent.get_react_output_model()
        assert model.__name__ == "BrowserUseOutputForRun"
        # Built per run: a second prepare with a different verb set gives a
        # different model, which is the point of building it from the
        # negotiated descriptors rather than from the class.
        agent.prepare(task="t", action_descriptors=_descriptors("browser.done"))
        assert agent.get_react_output_model() is not model


class TestFrameElementIndices:
    """An element inside an out-of-process frame has a ten-digit index.

    ``dom.models.element_handle`` namespaces each frame target above
    ``2**31``, and that key is the ``[index]`` the model copies back. The
    agent must hand ClickAction the same integer, unchanged, whichever path
    the reply takes.
    """

    KEY = 2 * 2**31 + 58

    async def _click(self, action: Action) -> tuple[Any, Any]:
        from spectragent.core.capabilities.browser.actions.click import ClickAction
        from tests.test_core.test_capabilities.test_browser_actions.conftest import (
            FakeCapability,
            FakeElementNode,
            FakeScope,
        )

        capability = FakeCapability()
        capability._scopes["tab"] = FakeScope()
        capability._selector_maps["tab"] = {
            self.KEY: FakeElementNode(backend_node_id=58, session_id="frame")
        }
        capability._default_session_id = "tab"
        result = await ClickAction(post_delay_ms=0).execute(action.params, capability=capability)  # type: ignore[arg-type]
        (event,) = capability._scopes["tab"].emitted
        return result, event

    async def test_through_the_text_path(self) -> None:
        agent = _prepared()
        (action,) = agent.parse_react_output(_reply({"browser.click": {"index": self.KEY}}))
        assert action.params == {"index": self.KEY}
        assert type(action.params["index"]) is int

        result, event = await self._click(action)
        assert result.success, result.error
        assert (event.backend_node_id, event.session_id) == (58, "frame")

    async def test_through_the_structured_path(self) -> None:
        agent = _prepared()
        out = agent.get_react_output_model().model_validate_json(
            f'{{"memory": "", "action": [{{"browser.click": {{"index": {self.KEY}}}}}]}}'
        )
        (action,) = agent.parse_react_output(out)
        result, event = await self._click(action)
        assert result.success, result.error
        assert event.backend_node_id == 58

"""Tests for the M14 SkyvernAgent ReactLoop slot adaptation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.skyvern.output_models import SkyvernActionItem, SkyvernOutput
from spectragent.core.capabilities.browser.dom.models import DOMState
from spectragent.core.llm.messages import (
    BaseMessage,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops import ReactLoop
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation.fragments import HistoryObs
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.step_record import StepRecord
from tests._helpers.action_spaces import space_of, wired_space
from tests._helpers.fragment_formats import dom_formats

#: Stand-in for a negotiated action set. Deliberately *not* empty: the
#: old suite passed an empty action list everywhere, which is why a
#: prompt that dropped the action space on the floor stayed green.
_DESCRIPTORS = [
    ActionDescriptor(
        name="browser.click",
        description="Click a DOM element by its index in the selector map.",
        params={"index": "int -- element index from selector_map"},
    ),
    ActionDescriptor(
        name="browser.type",
        description="Type text into a DOM element.",
        params={"index": "int -- element index", "text": "str"},
    ),
]


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
        history: list[StepRecord] | None = None,
    ) -> None:
        self.dom = dom
        self.browser_meta = browser_meta
        self.task = task or _FakeTask()
        self.platform = SimpleNamespace(operating_system="macos", fragment_id="browser.platform")
        self.step_info = step_info
        self.history = HistoryObs(items=list(history or []))


def _clicks(*indexes: int) -> list[StepRecord]:
    """One executed ``browser.click`` step per index, each with an ok result."""
    return [
        StepRecord(
            step=step,
            actions=[Action(name="browser.click", params={"index": index, "reasoning": "r"})],
            action_results=[ActionResult(success=True)],
        )
        for step, index in enumerate(indexes)
    ]


def _make_agent(**overrides: Any) -> SkyvernAgent:
    """An agent prepared with every action its wiring names, as a browser run gives it."""
    from tests._helpers.assembly import make_agent

    agent = make_agent(
        SkyvernAgent,
        memory=SimpleMemory(max_messages=100),
        **overrides,
    )
    agent.prepare(
        task="t",
        action_space=wired_space(SkyvernAgent),
        execution_limits=ExecutionLimits(max_actions_per_step=5),
        fragment_formats=dom_formats(),
    )
    return agent


class TestSlotConformance:
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
        assert agent.agent_id == "skyvern"
        assert agent.domain == "web_navigation"


class TestWiring:
    def test_agent_loop_wiring(self) -> None:
        wiring = SkyvernAgent.__wiring__
        assert wiring["agent_loop"] == ("agent_loop", ["react", "branch"])

    def test_no_capability_entry(self) -> None:
        """Naming a capability constrained codegen and nothing else."""
        assert "capability" not in SkyvernAgent.__wiring__

    def test_observation_wiring_dom_required_screenshot_optional(self) -> None:
        # Skyvern is element-tree based: DOM is required + the task. As a
        # browser agent it requires the page state (``browser_meta``), and
        # its bounded action-history window is rendered from the Loop's
        # ``history`` (both were otherwise before the 2026-10-20 layering
        # ruling: browser_meta optional, the window agent-owned state).
        # Screenshot, step_info and the platform-neutral capture_state are
        # optional.
        wiring = SkyvernAgent.__wiring__
        assert wiring["observation"] == {
            "registry": "observation",
            "keys": ["dom", "platform", "task", "browser_meta", "history"],
            "optional_keys": [
                "screenshot",
                "step_info",
                "capture_state",
            ],
            "declare_only": True,
        }

    def test_no_wiring_key_names_a_capability(self) -> None:
        for entry in SkyvernAgent.__wiring__.values():
            if isinstance(entry, str):
                continue
            for key_list in (
                [entry["keys"], entry.get("optional_keys", [])]
                if isinstance(entry, dict)
                else entry[1:]
            ):
                assert all("." not in k for k in key_list), key_list


class TestPrepare:
    def test_returns_jinja_rendered_system_prompt_seed(self) -> None:
        agent = _make_agent()
        seed = agent.prepare(
            task="Find the cheapest flight",
            action_space=space_of(_DESCRIPTORS),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        assert isinstance(seed, list)
        assert len(seed) == 1
        assert isinstance(seed[0], SystemMessage)
        # The Jinja template renders navigation_goal into the prompt
        assert "Find the cheapest flight" in seed[0].content
        # Pure builder — agent memory remains untouched.
        assert agent.memory.get_messages() == []

    def test_renders_the_negotiated_action_set(self) -> None:
        """The whole point of the action-space slot.

        The old template referenced eight variables the agent never
        supplied and ignored ``action_space`` entirely, so the negotiated
        action set never reached the model. Passing descriptors and
        finding nothing is the regression.
        """
        agent = _make_agent()
        seed = agent.prepare(
            task="t",
            action_space=space_of(_DESCRIPTORS),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        text = seed[0].content
        assert isinstance(text, str)
        for descriptor in _DESCRIPTORS:
            assert descriptor.name in text
            assert descriptor.description in text

    def test_renders_the_loops_action_budget(self) -> None:
        agent = _make_agent()
        seed = agent.prepare(
            task="t",
            action_space=space_of(_DESCRIPTORS),
            execution_limits=ExecutionLimits(max_actions_per_step=4),
            fragment_formats=dom_formats(),
        )
        text = seed[0].content
        assert isinstance(text, str)
        assert {int(n) for n in re.findall(r"maximum of (\d+) actions", text)} == {4}

    def test_optional_config_blocks_render_only_when_set(self) -> None:
        """Every remaining knob is consumed by the template."""
        off = (
            _make_agent()
            .prepare(
                task="t",
                action_space=space_of([]),
                execution_limits=ExecutionLimits(max_actions_per_step=1),
                fragment_formats=dom_formats(),
            )[0]
            .content
        )
        assert isinstance(off, str)
        assert "PAY ONLY WITH THE SAVED CARD" not in off
        assert "GIVE UP AFTER A PAYWALL" not in off
        assert "verification code" not in off

        on = (
            _make_agent(
                complete_criterion="PAY ONLY WITH THE SAVED CARD",
                terminate_criterion="GIVE UP AFTER A PAYWALL",
                verification_code_check=True,
                navigation_payload="username=bob",
            )
            .prepare(
                task="t",
                action_space=space_of([]),
                execution_limits=ExecutionLimits(max_actions_per_step=1),
                fragment_formats=dom_formats(),
            )[0]
            .content
        )
        assert isinstance(on, str)
        assert "PAY ONLY WITH THE SAVED CARD" in on
        assert "GIVE UP AFTER A PAYWALL" in on
        assert "verification code" in on
        assert "username=bob" in on

    async def test_a_parsed_reply_never_enters_the_window(self) -> None:
        """The window is the Loop's record of what ran, not what was proposed.

        It used to be agent-owned state appended at parse time, so an
        action the Loop cut or never reached was shown as history. Now a
        parsed reply leaves no trace in the agent: the next prompt shows
        only what the ``history`` fragment says ran.
        """
        agent = _make_agent()
        agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        agent.parse_react_output(
            SkyvernOutput(actions=[SkyvernActionItem(action_type="browser.click", index=7)])
        )
        (message,) = await agent.build_react_prompt(
            _FakeObservation(), context=ReactPromptContext(step=1, phase="policy")
        )
        assert "<action_history>\n(no actions yet)\n</action_history>" in message.text
        assert "browser.click(" not in message.text


class TestBuildReactPrompt:
    async def test_includes_dom_text(self) -> None:
        """A real ``DOMState``, not a string.

        ``DOMObs.element_tree`` carries a ``DOMState``; the form the model
        is meant to read is its ``serialized_dom``. The old fixture passed
        a plain ``str``, which made ``str(element_tree)`` a no-op and hid
        the fact that a real run got a dataclass repr instead of the
        indexed DOM the model needs.
        """
        agent = _make_agent()
        agent.prepare(
            task="t",
            action_space=space_of(_DESCRIPTORS),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        state = DOMState(
            serialized_dom="[1]<button>Submit</button>\n[2]<input placeholder='q'/>",
        )
        obs = _FakeObservation(
            dom=_FakeDOM(element_tree=state),
            browser_meta=_FakeBrowserMeta(url="https://www.example.com", page_title="Ex"),
            task=_FakeTask(user_request="find the cheapest flight"),
        )
        messages = await agent.build_react_prompt(
            obs, context=ReactPromptContext(step=0, phase="policy")
        )
        assert len(messages) == 1
        assert isinstance(messages[0], UserMessage)
        content = messages[0].content
        assert isinstance(content, str)
        assert "https://www.example.com" in content
        assert "[1]<button>Submit</button>" in content
        assert "[2]<input placeholder='q'/>" in content
        # The dataclass repr must not be what the model reads.
        assert "DOMElementNode(" not in content
        assert "serialized_dom=" not in content

    async def test_dom_text_reaches_the_model_after_the_old_agent_cutoff(self) -> None:
        agent = _make_agent()
        agent.prepare(
            task="t",
            action_space=space_of(_DESCRIPTORS),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        tail = "[173]<button>Reachable at the end</button>"
        state = DOMState(serialized_dom="x" * 10_100 + tail)
        content = (
            await agent.build_react_prompt(
                _FakeObservation(dom=_FakeDOM(element_tree=state)),
                context=ReactPromptContext(step=0, phase="policy"),
            )
        )[0].content

        assert isinstance(content, str)
        assert tail in content
        assert "... (truncated)" not in content

    async def test_action_history_is_rendered_per_step_not_in_the_system_prompt(
        self,
    ) -> None:
        """The window follows the step, because it changes every step.

        ``prepare()`` is a pure builder called once per run, so a history
        window rendered into the system prompt is frozen at "empty"
        forever. It belongs next to ``<user_request>`` / ``<step_info>``.
        """
        agent = _make_agent(action_history_window=2)
        seed = agent.prepare(
            task="t",
            action_space=space_of(_DESCRIPTORS),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        assert isinstance(seed[0].content, str)
        assert "<action_history>" not in seed[0].content

        obs = _FakeObservation(dom=_FakeDOM(element_tree="<x/>"))
        first = (
            await agent.build_react_prompt(obs, context=ReactPromptContext(step=0, phase="policy"))
        )[0].content
        assert isinstance(first, str)
        assert "<action_history>\n(no actions yet)\n</action_history>" in first

        later = (
            await agent.build_react_prompt(
                _FakeObservation(dom=_FakeDOM(element_tree="<x/>"), history=_clicks(0, 1, 2)),
                context=ReactPromptContext(step=3, phase="policy"),
            )
        )[0].content
        assert isinstance(later, str)
        history = later.split("<action_history>\n")[1].split("\n</action_history>")[0]
        # window=2 → the two most recent actions, oldest dropped.
        assert history.count("browser.click(") == 2
        assert "'index': 0" not in history
        assert "'index': 1" in history
        assert "'index': 2" in history

    async def test_a_zero_window_renders_nothing_not_everything(self) -> None:
        """``lst[-0:]`` is ``lst[0:]``.

        Zero is the obvious YAML spelling of "no history", and the naive
        slice turns it into "the whole run" — the opposite of what was
        asked for, and unbounded prompt growth.
        """
        agent = _make_agent(action_history_window=0)
        agent.prepare(
            task="t",
            action_space=space_of(_DESCRIPTORS),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        content = (
            await agent.build_react_prompt(
                _FakeObservation(dom=_FakeDOM(element_tree="<x/>"), history=_clicks(0, 1, 2)),
                context=ReactPromptContext(step=3, phase="policy"),
            )
        )[0].content
        assert isinstance(content, str)
        assert "<action_history>\n(disabled)\n</action_history>" in content
        assert "browser.click(" not in content

    async def test_user_request_prefix(self) -> None:
        agent = _make_agent()
        agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<x/>"),
            task=_FakeTask(user_request="reset the user password"),
        )
        content = (
            await agent.build_react_prompt(obs, context=ReactPromptContext(step=0, phase="policy"))
        )[0].content
        assert isinstance(content, str)
        assert "<user_request>" in content
        assert "reset the user password" in content

    async def test_step_info_prefix_when_present(self) -> None:
        agent = _make_agent()
        agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        obs = _FakeObservation(
            dom=_FakeDOM(element_tree="<x/>"),
            task=_FakeTask(user_request="x"),
            step_info=_FakeStepInfo(step=2, max_steps=10, today="2026-05-01"),
        )
        content = (
            await agent.build_react_prompt(obs, context=ReactPromptContext(step=0, phase="policy"))
        )[0].content
        assert isinstance(content, str)
        assert "<step_info>step 2/10" in content
        assert "today=2026-05-01" in content

    async def test_returns_only_new_user_message(self) -> None:
        agent = _make_agent()
        agent.prepare(
            task="t",
            action_space=space_of([]),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        obs = _FakeObservation(dom=_FakeDOM(element_tree="<x/>"))
        messages = await agent.build_react_prompt(
            obs, context=ReactPromptContext(step=0, phase="policy")
        )
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
                    action_type="browser.click",
                    index=7,
                    reasoning="submit form",
                ),
            ]
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 1
        assert actions[0].name == "browser.click"
        # ``index``, not ``element_id``: BrowserCapability's selector_map
        # is index-keyed and ``ClickAction.execute`` reads params["index"].
        assert actions[0].params["index"] == 7
        assert "element_id" not in actions[0].params
        # The reasoning is the model's thought about the click, not a param of
        # it: it stays in the reply (``parse_react_thought`` reads it), and the
        # action carries only what ``ClickAction.Params`` declares.
        assert actions[0].params == {"index": 7}

    def test_carries_every_param_the_advertised_actions_need(self) -> None:
        """scroll needs ``direction``, send_keys needs ``keys``.

        Both are in ``__wiring__``'s optional set, so the prompt
        advertises them; an output model that cannot express their
        required params turns an advertised action into a guaranteed
        ``Missing param`` at execute time.
        """
        agent = _make_agent()
        actions = agent.parse_react_output(
            SkyvernOutput(
                actions=[
                    SkyvernActionItem(action_type="browser.scroll", direction="up", reasoning="r"),
                    SkyvernActionItem(action_type="browser.send_keys", keys="Enter", reasoning="r"),
                    SkyvernActionItem(
                        action_type="browser.type",
                        index=3,
                        text="hello",
                        reasoning="r",
                    ),
                ]
            )
        )
        assert actions[0].params["direction"] == "up"
        assert actions[1].params["keys"] == "Enter"
        assert actions[2].params == {"index": 3, "text": "hello"}

    def test_done_inside_actions_keeps_the_answer(self) -> None:
        """The template says done is a top-level flag; models improvise.

        ``browser.done`` is in the negotiated set, so ``guarded_act``
        accepts it and the run ends either way — the risk is ending with
        an empty answer.
        """
        agent = _make_agent()
        actions = agent.parse_react_output(
            SkyvernOutput(
                actions=[
                    SkyvernActionItem(
                        action_type="browser.done",
                        text="42 dollars",
                        reasoning="found it",
                    )
                ],
                success=False,
            )
        )
        assert len(actions) == 1
        assert actions[0].name == "done"
        assert actions[0].params["extracted_content"] == "42 dollars"
        assert actions[0].params["success"] is False

    def test_is_done_with_success_false_reports_failure(self) -> None:
        agent = _make_agent()
        actions = agent.parse_react_output(
            SkyvernOutput(
                actions=[],
                is_done=True,
                success=False,
                extracted_content="hit a paywall",
            )
        )
        assert actions[0].name == "done"
        assert actions[0].params["success"] is False
        assert actions[0].params["extracted_content"] == "hit a paywall"

    def test_is_done_without_success_claims_nothing(self) -> None:
        """A reply that leaves ``success`` out makes no claim; it is not read as ``true``."""
        agent = _make_agent()
        actions = agent.parse_react_output(
            SkyvernOutput.model_validate(
                {"actions": [], "is_done": True, "extracted_content": "the answer"}
            )
        )
        assert actions[0].params == {"extracted_content": "the answer"}

    def test_is_done_emits_only_done_action_not_mixed(self) -> None:
        # Regression: when ``is_done`` is set, Skyvern previously
        # returned ``[click, done]`` which ReactLoop's default
        # max_actions_per_step=1 would truncate back to ``[click]``,
        # preventing task completion. The fixed behaviour returns
        # ``[done]`` alone so the task terminates correctly.
        agent = _make_agent()
        out = SkyvernOutput(
            actions=[
                SkyvernActionItem(
                    action_type="browser.click",
                    index=1,
                    reasoning="x",
                )
            ],
            is_done=True,
            extracted_content="answer text",
        )
        actions = agent.parse_react_output(out)
        assert len(actions) == 1
        assert actions[0].name == "done"
        # Regression: ``extracted_content`` is the canonical key
        # (not ``text``).
        assert actions[0].params["extracted_content"] == "answer text"
        assert "text" not in actions[0].params

    async def test_an_executed_action_enters_the_window_with_its_result(self) -> None:
        agent = _make_agent()
        typed = StepRecord(
            step=0,
            actions=[Action(name="browser.type", params={"index": 4, "text": "hello"})],
            action_results=[ActionResult(success=True)],
        )
        (message,) = await agent.build_react_prompt(
            _FakeObservation(history=[typed]), context=ReactPromptContext(step=1, phase="policy")
        )
        assert "browser.type({'index': 4, 'text': 'hello'}) -> ok" in message.text

    def test_a_bare_name_is_left_for_the_guard_to_bind(self) -> None:
        """The agent must not invent a provider for a bare verb.

        The template teaches the negotiated (fully-qualified) names, but a
        model that answers ``click`` gets that string forwarded verbatim:
        ``build_guarded_act`` holds this run's short → fully-qualified
        map, and it is the only component that can know whether ``click``
        means ``browser.click`` or ``desktop.click``.
        """
        agent = _make_agent()
        actions = agent.parse_react_output(
            SkyvernOutput(actions=[SkyvernActionItem(action_type="click", index=1, reasoning="r")])
        )
        assert actions[0].name == "click"


class TestOutputModel:
    def test_returns_skyvern_output(self) -> None:
        agent = _make_agent()
        assert agent.get_react_output_model() is SkyvernOutput

    def test_the_shape_survives_a_strict_provider_schema(self) -> None:
        """Skyvern's whole advantage is a closed, provider-enforceable shape.

        ``SkyvernOutput`` is flat scalars all the way down, so the
        OpenAI-style ``strict: true`` path can constrain it for real —
        unlike an open ``{"<verb>": {...}}`` map, which JSON Schema cannot
        narrow. Widening the model with a free-form object would silently
        drop it back to "the provider validates nothing".
        """
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        schema = SchemaOptimizer.create_optimized_json_schema(SkyvernOutput, strict=True)
        assert SchemaOptimizer.is_strict_compatible(schema)
        item = schema["properties"]["actions"]["items"]
        assert item["additionalProperties"] is False
        assert set(item["required"]) == set(SkyvernActionItem.model_fields)


class TestFrameElementIndices:
    """A ten-digit frame-element index survives the closed Skyvern shape."""

    async def test_the_index_reaches_the_element_unchanged(self) -> None:
        from spectragent.core.capabilities.browser.actions.click import ClickAction
        from tests.test_core.test_capabilities.test_browser_actions.conftest import (
            FakeCapability,
            FakeElementNode,
            FakeScope,
        )

        key = 3 * 2**31 + 7
        out = SkyvernOutput.model_validate_json(
            f'{{"actions": [{{"action_type": "browser.click", "index": {key}, "reasoning": "r"}}]}}'
        )
        (action,) = _make_agent().parse_react_output(out)
        assert action.params["index"] == key

        capability = FakeCapability()
        capability._scopes["tab"] = FakeScope()
        capability._selector_maps["tab"] = {
            key: FakeElementNode(backend_node_id=7, session_id="frame")
        }
        capability._default_session_id = "tab"
        result = await ClickAction(post_delay_ms=0).execute(action.params, capability=capability)  # type: ignore[arg-type]
        assert result.success, result.error
        (event,) = capability._scopes["tab"].emitted
        assert (event.backend_node_id, event.session_id) == (7, "frame")

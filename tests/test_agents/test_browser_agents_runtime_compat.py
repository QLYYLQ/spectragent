"""End-to-end compatibility tests for the M14 browser agents.

Verifies that all three adapted agents:

- Pass ``ReactLoop.validate_slots`` for each browser Agent
- Have the right observation negotiation behaviour (UITars must NOT
  trigger DOM resolution)
- Successfully complete one mocked observe → think → act step driven
  by ReactLoop with a stub LLM and a stub Capability
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    BrowserPlatformObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.loops.identity import RunIdentity
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import extract_wiring, negotiate_action
from spectragent.core.negotiation.observation import _observation_rules
from spectragent.schemas.action import ActionDescriptor, ActionResult
from spectragent.schemas.observation.fragments import (
    HistoryObs,
    StepInfoObs,
    TaskObs,
)
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.action_params import minimal_params
from tests._helpers.assembly import make_agent

# ── Validate compatibility ────────────────────────────────────────────


class TestValidateCompatibility:
    def test_browser_use_agent_compatible_with_react_loop(self) -> None:
        agent = make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=10))
        loop = ReactLoop()
        # Should not raise.
        loop.validate_slots(agent)

    def test_skyvern_agent_compatible_with_react_loop(self) -> None:
        agent = make_agent(SkyvernAgent, memory=SimpleMemory(max_messages=10))
        loop = ReactLoop()
        loop.validate_slots(agent)

    def test_ui_tars_agent_compatible_with_react_loop(self) -> None:
        agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
        loop = ReactLoop()
        loop.validate_slots(agent)


# ── Observation negotiation ───────────────────────────────────────────


class TestObservationNegotiation:
    """The most important M14 verification: UITars (vision-only) must NOT
    activate the DOM fragment, even if the environment provides it."""

    def test_ui_tars_does_not_activate_dom(self) -> None:
        # Env provides everything (browser capability default).
        env_provides = {
            "browser.platform",
            "browser.dom",
            "browser.screenshot",
            "browser.browser_meta",
        }

        result = _observation_rules(
            agent=extract_wiring(UITarsAgent, "observation"),
            loop=extract_wiring(ReactLoop, "observation"),
            env_provides=env_provides,
        )

        assert "browser.screenshot" in result.active_fragments
        assert "browser.dom" not in result.active_fragments, (
            "UITars must not trigger DOM resolution — wiring requires only 'browser.screenshot'"
        )
        assert "browser.browser_meta" not in result.active_fragments

    def test_browser_use_activates_dom_screenshot_and_meta(self) -> None:
        env_provides = {
            "browser.platform",
            "browser.dom",
            "browser.screenshot",
            "browser.browser_meta",
        }
        result = _observation_rules(
            agent=extract_wiring(BrowserUseAgent, "observation"),
            loop=extract_wiring(ReactLoop, "observation"),
            env_provides=env_provides,
        )
        # dom + screenshot are required, browser_meta is optional but
        # the env provides it so it should be active too.
        assert {
            "browser.dom",
            "browser.screenshot",
            "browser.browser_meta",
        } <= result.active_fragments

    def test_skyvern_activates_dom_and_drops_unused_optionals(self) -> None:
        # When the env only provides dom (no screenshot/meta), Skyvern's
        # required dom is satisfied and the optionals are simply absent.
        result = _observation_rules(
            agent=extract_wiring(SkyvernAgent, "observation"),
            loop=extract_wiring(ReactLoop, "observation"),
            env_provides={"browser.dom", "browser.platform"},
        )
        assert "browser.dom" in result.active_fragments
        assert "browser.screenshot" not in result.active_fragments

    def test_ui_tars_fails_when_env_lacks_screenshot(self) -> None:
        # Required fragment missing → negotiation must raise.
        from spectragent.core.negotiation import IncompatibleFragmentError

        with pytest.raises(IncompatibleFragmentError):
            _observation_rules(
                agent=extract_wiring(UITarsAgent, "observation"),
                loop=extract_wiring(ReactLoop, "observation"),
                env_provides={"browser.dom"},
            )


# ── End-to-end ReactLoop drive with mocks ────────────────────────────


@dataclass
class _MockCompletion:
    completion: Any


class _MockLLM:
    """Stub LLM that returns a canned completion regardless of input."""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def ainvoke(self, messages: Any, output_format: Any = None) -> _MockCompletion:
        return _MockCompletion(completion=self._response)

    name = "test-model"

    async def aclose(self, *, timeout=5.0):
        return ()


class _MockEnv:
    """Stub Environment that returns a canned observation and records actions."""

    def __init__(self, observation: Any) -> None:
        self._observation = observation
        self.acted: list[Any] = []
        self._is_alive = True

    @property
    def provided_fragments(self) -> frozenset[str]:
        return frozenset({"dom", "screenshot", "browser_meta"})

    def get_action_space(self, **_: Any) -> list:
        return []

    def has_capability(self, _: str) -> bool:
        return True

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    @property
    def is_alive(self) -> bool:
        return self._is_alive

    async def observe(
        self, *args: Any, **kwargs: Any
    ) -> Any:  # accepts (negotiated, session_id) or (session_id,)
        return self._observation

    async def act(self, action: Any, *args: Any, **kwargs: Any) -> Any:
        from spectragent.schemas.action import ActionResult

        self.acted.append(action)
        # First action's result triggers done.
        is_done = action.name == "browser.done"
        return ActionResult(
            success=True,
            is_done=is_done,
            extracted_content=action.params.get("extracted_content", ""),
        )

    async def setup(self, **_: Any) -> None:
        return

    async def teardown(self) -> None:
        self._is_alive = False


class _MockNegotiated:
    """Minimal negotiation-result stand-in.

    ``loop_sourced`` is empty here because the canned observation is a
    plain object (not a Pydantic model) and ``populate_loop_fragments``
    expects ``model_copy``. Tests that need the loop-fragment population
    path live in tests/test_core/test_loops/test_react_loop_fragments.py.
    """

    active_fragments = {"dom", "screenshot", "browser_meta", "loop.task"}
    env_sourced = {"dom", "screenshot", "browser_meta"}
    loop_sourced: set[str] = set()


class _ActionsOnlyEnv:
    """Environment stand-in that advertises one capability's action set."""

    def __init__(self, capability: Any) -> None:
        self.provided_actions = capability.provided_actions


async def _resolve(agent: Any, action: Any, capability: Any) -> str:
    """What ``build_guarded_act`` would send to ``env.act`` for *action*."""
    negotiated = negotiate_action(agent, _ActionsOnlyEnv(capability))
    descriptors = [
        ActionDescriptor(name=name, description="") for name in sorted(negotiated.active_actions)
    ]
    routed: list[Any] = []

    async def _act(executed: Any, **_: Any) -> ActionResult:
        routed.append(executed)
        return ActionResult(success=True)

    _kept, guard = build_guarded_act(_act, descriptors, bindings=negotiated.action_bindings)

    result = await guard(action)
    assert result.success, result.error
    return routed[0].name


class TestAgentToCapabilityActionVocabulary:
    """Every action name an agent emits must reach a real capability key.

    The agents name no capability — that is the whole point of
    capability-relative keys — so the property under test is no longer
    "the emitted string is in ``provided_actions``" but "negotiation and
    the guard turn the emitted string into a key that is".
    """

    async def test_browser_use_verbs_resolve_to_capability_actions(self) -> None:
        """Now a tautology by construction, and worth pinning as one.

        browser_use compiles its output schema *from the negotiated
        descriptors*, so the verbs it can emit are the keys the capability
        provides — there is no vocabulary map left to drift. The test walks
        the whole negotiated set and puts each one through the guard.
        """
        from spectragent.core.capabilities.browser.capability import (
            BrowserCapability,
        )

        agent = make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=10))
        capability = BrowserCapability()
        provided = capability.provided_actions

        negotiated = negotiate_action(agent, _ActionsOnlyEnv(capability))
        descriptors = [
            a.descriptor() for a in capability.actions if a.action_id in negotiated.active_actions
        ]
        agent.prepare(
            task="t",
            action_descriptors=descriptors,
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        model = agent.get_react_output_model()

        for descriptor in descriptors:
            params = minimal_params(descriptor)
            out = model.model_validate({"memory": "", "action": [{descriptor.name: params}]})
            for action in agent.parse_react_output(out):
                routed = await _resolve(agent, action, capability)
                assert routed in provided, (
                    f"BrowserUseAgent emitted {action.name!r}, which resolved "
                    f"to {routed!r} — not in BrowserCapability."
                    f"provided_actions {sorted(provided)}"
                )

    async def test_ui_tars_click_produces_click_coordinate(self) -> None:
        from spectragent.core.capabilities.browser.capability import (
            BrowserCapability,
        )

        agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
        # A point is placed with the screenshot the step showed.
        (
            await agent.build_react_prompt(
                type(
                    "Obs",
                    (),
                    {
                        "platform": BrowserPlatformObs(operating_system="macos"),
                        "task": type("Task", (), {"user_request": "t"})(),
                        "screenshot": ScreenshotObs(
                            image_bytes=b"\x89PNG\r\n\x1a\n",
                            viewport_width=1280,
                            viewport_height=720,
                            image_width=1280,
                            image_height=720,
                        ),
                    },
                )(),
                context=ReactPromptContext(step=0, phase="policy"),
            )
        )
        actions = agent.parse_react_output("Thought: click it.\nAction: click(point='(100, 200)')")
        assert len(actions) == 1
        assert actions[0].name == "click_coordinate"
        capability = BrowserCapability()
        assert await _resolve(agent, actions[0], capability) == "browser.click_coordinate"

    async def test_all_three_agents_done_uses_extracted_content(self) -> None:
        # Regression: every adapted agent must emit its done action
        # with the ``extracted_content`` key so the answer survives the
        # capability's DoneEvent translation.
        from spectragent.agents._output_models import SkyvernOutput
        from spectragent.core.capabilities.browser.capability import (
            BrowserCapability,
        )

        capability = BrowserCapability()

        # BrowserUseAgent — ``done``'s answer field is named once, by the
        # action (``extracted_content``). The agent used to rename ``text``
        # onto it, which meant the prompt, the schema and the executor each
        # had their own name for the same value.
        bu = make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=10))
        bu.prepare(
            task="t",
            action_descriptors=[
                a.descriptor() for a in capability.actions if a.action_id == "browser.done"
            ],
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        bu_out = bu.get_react_output_model().model_validate(
            {
                "memory": "",
                "action": [{"browser.done": {"extracted_content": "answer bu", "success": True}}],
            }
        )
        bu_actions = bu.parse_react_output(bu_out)
        assert await _resolve(bu, bu_actions[0], capability) == "browser.done"
        assert bu_actions[0].params["extracted_content"] == "answer bu"

        # SkyvernAgent
        sk = make_agent(SkyvernAgent, memory=SimpleMemory(max_messages=10))
        sk_out = SkyvernOutput(
            actions=[],
            is_done=True,
            extracted_content="answer sk",
        )
        sk_actions = sk.parse_react_output(sk_out)
        assert await _resolve(sk, sk_actions[0], capability) == "browser.done"
        assert sk_actions[0].params["extracted_content"] == "answer sk"

        # UITarsAgent
        ut = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
        ut_actions = ut.parse_react_output("Action: finished(content='answer ut')")
        assert await _resolve(ut, ut_actions[0], capability) == "browser.done"
        assert ut_actions[0].params["extracted_content"] == "answer ut"


class TestReactLoopDriveBrowserUse:
    @pytest.mark.asyncio
    async def test_browser_use_one_step_done(self) -> None:
        # Build a fake observation matching the fragment-based shape.
        # ``task`` mirrors what populate_loop_fragments would inject —
        # this driver test bypasses ReactLoop's loop-fragment path because
        # the obs is a plain object (not a Pydantic model).
        obs = type(
            "Obs",
            (),
            {
                "platform": BrowserPlatformObs(operating_system="macos"),
                "dom": DOMObs(),
                "screenshot": ScreenshotObs(image_bytes=b"\x89PNG\r\n\x1a\n"),
                "browser_meta": BrowserMetaObs(
                    url="https://example.com",
                    page_title="Example",
                ),
                "task": TaskObs(user_request="Find the answer"),
                "step_info": StepInfoObs(
                    step=1,
                    max_steps=2,
                    today="2026-05-01",
                ),
                "history": HistoryObs(),
            },
        )()

        # ``max_steps`` is set on the loop below — it is the only source.
        agent = make_agent(
            BrowserUseAgent,
            memory=SimpleMemory(max_messages=100),
        )

        env = _MockEnv(obs)
        loop = ReactLoop()
        loop.max_steps = 2
        loop.max_actions_per_step = 3

        # Drive the loop directly (skip AgentRuntime to avoid AOP weaving),
        # but keep the guard: it is what turns the agent's ``done`` into
        # the ``browser.done`` the environment routes on, and a loop
        # driven without it would be testing a path production never
        # takes.
        from spectragent.core.capabilities.browser.capability import (
            BrowserCapability,
        )

        negotiated_actions = negotiate_action(agent, _ActionsOnlyEnv(BrowserCapability()))
        _kept, _act_fn = build_guarded_act(
            env.act,
            [
                ActionDescriptor(name=name, description="")
                for name in sorted(negotiated_actions.active_actions)
            ],
            bindings=negotiated_actions.action_bindings,
        )

        # Post-M17: AgentRuntime owns the prepare → memory.add_messages
        # handoff. Driving the loop without AgentRuntime requires the
        # caller to replicate that step. The descriptors are the negotiated
        # ones, because ``prepare`` compiles them into the output schema
        # the stub LLM below has to satisfy.
        descriptors = [
            a.descriptor()
            for a in BrowserCapability().actions
            if a.action_id in negotiated_actions.active_actions
        ]
        seed = agent.prepare(
            task="Find the answer",
            action_descriptors=descriptors,
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        agent.memory.add_messages(seed)
        llm = _MockLLM(
            agent.get_react_output_model().model_validate(
                {
                    "memory": "",
                    "action": [{"browser.done": {"extracted_content": "finished"}}],
                }
            )
        )

        ctx = {
            "env": env,
            "llm": llm,
            "memory": agent.memory,
            "act_fn": _act_fn,
            "negotiated_obs": _MockNegotiated(),
            "active_descriptors": [],
            "session_id": None,
            "identity": RunIdentity(task_id="t1"),
        }
        result = await loop._execute(
            agent, task="Find the answer", ctx={**ctx, "step_executor": loop}
        )

        # The agent emitted "done" → result should reflect that.
        assert result.task_id == "t1"
        # ReactLoop reports agent_done="done" via AgentDoneStatus.
        from spectragent.schemas._types import AgentDoneStatus

        assert result.agent_done == AgentDoneStatus.DONE
        # Env recorded the action.
        assert len(env.acted) == 1
        assert env.acted[0].name == "browser.done"
        # Agent state was populated by the explicit prepare() call above.
        # ``_task`` was deleted; task now lives on every observation as a
        # loop-source fragment.
        assert not hasattr(agent, "_task")
        assert agent._system_prompt_text  # populated

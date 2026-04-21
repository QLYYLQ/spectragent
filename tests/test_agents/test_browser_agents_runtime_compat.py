"""End-to-end compatibility tests for the M14 browser agents.

Verifies that all three adapted agents:

- Pass ``AgentRuntime``'s ``validate_compatibility`` against ``ReactLoop``
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
from spectragent.core.loops.react import ReactLoop
from tests._helpers.assembly import make_agent
from spectragent.core.loops.validation import validate_compatibility
from spectragent.schemas.observation.fragments import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.schemas.observation.negotiation import (
    extract_observation_wiring,
    negotiate,
)


# ── Validate compatibility ────────────────────────────────────────────


class TestValidateCompatibility:
    def test_browser_use_agent_compatible_with_react_loop(self) -> None:
        agent = make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=10))
        loop = ReactLoop()
        # Should not raise.
        validate_compatibility(agent, loop)

    def test_skyvern_agent_compatible_with_react_loop(self) -> None:
        agent = make_agent(SkyvernAgent, memory=SimpleMemory(max_messages=10))
        loop = ReactLoop()
        validate_compatibility(agent, loop)

    def test_ui_tars_agent_compatible_with_react_loop(self) -> None:
        agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
        loop = ReactLoop()
        validate_compatibility(agent, loop)


# ── Observation negotiation ───────────────────────────────────────────


class TestObservationNegotiation:
    """The most important M14 verification: UITars (vision-only) must NOT
    activate the DOM fragment, even if the environment provides it."""

    def test_ui_tars_does_not_activate_dom(self) -> None:
        # Env provides everything (browser capability default).
        env_provides = {"dom", "screenshot", "browser_meta"}

        # UITars wiring: required=[screenshot], optional=[]
        agent_required, agent_optional = extract_observation_wiring(UITarsAgent)
        # ReactLoop has no observation wiring of its own.
        loop_required, loop_optional = extract_observation_wiring(ReactLoop)

        result = negotiate(
            env_provides=env_provides,
            agent_required=agent_required,
            agent_optional=agent_optional,
            loop_required=loop_required,
            loop_optional=loop_optional,
        )

        assert "screenshot" in result.active_fragments
        assert "dom" not in result.active_fragments, (
            "UITars must not trigger DOM resolution — wiring requires "
            "only 'screenshot'"
        )
        assert "browser_meta" not in result.active_fragments

    def test_browser_use_activates_dom_screenshot_and_meta(self) -> None:
        env_provides = {"dom", "screenshot", "browser_meta"}
        agent_required, agent_optional = extract_observation_wiring(
            BrowserUseAgent
        )
        loop_required, loop_optional = extract_observation_wiring(ReactLoop)

        result = negotiate(
            env_provides=env_provides,
            agent_required=agent_required,
            agent_optional=agent_optional,
            loop_required=loop_required,
            loop_optional=loop_optional,
        )

        # dom + screenshot are required, browser_meta is optional but
        # the env provides it so it should be active too.
        assert {"dom", "screenshot", "browser_meta"} <= result.active_fragments

    def test_skyvern_activates_dom_and_drops_unused_optionals(self) -> None:
        # When the env only provides dom (no screenshot/meta), Skyvern's
        # required dom is satisfied and the optionals are simply absent.
        env_provides = {"dom"}
        agent_required, agent_optional = extract_observation_wiring(
            SkyvernAgent
        )
        loop_required, loop_optional = extract_observation_wiring(ReactLoop)

        result = negotiate(
            env_provides=env_provides,
            agent_required=agent_required,
            agent_optional=agent_optional,
            loop_required=loop_required,
            loop_optional=loop_optional,
        )

        assert "dom" in result.active_fragments
        assert "screenshot" not in result.active_fragments

    def test_ui_tars_fails_when_env_lacks_screenshot(self) -> None:
        # Required fragment missing → negotiation must raise.
        from spectragent.schemas.observation.negotiation import (
            IncompatibleFragmentError,
        )

        env_provides: set[str] = {"dom"}
        agent_required, agent_optional = extract_observation_wiring(UITarsAgent)
        loop_required, loop_optional = extract_observation_wiring(ReactLoop)

        with pytest.raises(IncompatibleFragmentError):
            negotiate(
                env_provides=env_provides,
                agent_required=agent_required,
                agent_optional=agent_optional,
                loop_required=loop_required,
                loop_optional=loop_optional,
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
        is_done = action.name == "done"
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
    active_fragments = {"dom", "screenshot", "browser_meta"}
    env_sourced = {"dom", "screenshot", "browser_meta"}
    loop_sourced: set[str] = set()


class TestAgentToCapabilityActionVocabulary:
    """Round-trip regression tests: every action name an agent can emit
    must be recognised by ``BrowserCapability.provided_actions``. After
    M13-Rev the symbol moved from a static ``_ACTION_EVENT_MAP`` to the
    symmetric ``provided_actions`` frozenset derived from ``__wiring__``.
    """

    def test_browser_use_agent_actions_are_in_capability_provided_actions(
        self,
    ) -> None:
        from spectragent.agents._output_models import (
            BrowserUseActionItem,
            BrowserUseOutput,
            BrowserUseStateAssessment,
        )
        from spectragent.core.capabilities.browser.capability import (
            BrowserCapability,
        )

        agent = make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=10))
        provided = BrowserCapability().provided_actions

        # Every LLM action_name the agent is expected to emit.
        llm_action_names = [
            "click_element",
            "input_text",
            "scroll",
            "go_to_url",
            "go_back",
            "send_keys",
            "extract_content",
            "done",
        ]
        for name in llm_action_names:
            out = BrowserUseOutput(
                current_state=BrowserUseStateAssessment(),
                actions=[BrowserUseActionItem(action_name=name, index=1)],
            )
            parsed = agent.parse_react_output(out)
            for action in parsed:
                assert action.name in provided, (
                    f"BrowserUseAgent emitted {action.name!r} but "
                    f"BrowserCapability.provided_actions has no entry for it. "
                    f"Provided: {sorted(provided)}"
                )

    def test_ui_tars_click_produces_click_coordinate(self) -> None:
        from spectragent.core.capabilities.browser.capability import (
            BrowserCapability,
        )

        agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
        actions = agent.parse_react_output(
            "Thought: click it.\nAction: click(point='(100, 200)')"
        )
        assert len(actions) == 1
        # click_coordinate MUST be provided by BrowserCapability
        assert actions[0].name == "click_coordinate"
        assert actions[0].name in BrowserCapability().provided_actions

    def test_all_three_agents_done_uses_extracted_content(self) -> None:
        # C2 regression: every adapted agent must emit its done action
        # with the ``extracted_content`` key so the answer survives the
        # capability's DoneEvent translation.
        from spectragent.agents._output_models import (
            BrowserUseActionItem,
            BrowserUseOutput,
            BrowserUseStateAssessment,
            SkyvernOutput,
        )

        # BrowserUseAgent
        bu = make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=10))
        bu_out = BrowserUseOutput(
            current_state=BrowserUseStateAssessment(),
            actions=[
                BrowserUseActionItem(
                    action_name="done", text="answer bu", success=True,
                )
            ],
        )
        bu_actions = bu.parse_react_output(bu_out)
        assert bu_actions[0].name == "done"
        assert bu_actions[0].params["extracted_content"] == "answer bu"

        # SkyvernAgent
        sk = make_agent(SkyvernAgent, memory=SimpleMemory(max_messages=10))
        sk_out = SkyvernOutput(
            actions=[],
            is_done=True,
            extracted_content="answer sk",
        )
        sk_actions = sk.parse_react_output(sk_out)
        assert sk_actions[0].name == "done"
        assert sk_actions[0].params["extracted_content"] == "answer sk"

        # UITarsAgent
        ut = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
        ut_actions = ut.parse_react_output(
            "Action: finished(content='answer ut')"
        )
        assert ut_actions[0].name == "done"
        assert ut_actions[0].params["extracted_content"] == "answer ut"


class TestReactLoopDriveBrowserUse:
    @pytest.mark.asyncio
    async def test_browser_use_one_step_done(self) -> None:
        from spectragent.agents._output_models import (
            BrowserUseActionItem,
            BrowserUseOutput,
            BrowserUseStateAssessment,
        )

        # Build a fake observation matching the fragment-based shape.
        obs = type(
            "Obs",
            (),
            {
                "dom": DOMObs(),
                "screenshot": ScreenshotObs(image_bytes=b"\x89PNG\r\n\x1a\n"),
                "browser_meta": BrowserMetaObs(
                    url="https://example.com", page_title="Example",
                ),
            },
        )()

        agent = make_agent(
            BrowserUseAgent,
            max_steps=2,
            memory=SimpleMemory(max_messages=100),
        )

        out = BrowserUseOutput(
            current_state=BrowserUseStateAssessment(),
            actions=[
                BrowserUseActionItem(action_name="done", text="finished")
            ],
        )
        llm = _MockLLM(out)
        env = _MockEnv(obs)
        loop = ReactLoop()
        loop.max_steps = 2
        loop.max_actions_per_step = 3

        # Drive the loop directly (skip AgentRuntime to avoid AOP weaving).
        async def _act_fn(action: Any, **kw: Any) -> Any:
            return await env.act(action)

        loop.ctx = dict(
            env=env, llm=llm, memory=agent.memory,
            act_fn=_act_fn, negotiated_obs=_MockNegotiated(),
            active_descriptors=[], session_id=None, task_id="t1",
        )
        result = await loop._execute(agent, task="Find the answer")

        # The agent emitted "done" → result should reflect that.
        assert result.task_id == "t1"
        # ReactLoop reports agent_done="done" via AgentDoneStatus.
        from spectragent.schemas._types import AgentDoneStatus

        assert result.agent_done == AgentDoneStatus.DONE
        # Env recorded the action.
        assert len(env.acted) == 1
        assert env.acted[0].name == "done"
        # Agent's prepare() was called by the loop and seeded the
        # system prompt into memory.
        assert agent._task == "Find the answer"
        assert agent._system_prompt_text  # populated

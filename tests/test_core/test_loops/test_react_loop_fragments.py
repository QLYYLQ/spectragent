"""ReactLoop ↔ loop-source fragment population.

Verifies that the Loop fills loop-source fragments (task / step_info /
history / action_result) into the observation between env.observe() and
agent.build_react_prompt(), per the negotiation spec in
docs/architecture_design.md §4.4.
"""

from __future__ import annotations

import asyncio
from typing import Any

from spectragent.core.loops.react import ReactLoop
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation import fragments as _  # noqa: F401
from spectragent.schemas.observation.dynamic_model import build_observation_model

# ── Helpers shared with test_react_parse_error ────────────────────


def _make_loop(
    max_steps: int = 100,
    today_override: str | None = None,
) -> ReactLoop:
    loop = ReactLoop()
    loop.max_steps = max_steps
    loop.today_override = today_override
    return loop


class _FakeNegotiated:
    """Stand-in for ObservationNegotiationResult passed to env.observe."""

    def __init__(self, active: frozenset[str], loop_sourced: frozenset[str]) -> None:
        self.active_fragments = active
        self.loop_sourced = loop_sourced
        self.env_sourced = active - loop_sourced


class _FakeEnv:
    """Env that builds a real NegotiatedObservation with loop fields = None.

    The dynamic model defaults loop-source fields to None; ReactLoop is
    responsible for filling them via populate_loop_fragments.
    """

    env_type = "fake"

    def __init__(self, active: frozenset[str]) -> None:
        self._model = build_observation_model(active)

    async def observe(self, *_a: Any, **_kw: Any) -> Any:
        return self._model()

    async def act(self, action: Action, **_kw: Any) -> ActionResult:
        if action.name == "done":
            return ActionResult(success=True, is_done=True, extracted_content="ok")
        return ActionResult(success=True)

    def get_action_space(self, **_kw: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name="done", description="Done")]


class _FakeLLM:
    model = "test"

    def __init__(self, completions: list[str]) -> None:
        self._completions = list(completions)
        self._idx = 0

    async def ainvoke(self, _msgs: Any, **_kw: Any) -> Any:
        text = self._completions[min(self._idx, len(self._completions) - 1)]
        self._idx += 1

        class R:
            completion = text

        return R()


class _FakeMemory:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, msg: Any) -> None:
        self.messages.append(msg)

    def add_messages(self, msgs: Any) -> None:
        self.messages.extend(msgs)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


class _CapturingAgent:
    """Records the observation it was handed each step."""

    __wiring__: dict = {}

    def __init__(self, max_steps_until_done: int = 1) -> None:
        self.received: list[Any] = []
        self._calls = 0
        self._max = max_steps_until_done

    def build_react_prompt(self, obs: Any) -> list:
        self.received.append(obs)
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, _llm_output: Any) -> list[Action]:
        self._calls += 1
        if self._calls >= self._max:
            return [Action(name="done", params={"extracted_content": "ok"})]
        return [Action(name="click", params={})]

    def get_react_output_model(self) -> None:
        return None


def _drive(loop: ReactLoop, agent: _CapturingAgent, env: _FakeEnv,
           negotiated: _FakeNegotiated, completions: list[str], task: str = "t") -> Any:
    ctx = {
        "env": env,
        "llm": _FakeLLM(completions),
        "memory": _FakeMemory(),
        "act_fn": env.act,
        "negotiated_obs": negotiated,
        "task_id": "task-1",
    }
    return asyncio.run(loop._execute(agent, task, ctx=ctx))


# ── Tests ─────────────────────────────────────────────────────────


class TestTaskFragment:
    def test_task_user_request_visible_to_agent(self) -> None:
        active = frozenset({"loop.task"})
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset({"loop.task"}))
        agent = _CapturingAgent()
        loop = _make_loop(max_steps=2, today_override="2026-05-01")

        _drive(loop, agent, env, negotiated, ["x"], task="navigate to example.com")
        assert len(agent.received) == 1
        assert agent.received[0].task is not None
        assert agent.received[0].task.user_request == "navigate to example.com"


class TestStepInfoFragment:
    def test_step_info_step_starts_at_one(self) -> None:
        active = frozenset({"loop.step_info"})
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset({"loop.step_info"}))
        agent = _CapturingAgent(max_steps_until_done=2)
        loop = _make_loop(max_steps=5, today_override="2026-05-01")

        _drive(loop, agent, env, negotiated, ["x", "x"])
        assert agent.received[0].step_info.step == 1
        assert agent.received[1].step_info.step == 2

    def test_step_info_max_steps_matches_loop(self) -> None:
        active = frozenset({"loop.step_info"})
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset({"loop.step_info"}))
        agent = _CapturingAgent()
        loop = _make_loop(max_steps=42, today_override="2026-05-01")

        _drive(loop, agent, env, negotiated, ["x"])
        assert agent.received[0].step_info.max_steps == 42

    def test_today_override_propagates(self) -> None:
        active = frozenset({"loop.step_info"})
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset({"loop.step_info"}))
        agent = _CapturingAgent()
        loop = _make_loop(max_steps=2, today_override="2026-05-01")

        _drive(loop, agent, env, negotiated, ["x"])
        assert agent.received[0].step_info.today == "2026-05-01"

    def test_today_falls_back_to_real_clock(self) -> None:
        active = frozenset({"loop.step_info"})
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset({"loop.step_info"}))
        agent = _CapturingAgent()
        loop = _make_loop(max_steps=2)  # no today_override

        _drive(loop, agent, env, negotiated, ["x"])
        today = agent.received[0].step_info.today
        # Just check it parses as ISO date.
        from datetime import date as _date
        _date.fromisoformat(today)


class TestHistoryFragment:
    def test_history_grows_per_step(self) -> None:
        active = frozenset({"loop.history"})
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset({"loop.history"}))
        agent = _CapturingAgent(max_steps_until_done=3)
        loop = _make_loop(max_steps=5, today_override="2026-05-01")

        _drive(loop, agent, env, negotiated, ["x", "y", "z"])
        # First step sees empty history; later steps see prior records.
        assert len(agent.received[0].history.items) == 0
        assert len(agent.received[1].history.items) == 1
        assert len(agent.received[2].history.items) == 2


class TestActionResultFragment:
    def test_action_result_zero_step_sentinel(self) -> None:
        active = frozenset({"loop.action_result"})
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset({"loop.action_result"}))
        agent = _CapturingAgent()
        loop = _make_loop(max_steps=2, today_override="2026-05-01")

        _drive(loop, agent, env, negotiated, ["x"])
        ar = agent.received[0].action_result
        assert ar is not None
        assert ar.last_action == ""
        assert ar.success is True

    def test_action_result_reflects_previous_step(self) -> None:
        active = frozenset({"loop.action_result"})
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset({"loop.action_result"}))
        agent = _CapturingAgent(max_steps_until_done=2)
        loop = _make_loop(max_steps=5, today_override="2026-05-01")

        _drive(loop, agent, env, negotiated, ["x", "x"])
        # Step 1 should see step 0's "click" action as the previous result.
        ar1 = agent.received[1].action_result
        assert ar1.last_action == "click"
        assert ar1.success is True


class TestSelectiveActivation:
    def test_unrequested_loop_fragments_skipped(self) -> None:
        # Agent only requests loop.task; env model still has step_info as
        # a field if active_fragments included it, but we keep it out of
        # active here.
        active = frozenset({"loop.task"})
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset({"loop.task"}))
        agent = _CapturingAgent()
        loop = _make_loop(max_steps=2, today_override="2026-05-01")

        _drive(loop, agent, env, negotiated, ["x"])
        obs = agent.received[0]
        assert obs.task is not None
        assert not hasattr(obs, "step_info")
        assert not hasattr(obs, "history")

    def test_no_loop_fragments_no_population(self) -> None:
        # When loop_sourced is empty, populate_loop_fragments short-circuits.
        active: frozenset[str] = frozenset()
        env = _FakeEnv(active)
        negotiated = _FakeNegotiated(active, frozenset())
        agent = _CapturingAgent()
        loop = _make_loop(max_steps=2, today_override="2026-05-01")

        result = _drive(loop, agent, env, negotiated, ["x"])
        assert result.agent_done.value == "done"

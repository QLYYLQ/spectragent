"""Tests for Loop.weave() — the monkey-patch mechanism."""

import asyncio
import copy

import pytest

from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar
from spectragent.core.loops.validation import IncompatibleError


class _TestLoop(AgentLoopBase):
    __registry_key__ = "test_weave_loop"
    __required_slots__ = {"build_prompt", "parse_output"}
    __optional_slots__ = {"prepare"}
    __requires_env__ = True

    async def _execute(self, agent, task):
        return {"task": task, "env": self.ctx.get("env"), "agent_name": agent.name}


class _NoEnvLoop(AgentLoopBase):
    __registry_key__ = "test_weave_noenv"
    __required_slots__ = {"build_plan"}
    __requires_env__ = False

    async def _execute(self, agent, task):
        return {"task": task, "plan": True}


def teardown_module():
    AgentLoopRegistrar.unregister("test_weave_loop")
    AgentLoopRegistrar.unregister("test_weave_noenv")


class _GoodAgent:
    name = "good"
    def build_prompt(self, obs):
        return []
    def parse_output(self, out):
        return []


class _PlanAgent:
    name = "planner"
    def build_plan(self, obs):
        return []


class TestWeave:
    def test_weave_patches_run(self) -> None:
        agent = _GoodAgent()
        assert not hasattr(agent, "run")

        loop = _TestLoop()
        loop.weave(agent, env="fake_env")
        assert hasattr(agent, "run")
        assert callable(agent.run)

    def test_weave_returns_same_agent(self) -> None:
        agent = _GoodAgent()
        loop = _TestLoop()
        result = loop.weave(agent, env="fake_env")
        assert result is agent

    def test_run_calls_execute(self) -> None:
        agent = _GoodAgent()
        loop = _TestLoop()
        loop.weave(agent, env="fake_env")

        result = asyncio.run(agent.run("do something"))
        assert result["task"] == "do something"
        assert result["env"] == "fake_env"
        assert result["agent_name"] == "good"

    def test_deps_bound_to_self_ctx(self) -> None:
        agent = _GoodAgent()
        loop = _TestLoop()
        loop.weave(agent, env="env1", llm="llm1", memory="mem1")

        assert loop.ctx["env"] == "env1"
        assert loop.ctx["llm"] == "llm1"

        result = asyncio.run(agent.run("task"))
        assert result["env"] == "env1"

    def test_missing_required_slots_raises(self) -> None:
        class BadAgent:
            agent_name = "bad"

        loop = _TestLoop()
        with pytest.raises(IncompatibleError) as exc_info:
            loop.weave(BadAgent())

        assert "build_prompt" in exc_info.value.missing_slots
        assert "parse_output" in exc_info.value.missing_slots

    def test_optional_slot_missing_ok(self) -> None:
        # _GoodAgent has build_prompt and parse_output but not prepare
        agent = _GoodAgent()
        loop = _TestLoop()
        loop.weave(agent, env="e")  # Should not raise

    def test_instance_level_patch(self) -> None:
        """weave patches the instance, not the class."""
        agent1 = _GoodAgent()
        agent2 = _GoodAgent()

        loop = _TestLoop()
        loop.weave(agent1, env="env1")

        assert hasattr(agent1, "run")
        assert not hasattr(agent2, "run")

    def test_deepcopy_different_loops(self) -> None:
        """deepcopy agent can be woven with different loops."""
        agent = _GoodAgent()
        # _GoodAgent also satisfies _NoEnvLoop? No — it needs build_plan.
        # Use a dual agent.
        class DualAgent:
            name = "dual"
            def build_prompt(self, obs): return []
            def parse_output(self, out): return []
            def build_plan(self, obs): return []

        original = DualAgent()
        agent_react = copy.deepcopy(original)
        agent_plan = copy.deepcopy(original)

        _TestLoop().weave(agent_react, env="browser_env")
        _NoEnvLoop().weave(agent_plan)

        r1 = asyncio.run(agent_react.run("browse"))
        r2 = asyncio.run(agent_plan.run("plan"))

        assert r1["env"] == "browser_env"
        assert r2["plan"] is True

    def test_no_env_loop_works_without_env(self) -> None:
        agent = _PlanAgent()
        loop = _NoEnvLoop()
        loop.weave(agent)  # no env passed

        result = asyncio.run(agent.run("create plan"))
        assert result["task"] == "create plan"
        assert result["plan"] is True

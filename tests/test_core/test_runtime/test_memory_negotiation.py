"""Tests for Memory protocol negotiation (M17)."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.core.loops import ReactLoop
from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar
from spectragent.core.negotiation.memory import (
    IncompatibleMemoryError,
    negotiate_memory,
)
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import AGENT_ID, make_env, make_run


class _ToolCallLoop(AgentLoopBase):
    __registry_key__ = "test_memory_nego_toolcall"
    __required_slots__: ClassVar[set[str]] = set()
    __optional_slots__: ClassVar[set[str]] = set()
    __required_message_protocols__: ClassVar[frozenset[MessageProtocol]] = frozenset({"tool_call"})

    async def _execute(self, agent, task, **kwargs):
        return None


class _NoRequireLoop(AgentLoopBase):
    __registry_key__ = "test_memory_nego_norequire"
    __required_slots__: ClassVar[set[str]] = set()
    __optional_slots__: ClassVar[set[str]] = set()

    async def _execute(self, agent, task, **kwargs):
        return None


def teardown_module():
    AgentLoopRegistrar.unregister("test_memory_nego_toolcall")
    AgentLoopRegistrar.unregister("test_memory_nego_norequire")


class TestNegotiateMemory:
    def test_compatible_passes(self) -> None:
        negotiate_memory(ReactLoop(), SimpleMemory())

    def test_compatible_passes_sliding_window(self) -> None:
        negotiate_memory(ReactLoop(), SlidingWindowMemory())

    def test_missing_protocol_raises(self) -> None:
        loop = _ToolCallLoop()
        memory = SimpleMemory()
        with pytest.raises(IncompatibleMemoryError) as exc_info:
            negotiate_memory(loop, memory)

        err = exc_info.value
        assert err.missing_protocols == ["tool_call"]
        assert err.memory_id == "simple"
        assert err.loop_id == "test_memory_nego_toolcall"
        msg = str(err)
        assert "simple" in msg
        assert "test_memory_nego_toolcall" in msg
        assert "tool_call" in msg

    def test_no_required_passes(self) -> None:
        negotiate_memory(_NoRequireLoop(), SimpleMemory())


# ── Integration: AgentRuntime.prepare runs memory negotiation ───

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase


class _DoneCap(CapabilityBase):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "rt_memnego_cap"
    __wiring__ = {"action": ("action", ["rt_memnego_cap.done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="rt_memnego_cap.done", description="Done")]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, is_done=True, extracted_content="ok")


class _Env(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "rt_memnego_env"


class _Agent:
    __wiring__ = {"action": ("action", ["rt_memnego_cap.done"])}

    def build_react_prompt(self, obs):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, llm_output):
        return [Action(name="rt_memnego_cap.done")]

    def get_react_output_model(self):
        return None


class _Completion:
    completion = "done"
    usage = None


class _LLM:
    model = "test"

    async def ainvoke(self, messages, **kwargs):
        return _Completion()


class TestPrepareRunsMemoryNegotiation:
    def test_runtime_prepare_runs_memory_negotiation(self) -> None:
        agent = _Agent()
        loop = _ToolCallLoop()
        env = make_env(_Env, {"rt_memnego_cap": _DoneCap()})
        run = make_run(
            agent=agent,
            loop=loop,
            env=env,
            llm=_LLM(),
            memory=SimpleMemory(),
        )
        with pytest.raises(IncompatibleMemoryError):
            AgentRuntime.for_agent(run, AGENT_ID).prepare()

        # Loop weave never happened — agent.run was not patched.
        assert not hasattr(agent, "run")

    def test_runtime_prepare_compatible_memory_passes(self) -> None:
        agent = _Agent()
        env = make_env(_Env, {"rt_memnego_cap": _DoneCap()})
        run = make_run(
            agent=agent,
            loop=ReactLoop(),
            env=env,
            llm=_LLM(),
            memory=SimpleMemory(),
        )
        prepared = AgentRuntime.for_agent(run, AGENT_ID).prepare()
        assert hasattr(prepared, "run")
        assert callable(prepared.run)
        # Drain any coroutine state.
        del prepared
        asyncio.get_event_loop_policy()

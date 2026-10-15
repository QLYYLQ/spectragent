"""The typed per-task channel into a Loop, through ``AgentRuntime``.

``docs/architecture_design.md`` §4.1 "Loop 输入" and §4.2 "任务上下文的三部分":
``prepare()`` checks ``loop_inputs`` right after slot validation — before
negotiation, before ``on_run_start`` (so a refused task leaves no trace
directory) and before any environment setup; the checked inputs reach the
Loop as ``ctx["loop_inputs"]``; ``branch`` reaches ``on_run_start`` by name;
and the untyped ``**kwargs`` path into ``weave`` no longer exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar, LoopInput
from spectragent.core.loops.validation import IncompatibleLoopInputError
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.restoration import BranchProvenance, Checkpoint
from tests._helpers.assembly import AGENT_ID, make_env, make_run


class _Cap(CapabilityBase):
    """No ``__init__`` of its own: it would hide the inherited ``id`` knob from conscribe."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.

    __registry_key__ = "loop_inputs_channel_cap"
    __wiring__ = {"action": ("action", ["loop_inputs_channel_cap.done"])}

    setups = 0

    async def setup(self, *, negotiation: Any = None) -> None:
        self.setups += 1

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name="loop_inputs_channel_cap.done", description="Done")]

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        return ActionResult(success=True, is_done=True, extracted_content="ok")


class _Env(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "loop_inputs_channel_env"


class _Agent:
    __execution_limit_fields__ = frozenset()
    __wiring__ = {"action": ("action", ["loop_inputs_channel_cap.done"])}

    async def build_react_prompt(self, obs: Any, *, context: ReactPromptContext) -> list[Any]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, raw: Any) -> list[Action]:
        return [Action(name="loop_inputs_channel_cap.done")]


class _LLM:
    model = "test"

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        class _C:
            completion = "done"
            usage = None

        return _C()

    name = "test-model"

    async def aclose(self, *, timeout=5.0):
        return ()


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

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


class _ProbeLoop(AgentLoopBase):
    """Declares one required input and reports the ctx it was handed."""

    __registry_key__ = "test_loop_inputs_channel_probe"
    __required_slots__ = {"build_react_prompt"}
    __required_message_protocols__ = frozenset({"user_assistant"})
    __loop_inputs__ = {"checkpoint": LoopInput(Checkpoint)}

    async def _execute(self, agent: Any, task: str, *, ctx: dict[str, Any], **kwargs: Any) -> Any:
        _SEEN.append(dict(ctx))
        return AgentResult(
            task_id=ctx["identity"].task_id,
            task=task,
            timestamp=datetime.now(UTC),
            env_status=EnvironmentStatus.SUCCESS,
            agent_done=AgentDoneStatus.DONE,
            metrics=AgentMetrics(execution_ms=0, steps=0),
        )


_SEEN: list[dict[str, Any]] = []


def teardown_module() -> None:
    AgentLoopRegistrar.unregister("test_loop_inputs_channel_probe")


class _StartSpy:
    hook_id = "loop_inputs_channel_spy"
    blocking = True
    priority = 1
    __intercepts__: list[Any] = []

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []

    async def on_run_start(self, **kw: Any) -> None:
        self.starts.append(kw)

    async def on_run_end(self, **kw: Any) -> None:
        pass

    async def on_run_error(self, **kw: Any) -> None:
        pass


def _runtime(loop: Any, *, hooks: list[Any] | None = None) -> tuple[AgentRuntime, _Cap]:
    cap = _Cap()
    env = make_env(_Env, {"loop_inputs_channel_cap": cap})
    run = make_run(agent=_Agent(), loop=loop, env=env, llm=_LLM(), memory=_Memory(), hooks=hooks)
    return AgentRuntime.for_agent(run, AGENT_ID), cap


def _checkpoint(tmp_path: Any) -> Checkpoint:
    return Checkpoint(trace_dir=str(tmp_path / "source"), step=1)


@pytest.fixture(autouse=True)
def _clear_seen() -> None:
    _SEEN.clear()


class TestRefusedEarly:
    def test_missing_input_is_refused_before_negotiation(self, monkeypatch) -> None:  # noqa: ANN001
        def _negotiated(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("negotiation ran before the loop inputs were checked")

        monkeypatch.setattr("spectragent.core.runtime.negotiate_observation", _negotiated)
        runtime, _ = _runtime(_ProbeLoop())
        with pytest.raises(IncompatibleLoopInputError, match="requires checkpoint"):
            runtime.prepare()

    async def test_refused_before_on_run_start_and_setup(self, tmp_path) -> None:  # noqa: ANN001
        traces = tmp_path / "traces"
        spy = _StartSpy()
        runtime, cap = _runtime(_ProbeLoop(), hooks=[spy, TraceWriter(trace_dir=str(traces))])
        with pytest.raises(IncompatibleLoopInputError):
            await runtime.execute("t", task_id="t-1", loop_inputs={"checkpoint": {"step": 1}})
        assert spy.starts == []
        assert cap.setups == 0
        assert not traces.exists()

    async def test_react_refuses_a_checkpoint_before_any_trace(self, tmp_path) -> None:  # noqa: ANN001
        traces = tmp_path / "traces"
        runtime, cap = _runtime(ReactLoop(), hooks=[TraceWriter(trace_dir=str(traces))])
        with pytest.raises(IncompatibleLoopInputError) as err:
            await runtime.execute("t", loop_inputs={"checkpoint": _checkpoint(tmp_path)})
        assert err.value.loop_id == "react"
        assert err.value.unknown == ["checkpoint"]
        assert cap.setups == 0
        assert not traces.exists()


class TestDelivered:
    async def test_inputs_reach_the_loop_ctx(self, tmp_path) -> None:  # noqa: ANN001
        checkpoint = _checkpoint(tmp_path)
        loop = _ProbeLoop()
        runtime, _ = _runtime(loop)
        await runtime.execute("t", task_id="t-1", loop_inputs={"checkpoint": checkpoint})
        (ctx,) = _SEEN
        assert ctx["loop_inputs"]["checkpoint"] is checkpoint
        with pytest.raises(TypeError):
            ctx["loop_inputs"]["checkpoint"] = None
        # The action negotiation travels with the observation one.
        assert ctx["negotiated_actions"] is runtime.negotiated_actions
        assert ctx["negotiated_obs"] is runtime.negotiated_observation
        # Still stateless: nothing landed on the loop instance.
        assert not hasattr(loop, "ctx")

    async def test_a_loop_without_inputs_runs_with_none(self) -> None:
        runtime, _ = _runtime(ReactLoop())
        agent = runtime.prepare()
        result = await agent.run("t")
        assert result.agent_done == AgentDoneStatus.DONE

    async def test_branch_reaches_on_run_start_by_name(self, tmp_path) -> None:  # noqa: ANN001
        spy = _StartSpy()
        provenance = BranchProvenance(
            source_run_id="src",
            source_trace="src",
            checkpoint_step=1,
            branch_index=0,
            branch_group_id="src/s1",
        )
        runtime, _ = _runtime(_ProbeLoop(), hooks=[spy])
        await runtime.execute(
            "t",
            task_id="src/s1/b0",
            loop_inputs={"checkpoint": _checkpoint(tmp_path)},
            branch=provenance,
        )
        (start,) = spy.starts
        assert start["branch"] is provenance

    async def test_a_plain_run_starts_with_branch_none(self) -> None:
        spy = _StartSpy()
        runtime, _ = _runtime(ReactLoop(), hooks=[spy])
        await runtime.execute("t")
        (start,) = spy.starts
        assert start["branch"] is None


class TestNoUntypedChannel:
    async def test_execute_rejects_a_stray_keyword(self, tmp_path) -> None:  # noqa: ANN001
        runtime, cap = _runtime(_ProbeLoop())
        with pytest.raises(TypeError):
            await runtime.execute("t", checkpoint=_checkpoint(tmp_path))  # type: ignore[call-arg]
        assert cap.setups == 0

    def test_prepare_rejects_a_stray_keyword(self, tmp_path) -> None:  # noqa: ANN001
        runtime, _ = _runtime(_ProbeLoop())
        with pytest.raises(TypeError):
            runtime.prepare(checkpoint=_checkpoint(tmp_path))  # type: ignore[call-arg]

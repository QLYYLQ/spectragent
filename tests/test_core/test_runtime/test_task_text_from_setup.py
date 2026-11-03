"""A run whose setup completes its task text: Runtime reads it after setup, records it, runs on it.

The user's ruling of 2026-11-03 (``docs/architecture_design.md`` §4.2, §4.10).
``prepare()`` asks the environment whose setup completes the task's text
(``task_text_setter``; two are refused). ``execute()`` builds the seed messages
from the loader's text before the trace opens, as for every run, but writes
them only when no setup completes the text. After setup it reads the text
through the woven environment (the trace hook records ``task_text.json``),
seeds the agent with it and runs on it; ``config.json`` keeps the loader's text
and names the setter. Failing to read the text is a failed setup; a seed
builder that fails on it is the agent's error.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.task import SetupTaskText
from spectragent.schemas.verdict import Judgment, VerifierIdentity
from tests._helpers.assembly import AGENT_ID, make_env, make_run

LOADED = "Follow the instructions shown on the top of the screen."
MADE = "Follow the instructions shown on the top of the screen: Click on the Submit button."
CAP = "setup_text_cap"
DONE = f"{CAP}.done"


class _TextCap(CapabilityBase):
    """Completes the task text at setup when *sets*, handing back *text* (or raising it); scores."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = CAP
    __wiring__ = {"action": ("action", [DONE])}

    def __init__(self, *, sets: bool = True, text: object = MADE, **kw: Any) -> None:
        super().__init__(**kw)
        self.sets = sets
        self.text = text
        self.log: list[str] = []

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name=DONE, description="Done")]

    async def execute_action(self, action: Action) -> ActionResult:
        return ActionResult(success=True, is_done=True, extracted_content="answer")

    async def setup(self, *, negotiation: Any = None, goal_seed: int | None = None) -> None:
        self.log.append("setup")
        await super().setup(negotiation=negotiation)

    def sets_task_text(self, context: Mapping[str, Any], *, setup: object = None) -> bool:
        self.log.append(f"sets_task_text {dict(context)}")
        return self.sets

    async def task_text_from_setup(self) -> str | None:
        self.log.append("task_text_from_setup")
        if isinstance(self.text, BaseException):
            raise self.text
        return self.text  # type: ignore[return-value]  # a breach of contract on purpose

    def oracle_identity(self) -> VerifierIdentity | None:
        return VerifierIdentity(verifier_id="setup_text_oracle")

    async def read_score(self, *, answer: str | None) -> Judgment:
        self.log.append("score")
        return Judgment(score=1.0)


class _OtherTextCap(_TextCap):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "other_setup_text_cap"


class _Env(EnvironmentBase):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "setup_text_env"
    __wiring__ = {"capabilities": "capability"}


class _Agent:
    """Seeds with the task it is given and ends the run at once; fails to seed on *refused*."""

    __execution_limit_fields__ = frozenset()
    __wiring__ = {"action": ("action", [DONE])}

    def __init__(self, *, refused: str | None = None) -> None:
        self.refused = refused
        self.seeded: list[str] = []

    def prepare(self, *, task: str, action_space: Any) -> list[Any]:
        self.seeded.append(task)
        if task == self.refused:
            raise KeyError("instruction")
        return [{"role": "system", "content": f"task: {task}"}]

    async def build_react_prompt(self, obs: Any, *, context: ReactPromptContext) -> list[Any]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Action]:
        return [Action(name=DONE)]

    def get_react_output_model(self) -> None:
        return None


class _Completion:
    completion = "done"
    usage = None


class _LLM:
    inference_kind = "chat"
    model = name = "test-model"

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        self.messages = list(messages)
        return _Completion()

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
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


class _StartHook(HookBase):
    """Keeps what ``on_run_start`` was handed."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "setup_text_start_hook"

    async def on_run_start(self, **kw: Any) -> None:
        self.started = kw


def _runtime(
    tmp_path: Path, *caps: CapabilityBase, agent: _Agent | None = None
) -> tuple[AgentRuntime, _Agent, _Memory, _StartHook, TraceWriter]:
    agent = agent if agent is not None else _Agent()
    memory = _Memory()
    start, writer = _StartHook(), TraceWriter(trace_dir=str(tmp_path / "traces"))
    env = make_env(_Env, {cap.capability_id: cap for cap in caps})
    run = make_run(
        agent=agent,
        loop=ReactLoop(max_steps=3),
        env=env,
        llm=_LLM(),
        memory=memory,
        hooks=[start, writer],
    )
    return AgentRuntime.for_agent(run, AGENT_ID), agent, memory, start, writer


async def test_the_agent_runs_on_the_text_its_setup_completed_and_the_trace_keeps_both(
    tmp_path: Path,
) -> None:
    cap = _TextCap()
    runtime, agent, memory, start, writer = _runtime(tmp_path, cap)

    result = await runtime.execute(
        LOADED, task_id="click-button/42/0", env_context={"goal_seed": 7}
    )

    # Seeded once from the loader's text before the trace opened, then from the made one.
    assert agent.seeded == [LOADED, MADE]
    assert memory.messages[0] == {"role": "system", "content": f"task: {MADE}"}
    assert [
        m for m in memory.messages if m == {"role": "system", "content": f"task: {LOADED}"}
    ] == []
    assert result.task == MADE and result.agent_done.value == "done"
    assert (start.started["task"], start.started["task_text_setter"]) == (LOADED, CAP)
    assert cap.log[:3] == ["sets_task_text {'goal_seed': 7}", "setup", "task_text_from_setup"]
    assert writer.run_dir is not None
    trace = RunTrace(writer.run_dir)
    assert (trace.config.task, trace.config.task_text_setter) == (LOADED, CAP)
    assert trace.setup_task_text() == SetupTaskText(capability_id=CAP, text=MADE)
    assert trace.task_text() == MADE
    assert trace.manifest.status.value == "complete"


async def test_a_run_no_setup_completes_is_unchanged(tmp_path: Path) -> None:
    cap = _TextCap(sets=False)
    runtime, agent, memory, start, writer = _runtime(tmp_path, cap)

    result = await runtime.execute(LOADED, task_id="t-1")

    assert agent.seeded == [LOADED]
    assert memory.messages[0] == {"role": "system", "content": f"task: {LOADED}"}
    assert result.task == LOADED
    assert start.started["task_text_setter"] is None
    assert "task_text_from_setup" not in cap.log
    assert writer.run_dir is not None
    trace = RunTrace(writer.run_dir)
    assert (trace.setup_task_text(), trace.config.task_text_setter) == (None, None)
    assert trace.task_text() == LOADED


def test_two_capabilities_that_would_each_complete_it_are_refused_in_prepare(
    tmp_path: Path,
) -> None:
    first, second = _TextCap(), _OtherTextCap()
    runtime, *_ = _runtime(tmp_path, first, second)
    with pytest.raises(ValueError, match="a run has one task text"):
        runtime.prepare(task_id="t-1")
    assert "setup" not in first.log + second.log


async def test_a_setup_that_completed_no_text_after_saying_it_would_is_a_failed_setup(
    tmp_path: Path,
) -> None:
    cap = _TextCap(text=None)
    runtime, agent, *_ = _runtime(tmp_path, cap)

    result = await runtime.execute(LOADED, task_id="t-1", raise_on_error=False)

    assert (result.agent_done.value, result.env_status.value) == ("error", "failed")
    assert result.error == (
        f"Environment setup failed: RuntimeError: capability {CAP!r} said its setup would "
        "complete the run's task text, and its setup completed none"
    )
    assert "score" not in cap.log, "a failed setup is not scored"
    assert agent.seeded == [LOADED]


async def test_reading_the_text_fails_as_setup_fails(tmp_path: Path) -> None:
    cap = _TextCap(text=ConnectionError("the service is gone"))
    runtime, *_ = _runtime(tmp_path, cap)

    result = await runtime.execute(LOADED, task_id="t-1", raise_on_error=False)

    assert (result.agent_done.value, result.env_status.value) == ("error", "failed")
    assert result.error == "Environment setup failed: ConnectionError: the service is gone"
    assert "score" not in cap.log


async def test_a_seed_builder_that_fails_on_the_completed_text_is_the_agents_error(
    tmp_path: Path,
) -> None:
    """Not the environment's: its prompt builder failed on the text, on a healthy device."""
    cap = _TextCap()
    runtime, agent, *_ = _runtime(tmp_path, cap, agent=_Agent(refused=MADE))

    result = await runtime.execute(LOADED, task_id="t-1", raise_on_error=False)

    assert (result.agent_done.value, result.env_status.value) == ("error", "success")
    assert result.error == (
        "RuntimeError: the agent's seed messages could not be built from the task text its "
        "setup completed: KeyError: 'instruction'"
    )
    assert cap.log.count("score") == 1, "scored, as any run whose Loop failed"
    assert agent.seeded == [LOADED, MADE]

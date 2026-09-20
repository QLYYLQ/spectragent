"""ReactLoop — a reply cut off at the token ceiling ends the run, honestly.

This is the opposite treatment from a ``ParseError``. A parse failure is the
model's mistake and gets re-asked; a truncation is our configuration and
cannot be re-asked out of existence — the ceiling is unchanged and the
history is longer, so every retry truncates sooner while costing a
maximum-length completion.

The trace half matters as much as the money: routed through the parse path a
run died reporting "Parse failed 3 consecutive times", which on an evaluation
platform charges our own budget to the model under test.
"""

from __future__ import annotations

import asyncio
from typing import Any

from spectragent.core.llm.exceptions import ModelOutputTruncatedError
from spectragent.core.llm.views import ChatInvokeUsage
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.react import ReactLoop
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult


class _DummyObs:
    pass


class _FakeEnv:
    env_type = "fake"

    def __init__(self) -> None:
        self.observe_calls = 0

    async def observe(self, *_a: Any, **_kw: Any) -> _DummyObs:
        self.observe_calls += 1
        return _DummyObs()

    async def act(self, action: Action, **kw: Any) -> ActionResult:
        return ActionResult(success=True)

    def get_action_space(self, **kw: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name="done", description="Done")]


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


_TRUNCATED_USAGE = ChatInvokeUsage(
    prompt_tokens=12000,
    prompt_cached_tokens=8000,
    prompt_cache_creation_tokens=1500,
    prompt_image_tokens=None,
    completion_tokens=8192,
    total_tokens=20192,
)


class _TruncatingLLM:
    """Every call hits the ceiling — which is what a too-small ceiling does."""

    model = "claude-test"

    def __init__(self, usage: ChatInvokeUsage | None = _TRUNCATED_USAGE) -> None:
        self.calls = 0
        self._usage = usage

    async def ainvoke(self, messages: Any, **kw: Any) -> Any:
        self.calls += 1
        raise ModelOutputTruncatedError(
            model=self.model,
            stop_reason="max_tokens",
            ceiling_field="max_tokens",
            ceiling=8192,
            usage=self._usage,
        )


class _Agent:
    __wiring__: dict = {}

    def __init__(self) -> None:
        self.parse_calls = 0

    def build_react_prompt(self, obs: Any) -> list:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        self.parse_calls += 1
        raise ParseError("unparseable", raw_output="{half-writt")

    def get_react_output_model(self) -> None:
        return None


class _Recorder:
    """The step sink. ``AgentResult`` carries a step *count*, not the records,
    so what reached the trace is only observable from here."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    async def record_step(self, record: Any, observation: Any = None) -> None:
        self.records.append(record)


def _run(
    llm: _TruncatingLLM,
    agent: _Agent,
    env: _FakeEnv,
    recorder: _Recorder | None = None,
) -> Any:
    loop = ReactLoop()
    loop.max_steps = 10
    loop.max_consecutive_parse_errors = 3
    ctx = {
        "env": env,
        "llm": llm,
        "memory": _FakeMemory(),
        "act_fn": env.act,
        "task_id": "t",
        "step_recorder": recorder,
    }
    return asyncio.run(loop._execute(agent, "test", ctx=ctx))


class TestTruncationEndsTheRun:
    def test_it_does_not_burn_the_parse_error_budget(self) -> None:
        """One call, not ``max_consecutive_parse_errors`` of them.

        Each avoided attempt is a maximum-length completion plus a fresh
        observation, so the difference here is the whole point of the fix.
        """
        llm, agent, env = _TruncatingLLM(), _Agent(), _FakeEnv()
        _run(llm, agent, env)
        assert llm.calls == 1
        assert env.observe_calls == 1

    def test_the_reply_never_reaches_the_agent_parser(self) -> None:
        """Half-written JSON is not a parsing problem and must not look like one."""
        llm, agent, env = _TruncatingLLM(), _Agent(), _FakeEnv()
        _run(llm, agent, env)
        assert agent.parse_calls == 0

    def test_the_error_names_the_ceiling_not_the_model(self) -> None:
        llm, agent, env = _TruncatingLLM(), _Agent(), _FakeEnv()
        result = _run(llm, agent, env)
        assert result.error is not None
        assert "max_tokens=8192" in result.error
        assert "Parse failed" not in result.error

    def test_the_environment_is_not_blamed(self) -> None:
        """A model-side failure is not a dead browser."""
        llm, agent, env = _TruncatingLLM(), _Agent(), _FakeEnv()
        result = _run(llm, agent, env)
        assert result.agent_done.value == "error"
        assert result.env_status.value == "success"


class TestTruncationIsStillBilled:
    def test_the_tokens_reach_the_run_total(self) -> None:
        """The most expensive call of the run must not be recorded as free."""
        llm, agent, env = _TruncatingLLM(), _Agent(), _FakeEnv()
        result = _run(llm, agent, env)

        usage = result.metrics.usage
        assert usage is not None, "a billed call was recorded as 'not measured'"
        assert usage.total_completion_tokens == 8192
        assert usage.total_prompt_cache_creation_tokens == 1500

    def test_the_step_record_carries_the_cost(self) -> None:
        llm, agent, env, recorder = _TruncatingLLM(), _Agent(), _FakeEnv(), _Recorder()
        _run(llm, agent, env, recorder)

        assert recorder.records, "the interrupted step never reached the trace"
        record = recorder.records[-1]
        assert record.error_phase == "llm"
        assert record.token_usage is not None
        assert record.token_usage.completion_tokens == 8192

    def test_a_provider_reporting_nothing_stays_not_measured(self) -> None:
        """``None`` means unmeasured; a zero-filled total would be a false claim."""
        llm, agent, env = _TruncatingLLM(usage=None), _Agent(), _FakeEnv()
        result = _run(llm, agent, env)
        assert result.metrics.usage is None

"""ReactLoop — a reply cut off at the token ceiling, under each ``cut_off_reply``.

By default (``end_run``) it ends the run, honestly. This is the opposite
treatment from a ``ParseError``. A parse failure is the model's mistake and
gets re-asked; a truncation is our configuration and cannot be re-asked out
of existence — the ceiling is unchanged and the history is longer, so every
retry truncates sooner while costing a maximum-length completion.

The trace half matters as much as the money: routed through the parse path a
run died reporting "Parse failed 3 consecutive times", which on an evaluation
platform charges our own budget to the model under test.

``read_as_is`` is what a benchmark's own harness does (AndroidWorld reads the
content whatever the finish reason): the text the reply had generated goes to
the agent's parser like any reply, and one it cannot read is a parse failure
like any other, re-asked by ``parse_retries_per_step`` and counted by
``max_consecutive_parse_errors``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from spectragent.core.llm.exceptions import ModelOutputTruncatedError
from spectragent.core.llm.views import ChatInvokeCompletion, ModelInvokeUsage
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.react import ReactLoop
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.action_space import ActionSpace


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

    async def episode_over(self) -> bool:
        return False


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


_TRUNCATED_USAGE = ModelInvokeUsage(
    prompt_tokens=12000,
    prompt_cached_tokens=8000,
    prompt_cache_creation_tokens=1500,
    prompt_image_tokens=None,
    completion_tokens=8192,
    total_tokens=20192,
)


class _TruncatingLLM:
    """Every call hits the ceiling — which is what a too-small ceiling does."""

    inference_kind = "chat"

    model = "claude-test"

    def __init__(self, usage: ModelInvokeUsage | None = _TRUNCATED_USAGE) -> None:
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

    __execution_limit_fields__ = frozenset()

    async def build_react_prompt(self, obs: Any, *, context) -> list:
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
        "action_space": ActionSpace(),
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


class TestTruncationWithoutInstrumentation:
    def test_loop_leaves_usage_unmeasured(self) -> None:
        """Without a recording component, usage remains unmeasured."""
        llm, agent, env = _TruncatingLLM(), _Agent(), _FakeEnv()
        result = _run(llm, agent, env)

        assert result.metrics.usage is None
        assert result.metrics.llm_calls is None

    def test_step_preserves_failure_without_inventing_usage(self) -> None:
        llm, agent, env, recorder = _TruncatingLLM(), _Agent(), _FakeEnv(), _Recorder()
        _run(llm, agent, env, recorder)

        assert recorder.records, "the interrupted step never reached the trace"
        record = recorder.records[-1]
        assert record.error_phase == "llm"
        assert record.token_usage is None

    def test_a_provider_reporting_nothing_stays_not_measured(self) -> None:
        """``None`` means unmeasured; a zero-filled total would be a false claim."""
        llm, agent, env = _TruncatingLLM(usage=None), _Agent(), _FakeEnv()
        result = _run(llm, agent, env)
        assert result.metrics.usage is None


# ── cut_off_reply: read_as_is ────────────────────────────────────────


def _cut_off(partial_output: str | None) -> ModelOutputTruncatedError:
    """The reply DeepSeek gave when its reasoning spent the whole budget, or one with *partial_output*."""
    return ModelOutputTruncatedError(
        model="scripted",
        stop_reason="length",
        ceiling_field="max_completion_tokens",
        ceiling=32768,
        usage=_TRUNCATED_USAGE,
        partial_output=partial_output,
    )


class _ScriptedLLM:
    """Answers each call with the next reply of its script; an exception in it is raised."""

    inference_kind = "chat"
    model = "scripted"

    def __init__(self, *replies: str | BaseException) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def ainvoke(self, messages: Any, **kw: Any) -> ChatInvokeCompletion[str]:
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return ChatInvokeCompletion(completion=reply, usage=None)


class _ReadingAgent:
    """Reads ``done <answer>``, and nothing else; keeps every text its parser was handed."""

    __wiring__: dict = {}
    __execution_limit_fields__ = frozenset()

    def __init__(self) -> None:
        self.read: list[Any] = []

    async def build_react_prompt(self, obs: Any, *, context) -> list:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        self.read.append(llm_output)
        verb, _, answer = str(llm_output).partition(" ")
        if verb != "done":
            raise ParseError(f"cannot read {llm_output!r}")
        return [Action(name="done", params={"extracted_content": answer})]

    def get_react_output_model(self) -> None:
        return None


async def _act(action: Action) -> ActionResult:
    return ActionResult(
        success=True, is_done=True, extracted_content=action.params["extracted_content"]
    )


def _read_as_is(
    llm: _ScriptedLLM, agent: _ReadingAgent, env: _FakeEnv, recorder: _Recorder, **loop: Any
) -> Any:
    ctx = {
        "env": env,
        "llm": llm,
        "memory": _FakeMemory(),
        "act_fn": _act,
        "action_space": ActionSpace(),
        "task_id": "t",
        "step_recorder": recorder,
    }
    react = ReactLoop(**{"max_steps": 10, "cut_off_reply": "read_as_is", **loop})
    return asyncio.run(react._execute(agent, "test", ctx=ctx))


class TestReadAsIs:
    def test_the_default_ends_the_run(self) -> None:
        """``end_run`` is the default: the classes above run the Loop as constructed."""
        assert ReactLoop().cut_off_reply == "end_run"

    def test_the_cut_off_text_is_read_and_its_unreadable_reply_spends_the_step(self) -> None:
        """The parser reads what the reply had generated; the run goes on to the next step."""
        llm, agent, env, recorder = (
            _ScriptedLLM(_cut_off("Reason: the slider is at -75, so"), "done ok"),
            _ReadingAgent(),
            _FakeEnv(),
            _Recorder(),
        )
        result = _read_as_is(llm, agent, env, recorder, max_consecutive_parse_errors=None)

        assert (result.agent_done.value, result.answer) == ("done", "ok")
        assert agent.read == ["Reason: the slider is at -75, so", "done ok"]
        assert (llm.calls, env.observe_calls) == (2, 2)
        spent, done = recorder.records
        assert spent.error_phase == "parse" and spent.actions == []
        assert spent.error == "cannot read 'Reason: the slider is at -75, so'"
        assert spent.llm_output == "Reason: the slider is at -75, so"
        assert done.error_phase == ""

    def test_a_reply_that_was_all_reasoning_reaches_the_parser_empty(self) -> None:
        """No text (``None``) and empty text (``""``) are both the empty reply the parser gets."""
        llm, agent, env, recorder = (
            _ScriptedLLM(_cut_off(None), _cut_off(""), "done ok"),
            _ReadingAgent(),
            _FakeEnv(),
            _Recorder(),
        )
        result = _read_as_is(llm, agent, env, recorder)

        assert result.agent_done.value == "done"
        assert agent.read == ["", "", "done ok"]
        assert [record.llm_output for record in recorder.records] == ["", "", "done ok"]

    def test_parse_retries_re_ask_a_cut_off_reply(self) -> None:
        llm, agent, env, recorder = (
            _ScriptedLLM(_cut_off(None), "done ok"),
            _ReadingAgent(),
            _FakeEnv(),
            _Recorder(),
        )
        result = _read_as_is(llm, agent, env, recorder, parse_retries_per_step=1)

        assert result.agent_done.value == "done"
        assert (llm.calls, env.observe_calls, len(recorder.records)) == (2, 1, 1)

    def test_the_consecutive_error_limit_counts_it(self) -> None:
        """Cut-off replies are unreadable steps like any other, and the streak ends the run."""
        llm, agent, env, recorder = (
            _ScriptedLLM(*(_cut_off(None) for _ in range(5))),
            _ReadingAgent(),
            _FakeEnv(),
            _Recorder(),
        )
        result = _read_as_is(llm, agent, env, recorder, max_consecutive_parse_errors=2)

        assert result.agent_done.value == "error"
        assert result.error == "Parse failed 2 consecutive times: cannot read ''"
        assert llm.calls == 2
        assert [record.error_phase for record in recorder.records] == ["parse", "parse"]

    def test_cut_off_text_that_reads_runs(self) -> None:
        """Read like any reply: text the parser can read runs as written."""
        llm, agent, env, recorder = (
            _ScriptedLLM(_cut_off("done early")),
            _ReadingAgent(),
            _FakeEnv(),
            _Recorder(),
        )
        result = _read_as_is(llm, agent, env, recorder)

        assert (result.agent_done.value, result.answer) == ("done", "early")

"""End-to-end acceptance for the run lifecycle and complete step records: a trace you can actually evaluate.

Drives a real ``AgentRuntime`` with the real ``TraceWriter`` and
``MetricsCollector``, then reads the result back through ``RunTrace`` —
the same way an Evaluator will. Every assertion here corresponds to
something that was broken or absent on disk across all 14 legacy traces.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    ScreenshotObs,
)
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.metrics_collector import MetricsCollector
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.exceptions import (
    ModelOutputTruncatedError,
    StructuredOutputValidationError,
)
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage
from spectragent.core.loops import ReactLoop
from spectragent.core.loops._loop_fragments import LOOP_FRAGMENT_KEYS
from spectragent.core.loops.errors import ParseError
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.models import TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation.fragments import TaskObs
from tests._helpers.assembly import AGENT_ID, make_env, make_run

_PNG = b"\x89PNG\r\n\x1a\n-pretend-this-is-a-screenshot"


class _Cap(CapabilityBase):
    __registry_key__ = "trace_e2e_cap"
    __wiring__ = {
        "action": ("action", ["trace_e2e_cap.done", "trace_e2e_cap.click"]),
        "observation": (
            "observation",
            ["browser.screenshot", "browser.browser_meta"],
        ),
    }

    def get_action_descriptors(self, **ctx):
        return [
            ActionDescriptor(name="trace_e2e_cap.done", description="Done"),
            ActionDescriptor(name="trace_e2e_cap.click", description="Click"),
        ]

    async def execute_action(self, action, **kw):
        if action.name.endswith("done"):
            return ActionResult(success=True, is_done=True, extracted_content="42 dollars")
        return ActionResult(success=True, extracted_content="clicked")

    async def resolve_fragment(self, fragment_key, **kwargs):
        if fragment_key == "browser.screenshot":
            return ScreenshotObs(image_bytes=_PNG, viewport_width=1280, viewport_height=720)
        if fragment_key == "browser.browser_meta":
            return BrowserMetaObs(url="https://example.com", page_title="Example")
        return None


class _Env(EnvironmentBase):
    __registry_key__ = "trace_e2e_env"


class _TeardownRecordingEnv(EnvironmentBase):
    __registry_key__ = "trace_e2e_teardown_env"

    torn_down = False

    async def teardown(self) -> None:
        self.torn_down = True
        await super().teardown()


class _Agent:
    """Declares and emits capability-relative names, like every shipped agent.

    ``done``/``click``, not ``trace_e2e_cap.done`` — so the path this
    file reads back off disk is the one ``build_guarded_act`` resolves.
    A fixture that hands the guard an already-qualified name never
    exercises the binding, and cannot see it drop the prefix.
    """

    __wiring__ = {
        "action": ("action", ["done", "click"]),
        "observation": (
            "observation",
            ["browser.screenshot", "browser.browser_meta", "loop.task"],
        ),
    }

    def __init__(self, plan: list[str]):
        self._plan = plan
        self._i = 0

    def build_react_prompt(self, obs):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out):
        name = self._plan[min(self._i, len(self._plan) - 1)]
        self._i += 1
        return [Action(name=name, params={"index": 1})]

    def get_react_output_model(self):
        return None


class _Usage:
    prompt_tokens = 120
    completion_tokens = 30
    prompt_cached_tokens = 0
    total_tokens = 150


class _Completion:
    completion = '{"action": "done"}'
    usage = _Usage()


class _LLM:
    model = "gpt-4o-mini"

    async def ainvoke(self, messages, **kw):
        return _Completion()


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

    def __init__(self):
        self.messages: list[Any] = []

    def add_message(self, m):
        self.messages.append(m)

    def add_messages(self, ms):
        self.messages.extend(ms)

    def get_context_messages(self):
        return list(self.messages)

    def clear(self):
        self.messages.clear()


class _SlowLoop:
    loop_id = "trace_e2e_slow"
    __required_slots__: set[str] = set()
    __optional_slots__: set[str] = set()
    __requires_env__ = True
    __loop_fragments__ = LOOP_FRAGMENT_KEYS

    def _validate_slots(self, agent):
        pass

    def check_loop_inputs(self, inputs):
        pass

    def weave(self, agent, **deps):
        async def run(task, **kw):
            await asyncio.sleep(100)

        agent.run = run
        return agent


class _StallAfterLLMLoop:
    """Gets an answer, then dies before surrendering the step.

    The real timeout shape: ``around_ainvoke`` has already recorded a
    prompt+completion when ``asyncio.wait_for`` cancels the loop, so
    ``record_step`` — the commit point — is never reached.
    """

    loop_id = "trace_e2e_stall"
    __required_slots__: set[str] = set()
    __optional_slots__: set[str] = set()
    __requires_env__ = True
    __loop_fragments__ = LOOP_FRAGMENT_KEYS

    def _validate_slots(self, agent):
        pass

    def check_loop_inputs(self, inputs):
        pass

    def weave(self, agent, **deps):
        llm = deps["llm"]

        async def run(task, **kw):
            await llm.ainvoke([{"role": "user", "content": task}])
            await asyncio.sleep(100)  # e.g. an action that never returns

        agent.run = run
        return agent


class _CrashAfterLLMLoop(_StallAfterLLMLoop):
    """Same window, closed by an exception instead of the timeout."""

    loop_id = "trace_e2e_crash"

    def weave(self, agent, **deps):
        llm = deps["llm"]

        async def run(task, **kw):
            await llm.ainvoke([{"role": "user", "content": task}])
            raise ValueError("could not parse that completion")

        agent.run = run
        return agent


class _HangingLLM:
    """A model that never answers — until something cancels the call."""

    model = "gpt-4o-mini"

    def __init__(self) -> None:
        self.called = asyncio.Event()

    async def ainvoke(self, messages, **kw):
        self.called.set()
        await asyncio.Event().wait()


def _run_with_hooks(tmp_path, *, plan, loop=None, hooks=None, llm=None, env=None, agent=None):
    writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
    # Declared in the order that used to zero out every result.json.
    hook_list = hooks if hooks is not None else [writer, MetricsCollector()]
    run = make_run(
        agent=agent or _Agent(plan),
        loop=loop or ReactLoop(max_steps=3),
        env=env or make_env(_Env, {"trace_e2e_cap": _Cap()}),
        llm=llm or _LLM(),
        memory=_Memory(),
        hooks=hook_list,
    )
    return run, writer


class TestCompletedRunTrace:
    @pytest.fixture()
    async def trace(self, tmp_path):
        run, writer = _run_with_hooks(tmp_path, plan=["click", "done"])
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price",
            task_id="mind2web--0042",
            benchmark="online_mind2web",
            subset="Hard",
            timeout=10,
        )
        assert writer.run_dir is not None
        return RunTrace(writer.run_dir), result

    async def test_identity_is_joinable(self, trace):
        t, _ = trace
        cfg = t.config
        assert cfg.benchmark == "online_mind2web"
        assert cfg.subset == "Hard"
        assert cfg.task_id == "mind2web--0042"
        assert cfg.llm_model == "gpt-4o-mini"
        assert cfg.loop_id == "react"
        assert cfg.env_type == "trace_e2e_env"

    async def test_manifest_complete_with_result(self, trace):
        t, _ = trace
        assert t.manifest.status == TraceStatus.COMPLETE
        assert t.result is not None
        assert t.result.answer == "42 dollars"
        assert t.result.agent_reported_success is True

    async def test_step_records_are_complete(self, trace):
        t, _ = trace
        rec = t.step(0)
        assert rec is not None
        assert rec.step == 0
        assert rec.actions[0].name == "trace_e2e_cap.click"
        assert rec.action_results[0].extracted_content == "clicked"
        assert rec.token_usage is not None
        assert rec.token_usage.total_tokens == 150
        assert rec.observation_summary
        assert t.step_count == 2

    async def test_screenshots_are_real_png_files(self, trace):
        t, _ = trace
        assert t.screenshots, "RunTrace.screenshots must not be empty"
        assert t.screenshots[0].read_bytes() == _PNG
        shot = t.observation_fragment(0, "browser.screenshot")
        assert isinstance(shot, ScreenshotObs)
        assert shot.viewport_width == 1280
        assert shot.image_bytes == _PNG
        files = t.manifest.steps[0].fragments["browser.screenshot"]
        sidecar = json.loads((t.trace_dir / files.file).read_text())
        assert "image_bytes" not in sidecar

    async def test_fragment_files_carry_their_producer(self, trace):
        """Provenance survives the round trip to disk, twice over.

        The file name is the fully-qualified key and the JSON repeats it
        in ``fragment_id``. Before this, ``fragment_id`` was a
        ``@property`` (so ``model_dump`` dropped it) and the file was
        named after the negotiated local name, so a trace could not say
        which capability produced a fragment — and an evaluator reading a
        ``RunTrace`` is the only reader these files have.
        """
        t, _ = trace
        written = set(t.manifest.steps[0].fragments)
        assert "loop.task" in written
        assert "browser.screenshot" in written
        assert all("." in key for key in written), written
        assert all(
            files.file == f"observations/000_{key}.json"
            for key, files in t.manifest.steps[0].fragments.items()
        )

        task = t.observation_fragment(0, "loop.task")
        assert isinstance(task, TaskObs)
        assert task.user_request == "find the price"
        assert task.fragment_id == "loop.task"
        # And the junk 0.0 timestamp file is gone.
        assert "timestamp" not in written

    async def test_every_call_is_recorded_with_its_step(self, trace):
        t, _ = trace
        for step in range(t.step_count):
            (call,) = t.llm_messages(step)
            assert (call.step, call.status) == (step, "completed")
            assert call.completion is not None
            assert call.completion.completion == '{"action": "done"}'
            assert call.completion.usage is not None
            assert call.completion.usage.total_tokens == 150
        assert t.trailing_calls == []

    async def test_recorded_actions_carry_their_producer(self, trace):
        """The action axis keeps provenance too — same rule, same trace.

        The agent declares and emits ``click``; ``build_guarded_act`` is
        the one place that becomes ``trace_e2e_cap.click``, and it has to
        resolve the object the loop records, not a copy of it. Otherwise
        a reader holding only the trace sees a provider-less verb for an
        action some specific capability executed, while the observation
        files right next to it name theirs.
        """
        t, _ = trace
        recorded = [a.name for rec in t.step_records for a in rec.actions]
        assert recorded == ["trace_e2e_cap.click", "trace_e2e_cap.done"]
        assert all("." in name for name in recorded), recorded

        assert t.result is not None
        assert [entry.split("(")[0] for entry in t.result.action_history] == recorded

    async def test_tokens_recorded_despite_hook_order(self, trace):
        t, _ = trace
        assert t.result is not None
        usage = t.result.metrics.usage
        assert usage is not None
        assert usage.total_tokens == 300  # 2 steps x 150
        assert usage.by_model is not None
        assert usage.by_model["gpt-4o-mini"]["calls"] == 2
        assert t.result.metrics.steps == 2
        assert t.result.metrics.llm_calls == 2
        assert t.result.model_id == "gpt-4o-mini"


class TestTimedOutRunTrace:
    """The acceptance case: a run that dies must still leave a full trace."""

    async def test_timeout_leaves_a_closed_trace(self, tmp_path):
        run, writer = _run_with_hooks(tmp_path, plan=["done"], loop=_SlowLoop())
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price",
            task_id="mind2web--0042",
            benchmark="online_mind2web",
            subset="Hard",
            timeout=0.05,
        )

        assert writer.run_dir is not None
        t = RunTrace(writer.run_dir)
        # Closed, not 'writing' — and complete: the timeout is the agent's ledger.
        assert t.manifest.status == TraceStatus.COMPLETE
        assert t.result is not None
        assert t.config.benchmark and t.config.task_id
        assert t.config.llm_model and t.config.loop_id
        assert result.agent_done.value == "timeout"
        assert json.loads((t.trace_dir / "result.json").read_text())["agent_done"] == ("timeout")


class TestAbnormalEndKeepsTheLastExchange:
    """The trailing turn of a dead run is the one you most need to read.

    Buffered LLM calls used to be dropped whenever the run ended without
    one more ``record_step`` — which is every timeout and every crash.
    """

    async def test_timeout_commits_the_answered_call_as_trailing(self, tmp_path):
        run, writer = _run_with_hooks(tmp_path, plan=["done"], loop=_StallAfterLLMLoop())
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price", task_id="mind2web--0042", timeout=0.05
        )
        assert result.agent_done.value == "timeout"
        assert writer.run_dir is not None

        t = RunTrace(writer.run_dir)
        (call,) = t.trailing_calls
        assert (call.step, call.call, call.status) == (0, 0, "completed")
        assert [m.text for m in call.messages] == ["find the price"]
        assert call.completion is not None
        assert call.completion.completion == '{"action": "done"}'
        # Recorded as trailing, not as a step that never happened.
        assert t.step_count == 0
        assert t.llm_messages(0) == []
        assert t.manifest.status == TraceStatus.COMPLETE

    async def test_exception_commits_the_answered_call_as_trailing(self, tmp_path):
        run, writer = _run_with_hooks(tmp_path, plan=["done"], loop=_CrashAfterLLMLoop())
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price",
            task_id="mind2web--0042",
            timeout=10,
            raise_on_error=False,
        )
        assert result.agent_done.value == "error"
        assert writer.run_dir is not None

        t = RunTrace(writer.run_dir)
        assert [m.text for m in t.trailing_calls[0].messages] == ["find the price"]
        assert t.manifest.status == TraceStatus.COMPLETE


class TestACancelledCallKeepsItsPrompt:
    """The call in flight when a run is cut off is the one most worth reading.

    ``CancelledError`` is a ``BaseException``: no ``after`` advice sees the
    call, and recording it only in ``except Exception`` lost the prompt.
    TraceWriter registers every call before it goes out, and the runtime
    closes the run out on cancellation too — so the prompt lands as a
    trailing call, ``cancelled``, whichever side did the cancelling.
    """

    @staticmethod
    def _assert_the_prompt_survived(t: RunTrace) -> None:
        assert t.manifest.status == TraceStatus.COMPLETE
        assert t.step_count == 0
        (call,) = t.trailing_calls
        assert (call.step, call.call, call.status) == (0, 0, "cancelled")
        assert call.completion is None
        assert call.failure is not None
        assert call.failure.error == "CancelledError"
        assert [m.text for m in call.messages] == ["go"]

    async def test_cut_off_by_the_runs_own_timeout(self, tmp_path):
        llm = _HangingLLM()
        env = make_env(_TeardownRecordingEnv, {"trace_e2e_cap": _Cap()})
        run, writer = _run_with_hooks(tmp_path, plan=["done"], llm=llm, env=env)
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price", task_id="mind2web--0042", timeout=0.1
        )

        assert result.agent_done.value == "timeout"
        assert writer.run_dir is not None
        self._assert_the_prompt_survived(RunTrace(writer.run_dir))
        assert env.torn_down

    async def test_cut_off_by_an_external_cancel(self, tmp_path):
        llm = _HangingLLM()
        env = make_env(_TeardownRecordingEnv, {"trace_e2e_cap": _Cap()})
        run, writer = _run_with_hooks(tmp_path, plan=["done"], llm=llm, env=env)
        task = asyncio.create_task(
            AgentRuntime.for_agent(run, AGENT_ID).execute(
                "find the price", task_id="mind2web--0042", timeout=30
            )
        )
        await llm.called.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert writer.run_dir is not None
        t = RunTrace(writer.run_dir)
        self._assert_the_prompt_survived(t)
        assert t.result is not None
        assert t.result.error == "Execution cancelled"
        assert env.torn_down


class _Reply(BaseModel):
    """The reply model the parsing agent asks for, so every call carries a schema."""

    action: str


class _ParsingAgent(_Agent):
    """Parses the way a shipped agent does: a reply naming no known verb is a ParseError."""

    def get_react_output_model(self):
        return _Reply

    def parse_react_output(self, out):
        if isinstance(out, _Reply):
            name = out.action
        else:
            try:
                name = json.loads(out)["action"]
            except (TypeError, ValueError, KeyError):
                raise ParseError(f"not a reply: {out!r}") from None
        if name not in ("done", "click"):
            raise ParseError(f"unknown action {name!r}")
        return [Action(name=name, params={"index": 1})]


#: Script entry for :class:`_ScriptedLLM`: never answer, until cancelled.
_HANG = object()


class _ScriptedLLM:
    """Answers each call from a script: a completion, an exception to raise, or ``_HANG``."""

    model = "gpt-4o-mini"

    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.hanging = asyncio.Event()

    async def ainvoke(self, messages, **kw):
        answer = self._script.pop(0)
        if answer is _HANG:
            self.hanging.set()
            await asyncio.Event().wait()
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _billed(completion_tokens: int) -> ChatInvokeUsage:
    return ChatInvokeUsage(
        prompt_tokens=100,
        prompt_cached_tokens=0,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=completion_tokens,
        total_tokens=100 + completion_tokens,
    )


def _refused() -> StructuredOutputValidationError:
    """The schema rejected the reply; the Loop hands the model its own text back."""
    return StructuredOutputValidationError(
        "reply does not match _Reply",
        raw_output='{"action": 7}',
        model="gpt-4o-mini",
        usage=_billed(5),
    )


def _unusable() -> ChatInvokeCompletion:
    """A reply the schema accepts and the agent cannot act on: a parse retry."""
    return ChatInvokeCompletion(
        completion=_Reply(action="scroll"), usage=_billed(6), stop_reason="end_turn"
    )


def _truncated() -> ModelOutputTruncatedError:
    return ModelOutputTruncatedError(
        model="gpt-4o-mini",
        stop_reason="length",
        ceiling=64,
        usage=_billed(64),
        partial_output='{"action": "cli',
    )


class TestEveryCallOfAStepThroughTheRealLoop:
    """One ReactLoop step, several calls — each one readable, in the order it was made.

    The step re-asks in place (``parse_retries_per_step``): a structured reply
    the schema refused, then one the agent could not use, then a third that
    the token ceiling cut off (which aborts the step) or that a cancellation
    cut off (which ends the run). Real Loop, real Weaver, real TraceWriter,
    read back through ``RunTrace``.
    """

    @staticmethod
    def _run(tmp_path, llm: _ScriptedLLM, **kwargs: Any) -> tuple[Any, TraceWriter]:
        return _run_with_hooks(
            tmp_path,
            plan=["done"],
            agent=_ParsingAgent(["done"]),
            loop=ReactLoop(max_steps=3, parse_retries_per_step=2),
            llm=llm,
            **kwargs,
        )

    @staticmethod
    def _assert_the_first_two_attempts(calls: list[Any]) -> None:
        refused, unusable = calls[:2]
        assert refused.failure is not None
        assert refused.failure.error == "StructuredOutputValidationError"
        assert refused.failure.raw_output == '{"action": 7}'
        assert refused.completion is None
        assert unusable.completion is not None
        assert unusable.completion.completion == {"action": "scroll"}
        # Each attempt was asked with the feedback the one before it earned:
        # the recorded prompt is the one sent, not the memory at commit time.
        assert [len(call.messages) for call in calls] == [1, 3, 5][: len(calls)]
        assert [m.role for m in calls[1].messages] == ["user", "assistant", "user"]
        assert "could not be parsed" in calls[1].messages[-1].text

    async def test_a_committed_step_keeps_every_call_in_start_order(self, tmp_path):
        llm = _ScriptedLLM(_refused(), _unusable(), _truncated())
        run, writer = self._run(tmp_path, llm)
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price", task_id="mind2web--0042", timeout=10
        )

        assert result.agent_done.value == "error"
        assert writer.run_dir is not None
        t = RunTrace(writer.run_dir)
        assert t.step_count == 1
        assert t.trailing_calls == []
        calls = t.llm_messages(0)
        assert [(c.step, c.call, c.status) for c in calls] == [
            (0, 0, "failed"),
            (0, 1, "completed"),
            (0, 2, "failed"),
        ]
        self._assert_the_first_two_attempts(calls)
        truncated = calls[2]
        assert truncated.failure is not None
        assert truncated.failure.error == "ModelOutputTruncatedError"
        assert truncated.failure.partial_output == '{"action": "cli'
        assert truncated.failure.stop_reason == "length"
        assert truncated.failure.raw_output is None

        # Every call asked for the same reply model, committed once.
        (schema_sha,) = t.manifest.output_formats
        assert {c.output_format.sha256 for c in calls if c.output_format} == {schema_sha}
        assert t.output_schema(schema_sha)["title"] == "_Reply"
        # The calls account for what the step says it cost.
        billed = 0
        for call in calls:
            outcome = call.failure or call.completion
            assert outcome is not None
            assert outcome.usage is not None
            billed += outcome.usage.total_tokens
        step = t.step(0)
        assert step is not None
        assert step.error_phase == "llm"
        assert step.token_usage is not None
        assert step.token_usage.total_tokens == billed
        assert t.manifest.status == TraceStatus.COMPLETE

    async def test_a_step_cut_off_by_a_cancel_keeps_every_call_as_trailing(self, tmp_path):
        llm = _ScriptedLLM(_refused(), _unusable(), _HANG)
        env = make_env(_TeardownRecordingEnv, {"trace_e2e_cap": _Cap()})
        run, writer = self._run(tmp_path, llm, env=env)
        task = asyncio.create_task(
            AgentRuntime.for_agent(run, AGENT_ID).execute(
                "find the price", task_id="mind2web--0042", timeout=30
            )
        )
        await llm.hanging.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert writer.run_dir is not None
        t = RunTrace(writer.run_dir)
        assert t.step_count == 0
        assert t.llm_messages(0) == []
        calls = t.trailing_calls
        assert [(c.step, c.call, c.status) for c in calls] == [
            (0, 0, "failed"),
            (0, 1, "completed"),
            (0, 2, "cancelled"),
        ]
        self._assert_the_first_two_attempts(calls)
        cancelled = calls[2]
        assert cancelled.completion is None
        assert cancelled.failure is not None
        assert cancelled.failure.error == "CancelledError"
        assert [m.role for m in cancelled.messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
        assert cancelled.output_format is not None
        assert cancelled.output_format.sha256 in t.manifest.output_formats
        assert t.result is not None
        assert t.result.error == "Execution cancelled"
        assert t.manifest.status == TraceStatus.COMPLETE
        assert env.torn_down


class TestNoHooksConfigured:
    """Usage must be real even with no hook at all — the documented path."""

    async def test_loop_measures_usage_without_hooks(self, tmp_path):
        run, _ = _run_with_hooks(tmp_path, plan=["done"], hooks=[])
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price", task_id="t-1", timeout=10
        )
        assert result.metrics.usage is not None
        assert result.metrics.usage.total_tokens > 0

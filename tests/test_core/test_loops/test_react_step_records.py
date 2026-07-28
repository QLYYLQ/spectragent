"""ReactLoop surrenders complete StepRecords — F6.

The loop always *built* a proper StepRecord and then handed it to nobody,
so every trace on disk had two keys per step. These tests pin the sink
contract, the per-step token usage, action truncation feedback, the
answer surviving max_steps, and env/LLM failures producing a terminal
result instead of an unhandled exception.
"""

from __future__ import annotations

from typing import Any

from spectragent.core.loops.react import ReactLoop
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation import build_observation_model
from spectragent.schemas.observation.fragments import BrowserMetaObs, TaskObs


class _Usage:
    prompt_tokens = 10
    completion_tokens = 4
    prompt_cached_tokens = 1
    total_tokens = 14


class _Completion:
    def __init__(self, text="out", usage=None):
        self.completion = text
        self.usage = usage


class _LLM:
    model = "test-model"

    def __init__(self, usage=None, boom=False):
        self._usage = usage
        self._boom = boom
        self.calls = 0

    async def ainvoke(self, messages, **kw):
        self.calls += 1
        if self._boom:
            raise RuntimeError("429 rate limited")
        return _Completion(usage=self._usage)


class _Memory:
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


class _Negotiated:
    def __init__(self, active, loop_sourced=frozenset()):
        self.active_fragments = active
        self.loop_sourced = loop_sourced
        self.env_sourced = active - loop_sourced


class _Env:
    def __init__(self, active, boom=False):
        self._model = build_observation_model(active)
        self._active = active
        self._boom = boom

    async def observe(self, negotiated=None):
        if self._boom:
            raise RuntimeError("CDP disconnected")
        values = {}
        if "browser.browser_meta" in self._active:
            values["browser_meta"] = BrowserMetaObs(
                url="https://example.com", page_title="Example"
            )
        return self._model(**values)


class _Agent:
    def __init__(self, actions_per_step: list[list[Action]]):
        self._plan = actions_per_step
        self._i = 0

    def build_react_prompt(self, obs):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out):
        actions = self._plan[min(self._i, len(self._plan) - 1)]
        self._i += 1
        return list(actions)

    def get_react_output_model(self):
        return None


def _ctx(*, env, llm, memory, act_fn, negotiated, recorder, task_id="t-1"):
    return {
        "env": env,
        "llm": llm,
        "memory": memory,
        "act_fn": act_fn,
        "negotiated_obs": negotiated,
        "step_recorder": recorder,
        "task_id": task_id,
    }


def _act_ok(done_after: int = 1, boom: bool = False):
    state = {"n": 0}

    async def act(action):
        if boom:
            raise RuntimeError("browser died mid-click")
        state["n"] += 1
        is_done = state["n"] >= done_after
        return ActionResult(
            success=True,
            is_done=is_done,
            extracted_content="the answer" if is_done else "partial output",
        )

    return act


class TestStepSurrender:
    async def test_records_reach_the_sink_with_observation(self):
        active = frozenset({"browser.browser_meta", "loop.task"})
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=3)
        result = await loop._execute(
            _Agent([[Action(name="browser.click", params={"index": 1})]]),
            "find the price",
            ctx=_ctx(
                env=_Env(active),
                llm=_LLM(usage=_Usage()),
                memory=_Memory(),
                act_fn=_act_ok(done_after=1),
                negotiated=_Negotiated(active, frozenset({"loop.task"})),
                recorder=recorder,
            ),
        )

        assert result.agent_done.value == "done"
        assert len(recorder.records) == 1
        rec = recorder.records[0]
        assert rec.step == 0
        assert [a.name for a in rec.actions] == ["browser.click"]
        assert rec.action_results[0].success is True
        assert rec.timestamp > 0
        assert rec.token_usage is not None
        assert rec.token_usage.total_tokens == 14
        # The observation handed over is the one the agent saw — loop
        # fragments populated, not env.observe's return value.
        assert isinstance(recorder.last_observation.task, TaskObs)
        assert recorder.last_observation.task.user_request == "find the price"

    async def test_observation_summary_is_filled(self):
        active = frozenset({"browser.browser_meta"})
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=1)
        await loop._execute(
            _Agent([[Action(name="browser.click")]]),
            "t",
            ctx=_ctx(
                env=_Env(active),
                llm=_LLM(usage=_Usage()),
                memory=_Memory(),
                act_fn=_act_ok(done_after=1),
                negotiated=_Negotiated(active),
                recorder=recorder,
            ),
        )
        assert "Example" in recorder.records[0].observation_summary

    async def test_loop_runs_without_a_recorder(self):
        """No sink configured must not change execution."""
        active = frozenset({"browser.browser_meta"})
        loop = ReactLoop(max_steps=1)
        result = await loop._execute(
            _Agent([[Action(name="browser.click")]]),
            "t",
            ctx=_ctx(
                env=_Env(active),
                llm=_LLM(usage=_Usage()),
                memory=_Memory(),
                act_fn=_act_ok(done_after=1),
                negotiated=_Negotiated(active),
                recorder=None,
            ),
        )
        assert result.agent_done.value == "done"


class TestUsageAccounting:
    async def test_usage_accumulates_across_steps(self):
        active = frozenset({"browser.browser_meta"})
        loop = ReactLoop(max_steps=3)
        result = await loop._execute(
            _Agent([[Action(name="browser.click")]]),
            "t",
            ctx=_ctx(
                env=_Env(active),
                llm=_LLM(usage=_Usage()),
                memory=_Memory(),
                act_fn=_act_ok(done_after=3),
                negotiated=_Negotiated(active),
                recorder=StepRecorder(),
            ),
        )
        usage = result.metrics.usage
        assert usage is not None
        assert usage.by_model is not None
        # Three calls, accumulated — with no hooks configured at all.
        assert usage.total_tokens == 42
        assert usage.total_prompt_tokens == 30
        assert usage.entry_count == 3
        assert usage.by_model["test-model"]["calls"] == 3
        assert result.metrics.llm_calls == 3
        assert result.metrics.steps == 3
        assert result.model_id == "test-model"

    async def test_unmeasured_usage_stays_none(self):
        """A provider that reports nothing must not look like zero cost."""
        active = frozenset({"browser.browser_meta"})
        loop = ReactLoop(max_steps=1)
        result = await loop._execute(
            _Agent([[Action(name="browser.click")]]),
            "t",
            ctx=_ctx(
                env=_Env(active),
                llm=_LLM(usage=None),
                memory=_Memory(),
                act_fn=_act_ok(done_after=1),
                negotiated=_Negotiated(active),
                recorder=StepRecorder(),
            ),
        )
        assert result.metrics.usage is None
        assert result.metrics.llm_calls == 1


class TestActionTruncation:
    async def test_truncation_is_marked_and_fed_back(self):
        active = frozenset({"browser.browser_meta"})
        memory = _Memory()
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=1, max_actions_per_step=1)
        await loop._execute(
            _Agent(
                [[
                    Action(name="browser.click"),
                    Action(name="browser.type"),
                    Action(name="browser.scroll"),
                ]]
            ),
            "t",
            ctx=_ctx(
                env=_Env(active),
                llm=_LLM(usage=_Usage()),
                memory=memory,
                act_fn=_act_ok(done_after=99),
                negotiated=_Negotiated(active),
                recorder=recorder,
            ),
        )
        assert recorder.records[0].truncated_actions == 2
        feedback = [
            m for m in memory.messages
            if "did NOT" in str(getattr(m, "content", ""))
        ]
        assert feedback, "the model must be told its extra actions never ran"


class TestFailurePaths:
    async def test_observe_failure_terminates_with_env_failed(self):
        active = frozenset({"browser.browser_meta"})
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=2)
        result = await loop._execute(
            _Agent([[Action(name="browser.click")]]),
            "t",
            ctx=_ctx(
                env=_Env(active, boom=True),
                llm=_LLM(usage=_Usage()),
                memory=_Memory(),
                act_fn=_act_ok(),
                negotiated=_Negotiated(active),
                recorder=recorder,
            ),
        )
        assert result.agent_done.value == "error"
        assert result.env_status.value == "failed"
        assert result.error is not None and "CDP disconnected" in result.error
        assert recorder.records[0].error_phase == "observe"

    async def test_llm_failure_keeps_env_status_success(self):
        """A 429 is not a broken browser."""
        active = frozenset({"browser.browser_meta"})
        loop = ReactLoop(max_steps=2)
        result = await loop._execute(
            _Agent([[Action(name="browser.click")]]),
            "t",
            ctx=_ctx(
                env=_Env(active),
                llm=_LLM(boom=True),
                memory=_Memory(),
                act_fn=_act_ok(),
                negotiated=_Negotiated(active),
                recorder=StepRecorder(),
            ),
        )
        assert result.agent_done.value == "error"
        assert result.env_status.value == "success"
        assert result.error is not None and "429" in result.error

    async def test_action_failure_terminates_with_env_failed(self):
        active = frozenset({"browser.browser_meta"})
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=2)
        result = await loop._execute(
            _Agent([[Action(name="browser.click")]]),
            "t",
            ctx=_ctx(
                env=_Env(active),
                llm=_LLM(usage=_Usage()),
                memory=_Memory(),
                act_fn=_act_ok(boom=True),
                negotiated=_Negotiated(active),
                recorder=recorder,
            ),
        )
        assert result.agent_done.value == "error"
        assert result.env_status.value == "failed"
        assert recorder.records[0].error_phase == "act"

    async def test_action_failure_is_logged_with_its_traceback(self, caplog):
        """The one branch whose entire purpose is per-action attribution.

        ``logger.exception`` was called several statements after the
        ``except`` handler that captured the error had already exited, so
        ``sys.exc_info()`` was back to ``(None, None, None)`` and the
        operator got ``NoneType: None`` where the stack should be.
        """
        active = frozenset({"browser.browser_meta"})
        loop = ReactLoop(max_steps=2)
        with caplog.at_level("ERROR", logger="spectragent.core.loops.react"):
            await loop._execute(
                _Agent([[Action(name="browser.click")]]),
                "t",
                ctx=_ctx(
                    env=_Env(active),
                    llm=_LLM(usage=_Usage()),
                    memory=_Memory(),
                    act_fn=_act_ok(boom=True),
                    negotiated=_Negotiated(active),
                    recorder=StepRecorder(),
                ),
            )

        records = [r for r in caplog.records if "action execution failed" in r.message]
        assert records, "the act failure was not logged at all"
        record = records[0]
        # ``(None, None, None)`` is what a popped exception state looks
        # like — the shape this used to log.
        assert record.exc_info and record.exc_info[0] is RuntimeError, (
            f"logged without a traceback: exc_info={record.exc_info}"
        )
        assert "browser died mid-click" in str(record.exc_info[1])
        # The type name is in the message too, so an exception whose
        # ``str()`` is empty still identifies itself.
        assert "RuntimeError" in record.getMessage()


class TestAnswerSurvivesMaxSteps:
    async def test_max_steps_keeps_the_last_output(self):
        """An agent that produced the deliverable and ran out of steps
        must not lose it."""
        active = frozenset({"browser.browser_meta"})
        loop = ReactLoop(max_steps=2)
        result = await loop._execute(
            _Agent([[Action(name="browser.click")]]),
            "t",
            ctx=_ctx(
                env=_Env(active),
                llm=_LLM(usage=_Usage()),
                memory=_Memory(),
                act_fn=_act_ok(done_after=99),
                negotiated=_Negotiated(active),
                recorder=StepRecorder(),
            ),
        )
        assert result.agent_done.value == "max_steps"
        assert result.answer == "partial output"
        # The self-report stays None — the agent never called done().
        assert result.agent_reported_success is None

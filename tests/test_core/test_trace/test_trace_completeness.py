"""End-to-end acceptance for F5 + F6: a trace you can actually evaluate.

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

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.metrics_collector import MetricsCollector
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.models import RunStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation.fragments import BrowserMetaObs, ScreenshotObs
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
            return ActionResult(
                success=True, is_done=True, extracted_content="42 dollars"
            )
        return ActionResult(success=True, extracted_content="clicked")

    async def resolve_fragment(self, fragment_key, **kwargs):
        if fragment_key == "browser.screenshot":
            return ScreenshotObs(
                image_bytes=_PNG, viewport_width=1280, viewport_height=720
            )
        if fragment_key == "browser.browser_meta":
            return BrowserMetaObs(url="https://example.com", page_title="Example")
        return None


class _Env(EnvironmentBase):
    __registry_key__ = "trace_e2e_env"


class _Agent:
    __wiring__ = {
        "action": ("action", ["trace_e2e_cap.done", "trace_e2e_cap.click"]),
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
        return [Action(name=f"trace_e2e_cap.{name}", params={"index": 1})]

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

    def _validate_slots(self, agent):
        pass

    def weave(self, agent, **deps):
        async def run(task, **kw):
            await asyncio.sleep(100)

        agent.run = run
        return agent


class _StallAfterLLMLoop:
    """Gets an answer, then dies before surrendering the step.

    The real timeout shape: ``after_ainvoke`` has already buffered a
    prompt+completion when ``asyncio.wait_for`` cancels the loop, so
    ``record_step`` — the commit point — is never reached.
    """

    loop_id = "trace_e2e_stall"
    __required_slots__: set[str] = set()
    __optional_slots__: set[str] = set()
    __requires_env__ = True

    def _validate_slots(self, agent):
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


def _run_with_hooks(tmp_path, *, plan, loop=None, hooks=None):
    writer = TraceWriter()
    writer.trace_dir = str(tmp_path / "traces")
    # Declared in the order that used to zero out every result.json.
    hook_list = hooks if hooks is not None else [writer, MetricsCollector()]
    run = make_run(
        agent=_Agent(plan),
        loop=loop or ReactLoop(max_steps=3),
        env=make_env(_Env, {"trace_e2e_cap": _Cap()}),
        llm=_LLM(),
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

    async def test_manifest_completed_with_result(self, trace):
        t, _ = trace
        assert t.manifest.status == RunStatus.COMPLETED
        assert t.result is not None
        assert t.result["answer"] == "42 dollars"
        assert t.result["agent_reported_success"] is True

    async def test_step_records_are_complete(self, trace):
        t, _ = trace
        rec = t.get_step_record(0)
        assert rec is not None
        assert set(rec) >= {
            "step",
            "llm_output",
            "actions",
            "action_results",
            "timestamp",
            "token_usage",
        }
        assert rec["actions"][0]["name"] == "trace_e2e_cap.click"
        assert rec["action_results"][0]["extracted_content"] == "clicked"
        assert rec["token_usage"]["total_tokens"] == 150
        assert rec["observation_summary"]
        assert t.step_count == 2

    async def test_screenshots_are_real_png_files(self, trace):
        t, _ = trace
        assert t.screenshots, "RunTrace.screenshots must not be empty"
        assert t.screenshots[0].read_bytes() == _PNG
        sidecar = t.get_observation(0, "screenshot")
        assert sidecar["viewport_width"] == 1280
        assert "image_bytes" not in sidecar

    async def test_loop_fragments_land_on_disk(self, trace):
        t, _ = trace
        obs_files = list((t.trace_dir / "observations").iterdir())
        written = {p.name.split("_", 1)[1].rsplit(".", 1)[0] for p in obs_files}
        assert "task" in written
        assert t.get_observation(0, "task")["user_request"] == "find the price"
        # And the junk 0.0 timestamp file is gone.
        assert "timestamp" not in written

    async def test_tokens_recorded_despite_hook_order(self, trace):
        t, _ = trace
        assert t.result is not None
        usage = t.result["metrics"]["usage"]
        assert usage is not None
        assert usage["total_tokens"] == 300  # 2 steps x 150
        assert usage["by_model"]["gpt-4o-mini"]["calls"] == 2
        assert t.result["metrics"]["steps"] == 2
        assert t.result["metrics"]["llm_calls"] == 2
        assert t.result["model_id"] == "gpt-4o-mini"


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
        assert t.manifest.status == RunStatus.ERROR  # not 'running'
        assert t.result is not None
        assert t.config.benchmark and t.config.task_id
        assert t.config.llm_model and t.config.loop_id
        assert result.agent_done.value == "timeout"
        assert json.loads((t.trace_dir / "result.json").read_text())["agent_done"] == (
            "timeout"
        )


class TestAbnormalEndKeepsTheLastExchange:
    """The trailing turn of a dead run is the one you most need to read.

    Buffered LLM calls used to be dropped whenever the run ended without
    one more ``record_step`` — which is every timeout and every crash.
    """

    async def test_timeout_flushes_the_buffered_call(self, tmp_path):
        run, writer = _run_with_hooks(
            tmp_path, plan=["done"], loop=_StallAfterLLMLoop()
        )
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price", task_id="mind2web--0042", timeout=0.05
        )
        assert result.agent_done.value == "timeout"
        assert writer.run_dir is not None

        t = RunTrace(writer.run_dir)
        prompt, completion = t.get_messages(0)
        assert prompt == [{"role": "user", "content": "find the price"}]
        assert completion is not None
        assert [p.name for p in t.unfinished_message_files] == [
            "000_input.json",
            "000_output.json",
        ]
        # Recorded as unfinished, not as a step that never happened.
        assert t.step_count == 0
        assert t.manifest.status == RunStatus.ERROR

    async def test_exception_flushes_the_buffered_call(self, tmp_path):
        run, writer = _run_with_hooks(
            tmp_path, plan=["done"], loop=_CrashAfterLLMLoop()
        )
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price",
            task_id="mind2web--0042",
            timeout=10,
            raise_on_error=False,
        )
        assert result.agent_done.value == "error"
        assert writer.run_dir is not None

        t = RunTrace(writer.run_dir)
        assert t.get_messages(0)[0] == [{"role": "user", "content": "find the price"}]
        assert t.manifest.status == RunStatus.ERROR


class TestNoHooksConfigured:
    """Usage must be real even with no hook at all — the documented path."""

    async def test_loop_measures_usage_without_hooks(self, tmp_path):
        run, _ = _run_with_hooks(tmp_path, plan=["done"], hooks=[])
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "find the price", task_id="t-1", timeout=10
        )
        assert result.metrics.usage is not None
        assert result.metrics.usage.total_tokens > 0

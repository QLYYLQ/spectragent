"""Tests for AgentRuntime — full pipeline, timeout, hooks, compatibility."""

import asyncio

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops import ReactLoop
from spectragent.core.loops.validation import IncompatibleError
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult


# ── Shared fixtures ───────────────────────────────────────────

class SimpleCap(CapabilityBase):
    __registry_key__ = "rt_simple_cap"
    __wiring__ = {"action": ("action", ["done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="done", description="Done")]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, is_done=True, extracted_content="answer")


class SimpleEnv(EnvironmentBase):
    __registry_key__ = "rt_simple_env"


class SimpleAgent:
    # Declare the action as required so M13-Rev's runtime negotiation
    # keeps the ``done`` descriptor in the active set.
    __wiring__ = {"action": ("action", ["done"])}

    def build_react_prompt(self, obs):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, llm_output):
        return [Action(name="done")]

    def get_react_output_model(self):
        return None


class MockCompletion:
    completion = "done"
    usage = None


class MockLLM:
    model = "test"
    async def ainvoke(self, messages, **kwargs):
        return MockCompletion()


class MockMemory:
    def add_message(self, msg): pass
    def clear(self): pass


def _make_env():
    return SimpleEnv({"rt_simple_cap": SimpleCap()})


# ── Tests ─────────────────────────────────────────────────────

class TestFullPipeline:
    def test_execute_returns_done(self) -> None:
        result = asyncio.run(
            AgentRuntime().execute(
                SimpleAgent(), ReactLoop(), _make_env(), "test",
                llm=MockLLM(), memory=MockMemory(), task_id="t-001",
            )
        )
        assert result.agent_done.value == "done"
        assert result.answer == "answer"
        assert result.agent_success is True


class TestTimeout:
    def test_timeout_result(self) -> None:
        class SlowLoop:
            __requires_protocol__ = None
            loop_id = "slow"
            async def run(self, agent, env, task, **kw):
                await asyncio.sleep(100)

        result = asyncio.run(
            AgentRuntime().execute(
                SimpleAgent(), SlowLoop(), _make_env(), "timeout",
                llm=MockLLM(), memory=MockMemory(), timeout=0.05,
            )
        )
        assert result.agent_done.value == "timeout"


class TestCompatibility:
    def test_incompatible_raises(self) -> None:
        class BadAgent:
            __wiring__ = {}

        with pytest.raises(IncompatibleError):
            asyncio.run(
                AgentRuntime().execute(
                    BadAgent(), ReactLoop(), _make_env(), "fail",
                    llm=MockLLM(), memory=MockMemory(),
                )
            )


class TestHookLifecycle:
    def test_start_end_called(self) -> None:
        log = []

        class Hook:
            hook_id = "tracker"
            blocking = True
            __intercepts__ = []
            async def on_run_start(self, **kw): log.append("start")
            async def on_run_end(self, **kw): log.append("end")
            async def on_run_error(self, **kw): log.append("error")

        asyncio.run(
            AgentRuntime(hooks=[Hook()]).execute(
                SimpleAgent(), ReactLoop(), _make_env(), "hook",
                llm=MockLLM(), memory=MockMemory(),
            )
        )
        assert log == ["start", "end"]

    def test_error_called_on_timeout(self) -> None:
        log = []

        class Hook:
            hook_id = "tracker2"
            blocking = True
            __intercepts__ = []
            async def on_run_start(self, **kw): log.append("start")
            async def on_run_end(self, **kw): log.append("end")
            async def on_run_error(self, **kw): log.append("error")

        class SlowLoop:
            __requires_protocol__ = None
            loop_id = "slow2"
            async def run(self, agent, env, task, **kw):
                await asyncio.sleep(100)

        asyncio.run(
            AgentRuntime(hooks=[Hook()]).execute(
                SimpleAgent(), SlowLoop(), _make_env(), "timeout hook",
                llm=MockLLM(), memory=MockMemory(), timeout=0.05,
            )
        )
        assert log == ["start", "error"]


class TestActionNegotiation:
    """M13-Rev: ``_negotiate_actions`` runs symmetrically to obs negotiation."""

    def test_required_missing_raises_before_loop_runs(self) -> None:
        from spectragent.core.actions.negotiation import IncompatibleActionError

        class NeedsBash:
            __wiring__ = {"action": ("action", ["bash"])}

            def build_react_prompt(self, obs):
                return [{"role": "user", "content": "go"}]

            def parse_react_output(self, out):
                return [Action(name="bash")]

            def get_react_output_model(self):
                return None

        loop_runs: list[str] = []

        class SpyLoop:
            __requires_protocol__ = None
            loop_id = "nego_spy"

            async def run(self, *args, **kw):
                loop_runs.append("ran")
                from datetime import datetime, timezone

                from spectragent.schemas._types import (
                    AgentDoneStatus,
                    EnvironmentStatus,
                )
                from spectragent.schemas.agent_result import (
                    AgentMetrics,
                    AgentResult,
                    AgentUsage,
                )

                return AgentResult(
                    task_id="t",
                    task="x",
                    timestamp=datetime.now(timezone.utc),
                    env_status=EnvironmentStatus.SUCCESS,
                    agent_done=AgentDoneStatus.DONE,
                    metrics=AgentMetrics(
                        end_to_end_ms=0, steps=0, usage=AgentUsage(),
                    ),
                )

        with pytest.raises(IncompatibleActionError):
            asyncio.run(
                AgentRuntime().execute(
                    NeedsBash(), SpyLoop(), _make_env(), "fail",
                    llm=MockLLM(), memory=MockMemory(),
                )
            )
        assert loop_runs == []  # loop never ran

    def test_optional_not_provided_is_graceful(self) -> None:
        """Agent optional missing from env → run continues with narrower set."""
        seen_names: list[str] = []

        class OptionalAgent(SimpleAgent):
            __wiring__ = {
                "action": ("action", ["done"], ["click", "scroll"]),
            }

        class SpyLoop:
            __requires_protocol__ = None
            loop_id = "opt_spy"

            async def run(self, agent, env, task, **kw):
                seen_names.extend(
                    d.name for d in kw.get("active_descriptors", [])
                )
                from datetime import datetime, timezone

                from spectragent.schemas._types import (
                    AgentDoneStatus,
                    EnvironmentStatus,
                )
                from spectragent.schemas.agent_result import (
                    AgentMetrics,
                    AgentResult,
                    AgentUsage,
                )

                return AgentResult(
                    task_id="t",
                    task=task,
                    timestamp=datetime.now(timezone.utc),
                    env_status=EnvironmentStatus.SUCCESS,
                    agent_done=AgentDoneStatus.DONE,
                    metrics=AgentMetrics(
                        end_to_end_ms=0, steps=0, usage=AgentUsage(),
                    ),
                )

        asyncio.run(
            AgentRuntime().execute(
                OptionalAgent(), SpyLoop(), _make_env(), "opt",
                llm=MockLLM(), memory=MockMemory(),
            )
        )
        # SimpleCap only provides "done"; the two optional actions are
        # not in the env, so they should NOT be active.
        assert set(seen_names) == {"done"}


class TestActionFilter:
    def test_filter_narrows(self) -> None:
        seen = []

        class SpyLoop:
            __requires_protocol__ = None
            loop_id = "spy"
            async def run(self, agent, env, task, **kw):
                seen.extend(kw.get("active_descriptors", []))
                from datetime import datetime, timezone
                from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
                from spectragent.schemas.agent_result import AgentMetrics, AgentResult, AgentUsage
                return AgentResult(
                    task_id="t", task=task,
                    timestamp=datetime.now(timezone.utc),
                    env_status=EnvironmentStatus.SUCCESS,
                    agent_done=AgentDoneStatus.DONE,
                    metrics=AgentMetrics(end_to_end_ms=0, steps=0, usage=AgentUsage()),
                )

        class MultiCap(CapabilityBase):
            __registry_key__ = "rt_multi_cap"
            __wiring__ = {"action": ("action", ["click", "type", "done"])}

            def get_action_descriptors(self, **ctx):
                return [
                    ActionDescriptor(name="click", description="Click"),
                    ActionDescriptor(name="type", description="Type"),
                    ActionDescriptor(name="done", description="Done"),
                ]

        class MultiAgent(SimpleAgent):
            # Agent wiring accepts every action the capability provides;
            # the ``action_filter`` kwarg will narrow further.
            __wiring__ = {"action": ("action", ["click", "type", "done"])}

        env = SimpleEnv({"rt_multi_cap": MultiCap()})
        asyncio.run(
            AgentRuntime().execute(
                MultiAgent(), SpyLoop(), env, "filter",
                llm=MockLLM(), memory=MockMemory(),
                action_filter=["click", "done"],
            )
        )
        assert {d.name for d in seen} == {"click", "done"}

"""Tests for AgentRuntime — full pipeline, timeout, hooks, compatibility."""

import asyncio
from datetime import UTC

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops import ReactLoop
from spectragent.core.loops.validation import IncompatibleError
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import AGENT_ID, make_env, make_run

# ── Shared fixtures ───────────────────────────────────────────


class SimpleCap(CapabilityBase):
    __registry_key__ = "rt_simple_cap"
    __wiring__ = {"action": ("action", ["rt_simple_cap.done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="rt_simple_cap.done", description="Done")]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, is_done=True, extracted_content="answer")


class SimpleEnv(EnvironmentBase):
    __registry_key__ = "rt_simple_env"


class SimpleAgent:
    # Declare the action as required so runtime negotiation
    # keeps the ``done`` descriptor in the active set.
    __wiring__ = {"action": ("action", ["rt_simple_cap.done"])}

    def build_react_prompt(self, obs):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, llm_output):
        return [Action(name="rt_simple_cap.done")]

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
    supports_protocols = frozenset({"user_assistant"})

    def __init__(self):
        self.messages = []

    def add_message(self, msg):
        self.messages.append(msg)

    def add_messages(self, msgs):
        self.messages.extend(msgs)

    def get_context_messages(self):
        return list(self.messages)

    def clear(self):
        self.messages.clear()


def _make_env():
    return make_env(SimpleEnv, {"rt_simple_cap": SimpleCap()})


# ── Composite-env fixtures ────────────────────────────────────


class _ClickTypeCap(CapabilityBase):
    __registry_key__ = "rt_composite_cap_click_type"
    __wiring__ = {"action": ("action", ["browser.click", "browser.type"])}

    def get_action_descriptors(self, **ctx):
        return [
            ActionDescriptor(name="browser.click", description="Click"),
            ActionDescriptor(name="browser.type", description="Type"),
        ]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True)


class _CompositeDoneCap(CapabilityBase):
    __registry_key__ = "rt_composite_cap_done"
    __wiring__ = {"action": ("action", ["finish.done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="finish.done", description="Done")]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, is_done=True, extracted_content="ok")


def _make_composite_env():
    return make_env(
        SimpleEnv,
        {"browser": _ClickTypeCap(), "finish": _CompositeDoneCap()},
    )


# ── Mock loop helpers ─────────────────────────────────────────


class _MockLoopBase:
    """Minimal loop mock that supports the weave interface."""

    __required_slots__: set[str] = set()
    __optional_slots__: set[str] = set()
    __requires_env__ = True

    def _validate_slots(self, agent):
        pass

    def weave(self, agent, **deps):
        self.ctx = deps
        loop = self

        async def run(task, **kw):
            return await loop._execute(agent, task, **kw)

        agent.run = run
        return agent


class _SlowLoop(_MockLoopBase):
    loop_id = "slow"

    async def _execute(self, agent, task, **kw):
        await asyncio.sleep(100)


class _SpyLoop(_MockLoopBase):
    """Captures active_descriptors from ctx for assertion."""

    def __init__(self, loop_id: str, seen_names: list[str]):
        self._loop_id = loop_id
        self._seen_names = seen_names

    @property
    def loop_id(self):
        return self._loop_id

    async def _execute(self, agent, task, **kw):
        self._seen_names.extend(d.name for d in self.ctx.get("active_descriptors", []))
        from datetime import datetime

        from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
        from spectragent.schemas.agent_result import (
            AgentMetrics,
            AgentResult,
            AgentUsage,
        )

        return AgentResult(
            task_id="t",
            task=task,
            timestamp=datetime.now(UTC),
            env_status=EnvironmentStatus.SUCCESS,
            agent_done=AgentDoneStatus.DONE,
            metrics=AgentMetrics(end_to_end_ms=0, steps=0, usage=AgentUsage()),
        )


# ── Tests ─────────────────────────────────────────────────────


def _mk(agent, loop, env, *, hooks=None):
    """Shortcut: build a test ComposedRun with mock LLM/memory."""
    return make_run(
        agent=agent,
        loop=loop,
        env=env,
        llm=MockLLM(),
        memory=MockMemory(),
        hooks=hooks,
    )


class TestFullPipeline:
    def test_execute_returns_done(self) -> None:
        run = _mk(SimpleAgent(), ReactLoop(), _make_env())
        result = asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("test", task_id="t-001"))
        assert result.agent_done.value == "done"
        assert result.answer == "answer"
        assert result.agent_reported_success is True


class TestPrepare:
    def test_prepare_patches_agent_run(self) -> None:
        agent = SimpleAgent()
        assert not hasattr(agent, "run")

        run = _mk(agent, ReactLoop(), _make_env())
        prepared = AgentRuntime.for_agent(run, AGENT_ID).prepare()
        assert hasattr(prepared, "run")
        assert callable(prepared.run)

    def test_prepared_agent_can_run(self) -> None:
        agent = SimpleAgent()
        run = _mk(agent, ReactLoop(), _make_env())
        prepared = AgentRuntime.for_agent(run, AGENT_ID).prepare()
        result = asyncio.run(prepared.run("test"))
        assert result.agent_done.value == "done"

    def test_execute_invokes_agent_prepare_and_seeds_memory(self) -> None:
        """AgentRuntime.execute() owns the prepare → memory.add_messages
        handoff: the agent emits seed messages and the runtime writes
        them into memory before agent.run starts."""
        from spectragent.core.llm.messages import SystemMessage

        seed = SystemMessage(content="seed")
        prepare_calls: list[tuple[str, list]] = []

        class SeedingAgent(SimpleAgent):
            def prepare(self, task, action_descriptors):
                prepare_calls.append((task, list(action_descriptors)))
                return [seed]

        memory = MockMemory()
        run = make_run(
            agent=SeedingAgent(),
            loop=ReactLoop(),
            env=_make_env(),
            llm=MockLLM(),
            memory=memory,
        )
        asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("seed-task", task_id="t-seed"))

        assert len(prepare_calls) == 1
        assert prepare_calls[0][0] == "seed-task"
        assert {d.name for d in prepare_calls[0][1]} == {"rt_simple_cap.done"}
        assert memory.messages[0] is seed

    def test_execute_skips_prepare_when_agent_lacks_method(self) -> None:
        """Agents without ``prepare`` simply skip the step; no error."""
        memory = MockMemory()
        run = make_run(
            agent=SimpleAgent(),
            loop=ReactLoop(),
            env=_make_env(),
            llm=MockLLM(),
            memory=memory,
        )
        asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("noop", task_id="t-noop"))
        # SimpleAgent does not define ``prepare``, so no system prompt
        # was seeded by the runtime.
        assert memory.messages == [] or all(
            getattr(m, "role", None) != "system" for m in memory.messages
        )


class TestTimeout:
    def test_timeout_result(self) -> None:
        run = _mk(SimpleAgent(), _SlowLoop(), _make_env())
        result = asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("timeout", timeout=0.05))
        assert result.agent_done.value == "timeout"


class TestCompatibility:
    def test_incompatible_raises(self) -> None:
        class BadAgent:
            __wiring__ = {}

        run = _mk(BadAgent(), ReactLoop(), _make_env())
        with pytest.raises(IncompatibleError):
            asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("fail"))


class TestHookLifecycle:
    def test_start_end_called(self) -> None:
        log = []

        class Hook:
            hook_id = "tracker"
            blocking = True
            __intercepts__ = []

            async def on_run_start(self, **kw):
                log.append("start")

            async def on_run_end(self, **kw):
                log.append("end")

            async def on_run_error(self, **kw):
                log.append("error")

        run = _mk(SimpleAgent(), ReactLoop(), _make_env(), hooks=[Hook()])
        asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("hook"))
        assert log == ["start", "end"]

    def test_error_called_on_timeout(self) -> None:
        log = []

        class Hook:
            hook_id = "tracker2"
            blocking = True
            __intercepts__ = []

            async def on_run_start(self, **kw):
                log.append("start")

            async def on_run_end(self, **kw):
                log.append("end")

            async def on_run_error(self, **kw):
                log.append("error")

        run = _mk(SimpleAgent(), _SlowLoop(), _make_env(), hooks=[Hook()])
        asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("timeout hook", timeout=0.05))
        # on_run_end fires on the timeout path too — it is the only place
        # a persistence hook writes result.json, so skipping it is what
        # left timed-out runs as orphan traces stuck at status=running.
        assert log == ["start", "end", "error"]


class TestActionNegotiation:
    """_negotiate_actions runs symmetrically to obs negotiation."""

    def test_required_missing_raises_before_loop_runs(self) -> None:
        from spectragent.core.negotiation import IncompatibleActionError

        class NeedsBash:
            __wiring__ = {"action": ("action", ["sandbox.bash"])}

            def build_react_prompt(self, obs):
                return [{"role": "user", "content": "go"}]

            def parse_react_output(self, out):
                return [Action(name="sandbox.bash")]

            def get_react_output_model(self):
                return None

        seen: list[str] = []

        run = _mk(NeedsBash(), _SpyLoop("nego_spy", seen), _make_env())
        with pytest.raises(IncompatibleActionError):
            asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("fail"))
        assert seen == []  # loop never ran

    def test_optional_not_provided_is_graceful(self) -> None:
        """Agent optional missing from env -> run continues with narrower set."""
        seen_names: list[str] = []

        class OptionalAgent(SimpleAgent):
            __wiring__ = {
                "action": (
                    "action",
                    ["rt_simple_cap.done"],
                    ["rt_simple_cap.click", "rt_simple_cap.scroll"],
                ),
            }

        run = _mk(OptionalAgent(), _SpyLoop("opt_spy", seen_names), _make_env())
        asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("opt"))
        # SimpleCap only provides "done"; the two optional actions are
        # not in the env, so they should NOT be active.
        assert set(seen_names) == {"rt_simple_cap.done"}


class TestCompositeEnvNarrowing:
    """Composite envs must survive the runtime narrowing."""

    def test_composite_env_preserves_prefixed_names(self) -> None:
        seen: list[str] = []

        class MultiActionAgent(SimpleAgent):
            __wiring__ = {
                "action": (
                    "action",
                    ["browser.click", "finish.done"],
                    ["browser.type"],
                ),
            }

        run = _mk(
            MultiActionAgent(),
            _SpyLoop("composite_spy", seen),
            _make_composite_env(),
        )
        asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("composite"))
        assert set(seen) == {"browser.click", "browser.type", "finish.done"}

    def test_composite_env_drops_actions_not_in_wiring(self) -> None:
        seen: list[str] = []

        class FinishDoneAgent:
            __wiring__ = {"action": ("action", ["finish.done"])}

            def build_react_prompt(self, obs):
                return [{"role": "user", "content": "go"}]

            def parse_react_output(self, out):
                return [Action(name="finish.done")]

            def get_react_output_model(self):
                return None

        run = _mk(
            FinishDoneAgent(),
            _SpyLoop("composite_spy2", seen),
            _make_composite_env(),
        )
        asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("composite_min"))
        assert set(seen) == {"finish.done"}

    def test_composite_env_required_missing_raises(self) -> None:
        from spectragent.core.negotiation import IncompatibleActionError

        class NeedsBash:
            __wiring__ = {"action": ("action", ["sandbox.bash"])}

            def build_react_prompt(self, obs):
                return [{"role": "user", "content": "go"}]

            def parse_react_output(self, out):
                return [Action(name="sandbox.bash")]

            def get_react_output_model(self):
                return None

        run = _mk(NeedsBash(), ReactLoop(), _make_composite_env())
        with pytest.raises(IncompatibleActionError):
            asyncio.run(AgentRuntime.for_agent(run, AGENT_ID).execute("fail"))


class TestActionFilter:
    def test_filter_narrows(self) -> None:
        seen: list[str] = []

        class MultiCap(CapabilityBase):
            __registry_key__ = "rt_multi_cap"
            __wiring__ = {
                "action": (
                    "action",
                    ["rt_multi_cap.click", "rt_multi_cap.type", "rt_multi_cap.done"],
                ),
            }

            def get_action_descriptors(self, **ctx):
                return [
                    ActionDescriptor(name="rt_multi_cap.click", description="Click"),
                    ActionDescriptor(name="rt_multi_cap.type", description="Type"),
                    ActionDescriptor(name="rt_multi_cap.done", description="Done"),
                ]

        class MultiAgent(SimpleAgent):
            __wiring__ = {
                "action": (
                    "action",
                    ["rt_multi_cap.click", "rt_multi_cap.type", "rt_multi_cap.done"],
                ),
            }

        env = make_env(SimpleEnv, {"rt_multi_cap": MultiCap()})
        run = _mk(MultiAgent(), _SpyLoop("spy", seen), env)
        asyncio.run(
            AgentRuntime.for_agent(run, AGENT_ID).execute(
                "filter",
                action_filter=["rt_multi_cap.click", "rt_multi_cap.done"],
            )
        )
        assert set(seen) == {"rt_multi_cap.click", "rt_multi_cap.done"}


class TestDescriptorContext:
    """Step 4 hands the capability the run's observation context.

    A parameter can depend on a fragment — ``browser.scroll``'s ``index``
    addresses the selector map ``browser.dom`` builds — and only the
    providing capability knows which. Without the active fragment set it
    cannot drop such a param, so a vision-only run's prompt would teach an
    index it has no way to resolve. The negotiation *rules* are untouched;
    this is the ``**context`` channel a per-task tool schema already rides.
    """

    def test_get_action_space_receives_the_active_fragments(self) -> None:
        seen: dict[str, object] = {}

        class _SpyCap(SimpleCap):
            __registry_key__ = "rt_spy_cap"
            __wiring__ = {"action": ("action", ["rt_spy_cap.done"])}

            def get_action_descriptors(self, **ctx):
                seen.update(ctx)
                return [ActionDescriptor(name="rt_spy_cap.done", description="Done")]

        class _SpyAgent(SimpleAgent):
            __wiring__ = {
                "action": ("action", ["rt_spy_cap.done"]),
                "observation": ("observation", ["task"], []),
            }

            def parse_react_output(self, llm_output):
                return [Action(name="rt_spy_cap.done")]

        env = make_env(SimpleEnv, {"rt_spy_cap": _SpyCap()})
        runtime = AgentRuntime.for_agent(_mk(_SpyAgent(), ReactLoop(), env), AGENT_ID)
        runtime.prepare()

        assert "fragments" in seen, (
            "the capability was not told which fragments this run activated, "
            "so it cannot drop a param that depends on one"
        )
        negotiated = runtime.negotiated_observation
        assert negotiated is not None
        fragments = seen["fragments"]
        assert fragments == negotiated.active_fragments
        assert "loop.task" in negotiated.active_fragments

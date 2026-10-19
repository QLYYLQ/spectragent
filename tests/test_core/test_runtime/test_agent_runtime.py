"""Tests for AgentRuntime — full pipeline, timeout, hooks, compatibility."""

import asyncio
from datetime import UTC
from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops import ReactLoop
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.validation import IncompatibleError
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.assembly import AGENT_ID, make_env, make_run

# ── Shared fixtures ───────────────────────────────────────────


class SimpleCap(CapabilityBase):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "rt_simple_cap"
    __wiring__ = {"action": ("action", ["rt_simple_cap.done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="rt_simple_cap.done", description="Done")]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, is_done=True, extracted_content="answer")


class SimpleEnv(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "rt_simple_env"


class SimpleAgent:
    # Declare the action as required so runtime negotiation
    # keeps the ``done`` descriptor in the active set.
    __execution_limit_fields__ = frozenset()
    __wiring__ = {"action": ("action", ["rt_simple_cap.done"])}

    async def build_react_prompt(self, obs, *, context: ReactPromptContext):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, llm_output):
        return [Action(name="rt_simple_cap.done")]

    def get_react_output_model(self):
        return None


class MockCompletion:
    completion = "done"
    usage = None


class MockLLM:
    inference_kind = "chat"
    model = "test"

    async def ainvoke(self, messages, **kwargs):
        return MockCompletion()

    name = "test-model"

    async def aclose(self, *, timeout=5.0):
        return ()


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
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
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

    def validate_slots(self, agent):
        pass

    def check_loop_inputs(self, inputs):
        pass

    def weave(self, agent, **deps):
        self.ctx = deps
        loop: Any = self

        async def run(task, **kw):
            return await loop._execute(agent, task, **kw)

        agent.run = run
        return agent

    def execution_limits(self):
        return ExecutionLimits()


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
            metrics=AgentMetrics(execution_ms=0, steps=0, usage=AgentUsage()),
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


class TestSeedMessages:
    """``seed_messages(task)``: the seeding ``execute`` does, as a method of its own (§4.2)."""

    def _seeding_run(self, memory: MockMemory, calls: list[dict]) -> Any:
        from spectragent.core.llm.messages import SystemMessage

        class SeedingAgent(SimpleAgent):
            __execution_limit_fields__ = frozenset({"max_actions_per_step"})

            def prepare(self, task, action_descriptors, execution_limits):
                calls.append(
                    {
                        "task": task,
                        "names": sorted(d.name for d in action_descriptors),
                        "max_actions_per_step": execution_limits.max_actions_per_step,
                    }
                )
                return [SystemMessage(content=f"seed for {task}")]

        return make_run(
            agent=SeedingAgent(),
            loop=ReactLoop(max_actions_per_step=3),
            env=_make_env(),
            llm=MockLLM(),
            memory=memory,
        )

    def test_returns_the_agent_seed_without_writing_memory(self) -> None:
        memory, calls = MockMemory(), []
        runtime = AgentRuntime.for_agent(self._seeding_run(memory, calls), AGENT_ID)
        runtime.prepare()
        seeds = runtime.seed_messages("the task")
        assert [m.text for m in seeds] == ["seed for the task"]
        assert calls == [
            {"task": "the task", "names": ["rt_simple_cap.done"], "max_actions_per_step": 3}
        ]
        assert memory.messages == []  # the caller owns the write

    def test_is_exactly_what_execute_writes(self) -> None:
        memory, calls = MockMemory(), []
        runtime = AgentRuntime.for_agent(self._seeding_run(memory, calls), AGENT_ID)
        asyncio.run(runtime.execute("the task", task_id="t-seed"))
        executed_seed = memory.messages[0]
        assert runtime.seed_messages("the task") == [executed_seed]
        assert calls[0] == calls[1]

    def test_an_agent_without_prepare_seeds_nothing(self) -> None:
        runtime = AgentRuntime.for_agent(_mk(SimpleAgent(), ReactLoop(), _make_env()), AGENT_ID)
        runtime.prepare()
        assert runtime.seed_messages("x") == []

    def test_needs_prepare_first(self) -> None:
        runtime = AgentRuntime.for_agent(_mk(SimpleAgent(), ReactLoop(), _make_env()), AGENT_ID)
        with pytest.raises(RuntimeError, match="prepare"):
            runtime.seed_messages("x")


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
            __execution_limit_fields__ = frozenset()
            __wiring__ = {"action": ("action", ["sandbox.bash"])}

            async def build_react_prompt(self, obs, *, context: ReactPromptContext):
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
            __execution_limit_fields__ = frozenset()
            __wiring__ = {"action": ("action", ["finish.done"])}

            async def build_react_prompt(self, obs, *, context: ReactPromptContext):
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
            __execution_limit_fields__ = frozenset()
            __wiring__ = {"action": ("action", ["sandbox.bash"])}

            async def build_react_prompt(self, obs, *, context: ReactPromptContext):
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


async def test_action_snapshots_cannot_change_the_guard_or_recorded_contract():
    class MutatingAgent(SimpleAgent):
        def prepare(self, *, task, action_descriptors):
            action_descriptors[0].params["changed"] = "changed"
            action_descriptors.clear()
            return []

    loop = _SpyLoop("snapshot", [])
    runtime = AgentRuntime.for_agent(_mk(MutatingAgent(), loop, _make_env()), AGENT_ID)
    runtime.prepare()
    snapshot = runtime.action_space
    snapshot.descriptors[0].params["changed"] = "changed"
    snapshot.descriptors.clear()
    snapshot.action_bindings["done"] = "changed.action"
    runtime.active_descriptors.clear()
    runtime.seed_messages("t")
    assert [item.name for item in runtime.action_space.descriptors] == ["rt_simple_cap.done"]
    assert runtime.action_space.action_bindings["done"] == "rt_simple_cap.done"
    assert "changed" not in runtime.action_space.descriptors[0].params
    assert (await loop.ctx["act_fn"](Action(name="rt_simple_cap.done"))).is_done
    rejected = await loop.ctx["act_fn"](Action(name="changed.action"))
    assert not rejected.success
    assert "not in the active action set" in rejected.error


async def test_runtime_resolves_model_identity_once_and_shares_the_value():
    class CountingModel(MockLLM):
        reads = 0

        @property
        def model(self):
            self.reads += 1
            return "resolved-model"

    class IdentityHook:
        blocking = True
        seen: dict[str, Any] = {}

        async def on_run_start(self, **kwargs):
            self.seen = kwargs

    llm = CountingModel()
    loop = _SpyLoop("identity", [])
    hook = IdentityHook()
    run = make_run(
        agent=SimpleAgent(), loop=loop, env=_make_env(), llm=llm, memory=MockMemory(), hooks=[hook]
    )
    runtime = AgentRuntime.for_agent(run, AGENT_ID)
    await runtime.execute("t", task_id="identity-task")
    assert llm.reads == 1
    assert hook.seen["identity"] is loop.ctx["identity"]
    assert hook.seen["identity"].model_id == "resolved-model"
    assert hook.seen["identity"].task_id == "identity-task"
    assert hook.seen["llm_model"] == "resolved-model"


async def test_helper_models_share_llm_interception_and_receive_a_fresh_run_context():
    from spectragent.core.aop.advice import Intercept
    from spectragent.core.hooks._registrar import HookBase

    class PromptOwner(HookBase):
        __intercepts__ = [Intercept("ainvoke", role="llm", advice="around")]

        def __init__(self):
            super().__init__()
            self.helper_llms = [MockLLM()]
            self.contexts = []
            self.interceptions = 0

        def bind_prompt_models(self, models, call_context):
            self.models = models
            self.contexts.append(call_context)

        async def around_ainvoke(self, proceed, *args, **kwargs):
            self.interceptions += 1
            return await proceed()

    owner = PromptOwner()
    runtime = AgentRuntime.for_agent(
        _mk(SimpleAgent(), _SpyLoop("helper", []), _make_env(), hooks=[owner]), AGENT_ID
    )
    runtime.prepare()
    first = owner.contexts[-1]
    await owner.models[0].ainvoke([])
    assert owner.interceptions == 1
    assert owner.helper_llms[0] is not owner.models[0]
    runtime.prepare()
    assert owner.contexts[-1] is not first
    await owner.models[0].ainvoke([])
    assert owner.interceptions == 2


async def test_an_earlier_lifecycle_failure_cannot_skip_helper_model_cleanup():
    from spectragent.core.hooks._registrar import HookBase

    class Model(MockLLM):
        inference_kind = "chat"

        def __init__(self):
            self.calls = 0
            self.closed = 0

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            return await super().ainvoke(messages, **kwargs)

        async def aclose(self, *, timeout=5.0):
            self.closed += 1
            return ()

    class EarlyFailure(HookBase):
        priority = 0

        async def on_run_end(self, **kwargs):
            raise RuntimeError("earlier lifecycle failed")

    class PromptOwner(HookBase):
        priority = 100

        def __init__(self, helper):
            super().__init__()
            self.helper_llms = [helper]
            self.end_called = False

        def bind_prompt_models(self, models, call_context):
            self.models = models

        async def on_run_start(self, **kwargs):
            await self.models[0].ainvoke([])

        async def on_run_end(self, **kwargs):
            self.end_called = True
            await self.helper_llms[0].aclose()

    main, helper = Model(), Model()
    owner = PromptOwner(helper)
    run = make_run(
        agent=SimpleAgent(),
        loop=ReactLoop(),
        env=_make_env(),
        llm=main,
        memory=MockMemory(),
        hooks=[EarlyFailure(), owner],
    )
    with pytest.raises(RuntimeError, match="earlier lifecycle failed"):
        await AgentRuntime.for_agent(run, AGENT_ID).execute("task")
    assert (main.calls, helper.calls) == (1, 1)
    assert not owner.end_called
    assert (main.closed, helper.closed) == (1, 1)


@pytest.mark.parametrize("ending", ["error", "cancel", "veto"])
async def test_startup_failure_drains_handlers_and_closes_owned_models(ending):
    from spectragent.core.hooks._registrar import HookBase
    from spectragent.core.loops.errors import RunAbort

    events = []
    failure = {
        "error": RuntimeError("startup failed"),
        "cancel": asyncio.CancelledError("startup cancelled"),
        "veto": RunAbort("startup vetoed"),
    }[ending]

    class Model(MockLLM):
        inference_kind = "chat"

        def __init__(self, label):
            self.label = label
            self.calls = 0

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            return await super().ainvoke(messages, **kwargs)

        async def aclose(self, *, timeout=5.0):
            events.append(f"close:{self.label}")
            return ()

    class BackgroundStart(HookBase):
        priority = 0

        @property
        def blocking(self):
            return False

        async def on_run_start(self, **kwargs):
            await asyncio.sleep(0)
            events.append("background finished")

    class FailingStart(HookBase):
        priority = 10

        def __init__(self, helper):
            super().__init__()
            self.helper_llms = [helper]

        def bind_prompt_models(self, models, call_context):
            self.models = models

        async def on_run_start(self, *, llm, **kwargs):
            await llm.ainvoke([])
            await self.models[0].ainvoke([])
            raise failure

        async def on_run_end(self, **kwargs):
            events.append("end")

    main, helper = Model("main"), Model("helper")
    run = make_run(
        agent=SimpleAgent(),
        loop=ReactLoop(),
        env=_make_env(),
        llm=main,
        memory=MockMemory(),
        hooks=[BackgroundStart(), FailingStart(helper)],
    )
    with pytest.raises(type(failure)) as caught:
        await AgentRuntime.for_agent(run, AGENT_ID).execute("task", raise_on_error=False)
    assert caught.value is failure
    assert (main.calls, helper.calls) == (1, 1)
    assert events[0] == "background finished"
    assert sorted(events[1:]) == ["close:helper", "close:main"]

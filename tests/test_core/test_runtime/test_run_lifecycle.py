"""AgentRuntime owns the run lifecycle — F5.

Covers the guarantees that used to be caller conventions (or absent):
env setup/teardown ordering, on_run_end on *every* termination path
(external cancellation included), memory reset between tasks, hook
priority ordering, and the identity payload a persistence hook needs.
"""

from __future__ import annotations

import asyncio
import errno
from typing import Any

import pytest

from spectragent.core.aop import Intercept
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.loops import ReactLoop
from spectragent.core.loops.errors import RunAbort, carry_outcome
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.step_record import StepRecord
from tests._helpers.assembly import AGENT_ID, make_env, make_run
from tests._helpers.trace_builder import agent_result


class _DoneCap(CapabilityBase):
    __registry_key__ = "lifecycle_done_cap"
    __wiring__ = {"action": ("action", ["lifecycle_done_cap.done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="lifecycle_done_cap.done", description="Done")]

    async def execute_action(self, action):
        return ActionResult(success=True, is_done=True, extracted_content="answer")

    async def setup(self, *, negotiation=None, start_url=None):
        # ``start_url`` is *declared*, which is what makes the environment
        # willing to route it here.
        self.setup_kwargs = {"negotiation": negotiation, "start_url": start_url}
        await super().setup(negotiation=negotiation)


class _Env(EnvironmentBase):
    __registry_key__ = "lifecycle_env"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.events: list[str] = []
        self.setup_kwargs: dict[str, Any] = {}
        self.alive = True

    async def setup(self, context=None, *, negotiation=None):
        self.events.append("setup")
        self.setup_kwargs = {"context": context, "negotiation": negotiation}
        await super().setup(context, negotiation=negotiation)

    async def teardown(self):
        self.events.append("teardown")
        self.alive = False
        await super().teardown()

    @property
    def is_alive(self) -> bool:
        return self.alive


class _FailingSetupEnv(_Env):
    __registry_key__ = "lifecycle_failing_env"

    async def setup(self, context=None, *, negotiation=None):
        self.events.append("setup")
        raise RuntimeError("browser refused to start")


class _HangingSetupEnv(_Env):
    """An environment still coming up when the run is cancelled."""

    __registry_key__ = "lifecycle_hanging_setup_env"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.setup_started = asyncio.Event()

    async def setup(self, context=None, *, negotiation=None):
        self.events.append("setup")
        self.setup_started.set()
        await asyncio.sleep(100)


class _AcquiringCap(_DoneCap):
    """Stands in for ``BrowserCapability``: acquires first, fails later.

    ``BrowserCapability.setup`` opens the backend session and starts the
    CDP websocket and *then* checks the task's ``start_url``, so a
    failure there leaves a live Chrome process and a paid provider
    session behind.
    """

    __registry_key__ = "lifecycle_acquiring_cap"
    boom = False

    def __init__(self, **kw):
        super().__init__(**kw)
        self.log: list[str] = []

    async def setup(self, *, negotiation=None, start_url=None):
        self.log.append("acquire")  # = backend.open() + cdp.start()
        if self.boom:
            raise RuntimeError("could not open start_url 'https://flaky.example'")
        await super().setup(negotiation=negotiation, start_url=start_url)

    async def teardown(self):
        self.log.append("release")
        await super().teardown()


class _FailingAcquiringCap(_AcquiringCap):
    __registry_key__ = "lifecycle_failing_cap"
    boom = True


class _Agent:
    __wiring__ = {"action": ("action", ["lifecycle_done_cap.done"])}

    def __init__(self):
        self.prepare_calls = 0

    def prepare(self, *, task, action_descriptors):
        self.prepare_calls += 1
        return [{"role": "system", "content": "sys"}]

    def build_react_prompt(self, obs):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out):
        return [Action(name="lifecycle_done_cap.done")]

    def get_react_output_model(self):
        return None


class _Completion:
    completion = "done"
    usage = None


class _LLM:
    model = "test-model"

    async def ainvoke(self, messages, **kwargs):
        return _Completion()


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

    def __init__(self):
        self.messages: list[Any] = []
        self.clears = 0

    def add_message(self, msg):
        self.messages.append(msg)

    def add_messages(self, msgs):
        self.messages.extend(msgs)

    def get_context_messages(self):
        return list(self.messages)

    def clear(self):
        self.clears += 1
        self.messages.clear()


class _SlowLoop:
    loop_id = "lifecycle_slow"
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


class _BoomLoop(_SlowLoop):
    loop_id = "lifecycle_boom"

    def weave(self, agent, **deps):
        async def run(task, **kw):
            raise RuntimeError("loop exploded")

        agent.run = run
        return agent


class _VetoLoop(_SlowLoop):
    """A run a guard vetoed: ``RunAbort`` comes out of the Loop."""

    loop_id = "lifecycle_veto"

    def weave(self, agent, **deps):
        async def run(task, **kw):
            raise RunAbort("risk-control page detected", source="lifecycle_guard")

        agent.run = run
        return agent


#: The ``error`` a vetoed run's result carries.
_VETOED = "RunAbort: lifecycle_guard: risk-control page detected"


class _CutOffLoop(_SlowLoop):
    """The run ended in *outcome*; recording its last step then hangs until cancelled.

    The way ``ReactLoop`` hands a cancellation of that recording back: the
    same cancellation, carrying the outcome (``carry_outcome``). ``None``
    carries nothing — a run cut off while still under way.
    """

    loop_id = "lifecycle_cut_off"

    def __init__(self, outcome: Any = None):
        self.outcome = outcome
        self.recording = asyncio.Event()

    def weave(self, agent, **deps):
        async def run(task, **kw):
            try:
                self.recording.set()
                await asyncio.sleep(100)  # recording the step the run ended on
            except asyncio.CancelledError as cancelled:
                if self.outcome is not None:
                    carry_outcome(cancelled, self.outcome)
                raise

        agent.run = run
        return agent


class _VetoInFlightLoop(_SlowLoop):
    """The inline abort shape: records the vetoed step inside ``except RunAbort``.

    Nothing is carried explicitly — the cancellation lands while the veto is
    being handled, so the veto is its context all the same.
    """

    loop_id = "lifecycle_veto_in_flight"

    def weave(self, agent, **deps):
        async def run(task, **kw):
            try:
                raise RunAbort("risk-control page detected", source="lifecycle_guard")
            except RunAbort:
                await asyncio.sleep(100)  # recording the vetoed step
                raise

        agent.run = run
        return agent


def _veto() -> RunAbort:
    return RunAbort("risk-control page detected", source="lifecycle_guard")


#: The note a veto gets when the run's own timeout (0.05s) cut its step's recording off.
_CUT_OFF_NOTE = (
    "The run's own timeout (0.05s) expired while the vetoed step was still being recorded; "
    "the veto stands."
)

#: The ``error`` of such a run's result.
_VETOED_UNDER_TIMEOUT = (
    f"{_VETOED} (the run's timeout of 0.05s expired while the vetoed step was being recorded)"
)


class _GatedLoop(_SlowLoop):
    """Says when the run has started, then runs until something stops it."""

    loop_id = "lifecycle_gated"

    def __init__(self):
        self.started = asyncio.Event()

    def weave(self, agent, **deps):
        async def run(task, **kw):
            self.started.set()
            await asyncio.sleep(100)

        agent.run = run
        return agent


class _SlowCloseOutHook(HookBase):
    """Still inside ``on_run_end`` when the cancellation lands."""

    __registry_key__ = "lifecycle_slow_close_out"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered = asyncio.Event()
        self.calls: list[str] = []

    async def on_run_end(self, **kw):
        self.calls.append(f"end(error={kw['result'].error!r})")
        self.entered.set()
        await asyncio.sleep(100)

    async def on_run_error(self, **kw):
        self.calls.append(f"error({type(kw['error']).__name__})")


class _FailingCloseOutHook(HookBase):
    """A blocking hook whose ``on_run_end`` hits a full disk."""

    __registry_key__ = "lifecycle_failing_close_out"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls: list[str] = []

    async def on_run_end(self, **kw):
        self.calls.append(f"end(error={kw['result'].error!r})")
        raise OSError(errno.ENOSPC, "No space left on device")

    async def on_run_error(self, **kw):
        self.calls.append(f"error({type(kw['error']).__name__})")


class _SecondRunHangsLoop(_SlowLoop):
    """Finishes the first run it drives; every later one hangs until cancelled."""

    loop_id = "lifecycle_second_run_hangs"

    def __init__(self):
        self.runs = 0
        self.hanging = asyncio.Event()

    def weave(self, agent, **deps):
        async def run(task, **kw):
            self.runs += 1
            if self.runs == 1:
                return None
            self.hanging.set()
            await asyncio.sleep(100)

        agent.run = run
        return agent


class _RecordingHook(HookBase):
    """Not registered — instantiated directly by these tests."""

    __registry_key__ = "lifecycle_recording_hook"

    def __init__(self, log: list[str], **kw):
        super().__init__(**kw)
        self.log = log
        self.start_kwargs: dict[str, Any] = {}
        self.end_kwargs: dict[str, Any] = {}
        self.error_kwargs: dict[str, Any] = {}

    async def on_run_start(self, **kw):
        self.start_kwargs = kw
        env = kw.get("env")
        self.log.append(f"start(env_events={list(getattr(env, 'events', []))})")

    async def on_run_end(self, **kw):
        self.end_kwargs = kw
        env = kw.get("env")
        self.log.append(f"end(env_events={list(getattr(env, 'events', []))})")

    async def on_run_error(self, **kw):
        self.error_kwargs = kw
        self.log.append("error")


def _mk(agent, loop, env, *, memory=None, hooks=None):
    return make_run(
        agent=agent,
        loop=loop,
        env=env,
        llm=_LLM(),
        memory=memory or _Memory(),
        hooks=hooks,
    )


def _make_env(cls=_Env):
    return make_env(cls, {"lifecycle_done_cap": _DoneCap()})


class TestEnvLifecycle:
    async def test_execute_drives_setup_and_teardown(self):
        env = _make_env()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env), AGENT_ID)
        await runtime.execute("t", task_id="t-1")
        assert env.events == ["setup", "teardown"]

    async def test_task_context_reaches_env_setup(self):
        """TaskLoader.get_task_context() → Capability.setup(), finally wired."""
        env = _make_env()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env), AGENT_ID)
        await runtime.execute("t", task_id="t-1", env_context={"start_url": "https://example.com"})
        # The context arrives as one mapping and is routed by the env to
        # the capability that declares the key — it is not splatted at
        # ``env.setup`` as loose kwargs any more.
        assert env.setup_kwargs["context"] == {"start_url": "https://example.com"}
        cap = env.capabilities["lifecycle_done_cap"]
        assert isinstance(cap, _DoneCap)
        assert cap.setup_kwargs["start_url"] == "https://example.com"
        # Negotiation still threads through, so capabilities know the cost.
        assert cap.setup_kwargs["negotiation"] is not None

    async def test_env_alive_at_run_end_torn_down_after(self):
        log: list[str] = []
        env = _make_env()
        hook = _RecordingHook(log)
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env, hooks=[hook]), AGENT_ID)
        await runtime.execute("t", task_id="t-1")

        # on_run_end sees a live env (setup done, teardown not yet) — the
        # only moment env-state scoring can run.
        assert log == ["start(env_events=[])", "end(env_events=['setup'])"]
        assert hook.end_kwargs["env"] is env
        assert env.events == ["setup", "teardown"]

    async def test_setup_failure_still_produces_a_result(self):
        env = _make_env(_FailingSetupEnv)
        hook = _RecordingHook([])
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env, hooks=[hook]), AGENT_ID)
        result = await runtime.execute("t", task_id="t-1", raise_on_error=False)

        assert result.agent_done.value == "error"
        assert result.env_status.value == "failed"
        assert result.error is not None and "browser refused to start" in result.error
        assert hook.end_kwargs["result"] is result

    async def test_failed_setup_is_still_torn_down(self):
        """A setup that raised is exactly when teardown matters most.

        Gating teardown on a *successful* setup skipped it for every
        environment that failed part way — and those are the ones holding
        a half-built browser. ``teardown()`` must be safe on a capability
        that never came up, so the runtime always calls it once setup has
        been attempted.
        """
        env = _make_env(_FailingSetupEnv)
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env), AGENT_ID)
        await runtime.execute("t", task_id="t-1", raise_on_error=False)
        assert env.events == ["setup", "teardown"]

    async def test_teardown_skipped_when_setup_was_never_attempted(self):
        """``prepare()`` alone acquires nothing, so it owes nothing."""
        env = _make_env()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env), AGENT_ID)
        runtime.prepare()
        await runtime.teardown_environment()
        assert env.events == []

    async def test_capabilities_up_before_a_failed_setup_are_released(self):
        """The leak this gate caused: one orphaned browser per failing task.

        Cap A comes up (in production: a Chrome process plus a CDP
        websocket, or a billable cloud session), cap B raises, and
        ``EnvironmentBase.setup`` propagates. ``teardown_environment()``
        used to return early because ``_env_ready`` was never set, so cap
        A stayed up for the life of the process — and ``BenchmarkRunner``
        swallows the failure and composes a fresh environment for the next
        task, so a 300-task sweep against a flaky start URL orphaned 300
        of them.
        """
        good = _AcquiringCap()
        bad = _FailingAcquiringCap()
        env = make_env(_Env, {"good#0": good, "bad#0": bad})
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env), AGENT_ID)

        result = await runtime.execute("t", task_id="t-1", raise_on_error=False)

        assert result.agent_done.value == "error"
        assert "could not open start_url" in (result.error or "")
        assert good.log == ["acquire", "release"], (
            "a capability that came up before the failure was orphaned"
        )
        # The failing capability is released too: it had already opened
        # the backend session by the time it raised.
        assert bad.log == ["acquire", "release"]
        assert env.events == ["setup", "teardown"]

    async def test_one_capabilitys_teardown_failure_does_not_strand_the_rest(self):
        """Same leak, one level down, in the teardown loop itself."""

        class _BadTeardownCap(_AcquiringCap):
            __registry_key__ = "lifecycle_bad_teardown_cap"

            async def teardown(self):
                raise RuntimeError("cdp websocket already gone")

        first = _BadTeardownCap()
        second = _AcquiringCap()
        env = make_env(_Env, {"first#0": first, "second#0": second})
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env), AGENT_ID)

        await runtime.execute("t", task_id="t-1", raise_on_error=False)

        assert second.log == ["acquire", "release"]


class TestTerminationPaths:
    async def test_timeout_dispatches_run_end_with_result(self):
        log: list[str] = []
        hook = _RecordingHook(log)
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _SlowLoop(), _make_env(), hooks=[hook]), AGENT_ID
        )
        result = await runtime.execute("t", task_id="t-1", timeout=0.05)

        assert result.agent_done.value == "timeout"
        assert hook.end_kwargs["result"] is result
        assert isinstance(hook.error_kwargs["error"], TimeoutError)
        assert log[-2].startswith("end(")
        assert log[-1] == "error"

    async def test_exception_dispatches_run_end_then_reraises(self):
        hook = _RecordingHook([])
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _BoomLoop(), _make_env(), hooks=[hook]), AGENT_ID
        )
        with pytest.raises(RuntimeError, match="loop exploded"):
            await runtime.execute("t", task_id="t-1")

        result = hook.end_kwargs["result"]
        assert result.agent_done.value == "error"
        # The loop crashed; the browser did not. env_status must not
        # absorb every kind of failure or it stops meaning anything.
        assert result.env_status.value == "success"
        assert result.error is not None and "loop exploded" in result.error

    async def test_exception_returns_result_when_not_raising(self):
        runtime = AgentRuntime.for_agent(_mk(_Agent(), _BoomLoop(), _make_env()), AGENT_ID)
        result = await runtime.execute("t", task_id="t-1", raise_on_error=False)
        assert result.agent_done.value == "error"
        assert result.error is not None and "loop exploded" in result.error

    async def test_timeout_result_usage_is_none_not_zero(self):
        """A zero-filled usage would claim the run was measured."""
        runtime = AgentRuntime.for_agent(_mk(_Agent(), _SlowLoop(), _make_env()), AGENT_ID)
        result = await runtime.execute("t", task_id="t-1", timeout=0.05)
        assert result.metrics.usage is None

    async def test_dead_env_marks_timeout_as_env_failure(self):
        env = _make_env()
        env.alive = False
        runtime = AgentRuntime.for_agent(_mk(_Agent(), _SlowLoop(), env), AGENT_ID)
        result = await runtime.execute("t", task_id="t-1", timeout=0.05)
        assert result.env_status.value == "failed"


class TestCancellation:
    """External cancellation is a termination path like any other.

    ``asyncio.CancelledError`` is a ``BaseException``, so neither of
    ``execute``'s ``except Exception`` arms saw it: a cancelled run never
    reached ``on_run_end``, left its trace at ``running`` with no
    ``result.json``, and the prompt of the call in flight never reached
    disk — although ``on_run_end`` promised to fire on every path.
    """

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_cancelling_a_run_dispatches_end_then_error_then_reraises(
        self, raise_on_error: bool
    ):
        log: list[str] = []
        hook = _RecordingHook(log)
        env = _make_env()
        loop = _GatedLoop()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), loop, env, hooks=[hook]), AGENT_ID)

        task = asyncio.create_task(
            runtime.execute("t", task_id="t-1", timeout=30, raise_on_error=raise_on_error)
        )
        await loop.started.wait()
        task.cancel()
        # Propagates unchanged whatever ``raise_on_error`` says: the
        # cancellation belongs to whoever asked for it.
        with pytest.raises(asyncio.CancelledError):
            await task

        result = hook.end_kwargs["result"]
        assert result.agent_done.value == "error"
        assert result.error == "Execution cancelled"
        assert result.metrics.usage is None
        assert isinstance(hook.error_kwargs["error"], asyncio.CancelledError)
        # on_run_end first, against a live environment; teardown after.
        assert log == ["start(env_events=[])", "end(env_events=['setup'])", "error"]
        assert env.events == ["setup", "teardown"]

    async def test_cancelling_during_env_setup_still_closes_the_run_out(self):
        hook = _RecordingHook([])
        env = make_env(_HangingSetupEnv, {"lifecycle_done_cap": _DoneCap()})
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env, hooks=[hook]), AGENT_ID)

        task = asyncio.create_task(runtime.execute("t", task_id="t-1"))
        await env.setup_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert hook.end_kwargs["result"].error == "Execution cancelled"
        assert isinstance(hook.error_kwargs["error"], asyncio.CancelledError)
        # A setup that was attempted may hold resources — it is torn down.
        assert env.events == ["setup", "teardown"]

    async def test_a_cancellation_inside_the_close_out_does_not_dispatch_it_again(self):
        hook = _SlowCloseOutHook()
        env = _make_env()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env, hooks=[hook]), AGENT_ID)

        task = asyncio.create_task(runtime.execute("t", task_id="t-1"))
        await hook.entered.wait()  # the run finished; its close-out is under way
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The successful run's close-out, once — no second pass reporting
        # the same run as "Execution cancelled".
        assert hook.calls == ["end(error=None)"]
        assert env.events == ["setup", "teardown"]

    async def test_the_timeout_path_is_not_a_cancellation(self):
        """``wait_for`` cancels the loop internally; that must still read as a timeout."""
        hook = _RecordingHook([])
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _SlowLoop(), _make_env(), hooks=[hook]), AGENT_ID
        )

        result = await runtime.execute("t", task_id="t-1", timeout=0.05)

        assert result.agent_done.value == "timeout"
        assert result.error == "Execution timed out after 0.05s"
        assert hook.end_kwargs["result"] is result
        assert isinstance(hook.error_kwargs["error"], TimeoutError)

    async def test_a_callers_own_timeout_still_reads_as_a_timeout(self):
        """``asyncio.timeout`` converts only a cancellation handed back unchanged.

        Swallowing it — returning the ERROR result instead — would make the
        caller's timeout block exit normally, as if the run had finished.
        """
        hook = _RecordingHook([])
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _SlowLoop(), _make_env(), hooks=[hook]), AGENT_ID
        )

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await runtime.execute("t", task_id="t-1", timeout=30, raise_on_error=False)

        assert hook.end_kwargs["result"].error == "Execution cancelled"

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_a_close_out_that_fails_under_cancellation_does_not_replace_it(
        self, raise_on_error: bool, caplog: pytest.LogCaptureFixture
    ):
        """Ctrl-C must stay Ctrl-C when ``TraceWriter.on_run_end`` hits a full disk.

        The hook's ``OSError`` used to replace the ``CancelledError``: a sweep's
        ``except Exception`` recorded the task as failed and started the next
        one, and the cancellation survived only as ``__context__``.
        """
        hook = _FailingCloseOutHook()
        env = _make_env()
        loop = _GatedLoop()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), loop, env, hooks=[hook]), AGENT_ID)

        task = asyncio.create_task(
            runtime.execute("t", task_id="t-1", timeout=30, raise_on_error=raise_on_error)
        )
        await loop.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task.cancelled()
        assert hook.calls == ["end(error='Execution cancelled')"]
        assert env.events == ["setup", "teardown"]
        assert "closing out a cancelled run failed" in caplog.text

    async def test_a_cancellation_inside_a_vetoed_runs_close_out_outranks_the_veto(self):
        """The veto's precedence over its close-out stops at a cancellation.

        A Ctrl-C landing while the vetoed run is being closed out is still
        the cancellation the caller asked for — not swallowed as one more
        close-out failure noted on the veto.
        """
        hook = _SlowCloseOutHook()
        env = _make_env()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), _VetoLoop(), env, hooks=[hook]), AGENT_ID)

        task = asyncio.create_task(runtime.execute("t", task_id="t-1", raise_on_error=False))
        await hook.entered.wait()  # the veto's close-out is under way
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task.cancelled()
        assert hook.calls == [f"end(error={_VETOED!r})"]
        assert env.events == ["setup", "teardown"]

    async def test_a_callers_timeout_survives_a_failing_close_out(self):
        hook = _FailingCloseOutHook()
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _SlowLoop(), _make_env(), hooks=[hook]), AGENT_ID
        )

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await runtime.execute("t", task_id="t-1", timeout=30, raise_on_error=False)

        assert hook.calls == ["end(error='Execution cancelled')"]

    async def test_a_reused_runtime_closes_a_cancelled_second_run_out(self):
        """The close-out guard is per run: one finished run must not disarm the next."""
        hook = _RecordingHook([])
        loop = _SecondRunHangsLoop()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), loop, _make_env(), hooks=[hook]), AGENT_ID)
        await runtime.execute("first", task_id="t-1")

        task = asyncio.create_task(runtime.execute("second", task_id="t-2", timeout=30))
        await loop.hanging.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert hook.end_kwargs["result"].error == "Execution cancelled"
        assert isinstance(hook.error_kwargs["error"], asyncio.CancelledError)
        # start, end for the first run; start, end, error for the second.
        assert [entry.split("(")[0] for entry in hook.log] == [
            "start",
            "end",
            "start",
            "end",
            "error",
        ]


class TestTheCloseOutRunsOnce:
    """A blocking hook that raises while closing a run out is not a second ending.

    The success path's ``_finish`` sat inside the ``try`` whose ``except
    Exception`` builds a failed result, so a hook raising in ``on_run_end``
    closed the same run out again — every hook saw ``on_run_end`` twice,
    the second time with the hook's own error as the run's.
    """

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_a_finished_run(self, raise_on_error: bool):
        hook = _FailingCloseOutHook()
        env = _make_env()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), ReactLoop(), env, hooks=[hook]), AGENT_ID)

        with pytest.raises(OSError, match="No space left"):
            await runtime.execute("t", task_id="t-1", raise_on_error=raise_on_error)

        assert hook.calls == ["end(error=None)"]
        assert env.events == ["setup", "teardown"]

    async def test_a_timed_out_run(self):
        hook = _FailingCloseOutHook()
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _SlowLoop(), _make_env(), hooks=[hook]), AGENT_ID
        )

        with pytest.raises(OSError, match="No space left"):
            await runtime.execute("t", task_id="t-1", timeout=0.05)

        assert hook.calls == ["end(error='Execution timed out after 0.05s')"]

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_a_failed_run(self, raise_on_error: bool):
        """An ordinary failure has no precedence over its close-out: the hook's error is raised."""
        hook = _FailingCloseOutHook()
        env = _make_env()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), _BoomLoop(), env, hooks=[hook]), AGENT_ID)

        with pytest.raises(OSError, match="No space left"):
            await runtime.execute("t", task_id="t-1", raise_on_error=raise_on_error)

        assert hook.calls == ["end(error='RuntimeError: loop exploded')"]
        assert env.events == ["setup", "teardown"]

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_a_vetoed_run(self, raise_on_error: bool, caplog: pytest.LogCaptureFixture):
        """A veto outranks a close-out that fails under it, as a cancellation does.

        ``TraceWriter.on_run_end`` hitting a disk that is still full used to
        replace the ``RunAbort``: ``execute(raise_on_error=False)`` raised the
        ``OSError``, a sweep's ``except Exception`` recorded the task as an
        ordinary failure and started the next one — against the site the
        guard had just refused.
        """
        hook = _FailingCloseOutHook()
        env = _make_env()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), _VetoLoop(), env, hooks=[hook]), AGENT_ID)

        with pytest.raises(RunAbort, match="risk-control page detected") as caught:
            await runtime.execute("t", task_id="t-1", raise_on_error=raise_on_error)

        assert caught.value.__notes__ == [
            "Closing the vetoed run out failed too: "
            f"OSError: [Errno {errno.ENOSPC}] No space left on device"
        ]
        # Closed out once, and the failure logged — not retried, not dropped.
        assert hook.calls == [f"end(error={_VETOED!r})"]
        assert "closing out a vetoed run failed" in caplog.text
        assert env.events == ["setup", "teardown"]


class TestAnOutcomeOutranksTheRunsOwnTimeout:
    """The run's own timeout cannot replace an outcome the run had already reached.

    A Loop records the step its run ended on before the outcome leaves it,
    and that hand-over can be slow — ``TraceWriter`` pulls the environment's
    evidence then, of the very page the guard refused. A timeout landing
    there used to win: ``execute`` returned an ordinary timeout result
    whatever ``raise_on_error`` said, a sweep recorded it and went on to the
    next task, and nothing said a guard had said stop. A cancellation from
    outside still outranks the veto (``TestCancellation``).
    """

    @pytest.mark.parametrize("raise_on_error", [True, False])
    @pytest.mark.parametrize("shape", ["carried", "in flight"])
    async def test_a_veto_is_still_raised(
        self, raise_on_error: bool, shape: str, caplog: pytest.LogCaptureFixture
    ):
        hook = _RecordingHook([])
        env = _make_env()
        veto = _veto()
        loop = _CutOffLoop(veto) if shape == "carried" else _VetoInFlightLoop()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), loop, env, hooks=[hook]), AGENT_ID)

        with pytest.raises(RunAbort, match="risk-control page detected") as caught:
            await runtime.execute("t", task_id="t-1", timeout=0.05, raise_on_error=raise_on_error)

        if shape == "carried":
            assert caught.value is veto
        assert caught.value.__notes__ == [_CUT_OFF_NOTE]
        result = hook.end_kwargs["result"]
        assert result.agent_done == AgentDoneStatus.ERROR
        assert result.error == _VETOED_UNDER_TIMEOUT
        # Closed out as a vetoed run: on_run_error gets the veto, not a timeout.
        assert hook.error_kwargs["error"] is caught.value
        assert "the veto stands" in caplog.text
        assert env.events == ["setup", "teardown"]

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_a_result_the_loop_had_built_is_still_the_result(self, raise_on_error: bool):
        """A dead browser cut off mid-recording is not a slow model."""
        hook = _RecordingHook([])
        built = agent_result(
            task_id="t-1",
            env_status=EnvironmentStatus.FAILED,
            agent_done=AgentDoneStatus.ERROR,
            agent_reported_success=None,
            error="observe failed at step 1: ConnectionError: CDP websocket closed",
        )
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _CutOffLoop(built), _make_env(), hooks=[hook]), AGENT_ID
        )

        result = await runtime.execute(
            "t", task_id="t-1", timeout=0.05, raise_on_error=raise_on_error
        )

        assert result.agent_done == AgentDoneStatus.ERROR
        assert result.env_status == EnvironmentStatus.FAILED
        assert result.error == (
            "observe failed at step 1: ConnectionError: CDP websocket closed "
            "(recording this step was cut off by the run's timeout of 0.05s)"
        )
        # Closed out the way the Loop would have returned it: no on_run_error.
        assert hook.end_kwargs["result"] is result
        assert hook.error_kwargs == {}

    async def test_a_run_still_under_way_is_an_ordinary_timeout(self):
        hook = _RecordingHook([])
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _CutOffLoop(None), _make_env(), hooks=[hook]), AGENT_ID
        )
        result = await runtime.execute("t", task_id="t-1", timeout=0.05, raise_on_error=False)

        assert result.agent_done == AgentDoneStatus.TIMEOUT
        assert result.error == "Execution timed out after 0.05s"
        assert isinstance(hook.error_kwargs["error"], TimeoutError)

    async def test_an_outcome_it_does_not_know_is_ignored(self):
        """Only a veto or an ``AgentResult`` can end a run; anything else is a plain timeout."""
        odd: Any = object()
        runtime = AgentRuntime.for_agent(_mk(_Agent(), _CutOffLoop(odd), _make_env()), AGENT_ID)
        result = await runtime.execute("t", task_id="t-1", timeout=0.05)
        assert result.agent_done == AgentDoneStatus.TIMEOUT

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_an_outside_cancellation_still_outranks_the_veto(self, raise_on_error: bool):
        """Ctrl-C landing in the same place belongs to whoever pressed it."""
        hook = _RecordingHook([])
        loop = _CutOffLoop(_veto())
        runtime = AgentRuntime.for_agent(_mk(_Agent(), loop, _make_env(), hooks=[hook]), AGENT_ID)

        task = asyncio.create_task(
            runtime.execute("t", task_id="t-1", timeout=30, raise_on_error=raise_on_error)
        )
        await loop.recording.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task.cancelled()
        assert hook.end_kwargs["result"].error == "Execution cancelled"
        assert isinstance(hook.error_kwargs["error"], asyncio.CancelledError)

    async def test_a_close_out_that_fails_under_it_is_one_more_note(
        self, caplog: pytest.LogCaptureFixture
    ):
        hook = _FailingCloseOutHook()
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _CutOffLoop(_veto()), _make_env(), hooks=[hook]), AGENT_ID
        )

        with pytest.raises(RunAbort) as caught:
            await runtime.execute("t", task_id="t-1", timeout=0.05, raise_on_error=False)

        assert caught.value.__notes__ == [
            _CUT_OFF_NOTE,
            "Closing the vetoed run out failed too: "
            f"OSError: [Errno {errno.ENOSPC}] No space left on device",
        ]
        # Closed out once, as the vetoed run it was.
        assert hook.calls == [f"end(error={_VETOED_UNDER_TIMEOUT!r})"]
        assert "closing out a vetoed run failed" in caplog.text


class TestRunStartPayload:
    async def test_identity_reaches_hooks(self):
        hook = _RecordingHook([])
        agent = _Agent()
        run = _mk(agent, ReactLoop(), _make_env(), hooks=[hook])
        runtime = AgentRuntime.for_agent(run, AGENT_ID)
        await runtime.execute(
            "find the price",
            task_id="task-42",
            benchmark="online_mind2web",
            subset="Hard",
            run_id="run-7",
        )

        kw = hook.start_kwargs
        assert kw["task_id"] == "task-42"
        assert kw["benchmark"] == "online_mind2web"
        assert kw["subset"] == "Hard"
        assert kw["run_id"] == "run-7"
        assert kw["loop"] is run.instance("agent_loop", "loop#0")
        assert kw["llm"] is run.instance("llm", "llm#0")


class TestMemoryReset:
    async def test_memory_cleared_per_execute(self):
        """Task N's trajectory must not leak into task N+1."""
        memory = _Memory()
        agent = _Agent()
        runtime = AgentRuntime.for_agent(
            _mk(agent, ReactLoop(), _make_env(), memory=memory), AGENT_ID
        )
        await runtime.execute("first", task_id="t-1")
        first_len = len(memory.messages)
        await runtime.execute("second", task_id="t-2")

        assert memory.clears == 2
        assert agent.prepare_calls == 2
        # Not 2x: the second run started from an empty context.
        assert len(memory.messages) == first_len
        assert (
            sum(
                1 for m in memory.messages if getattr(m, "get", lambda _k: None)("role") == "system"
            )
            == 1
        )


class TestNonBlockingHooksAreDrained:
    """``blocking=False`` must mean "not waited for", never "never run".

    A lifecycle callback spawned with a discarded ``create_task`` handle
    is abandoned when the run's loop moves on: the handler never
    executes, and CPython may collect the task mid-flight. The run owns
    those tasks now and drains them before teardown, so the env a
    handler was handed is still alive when it looks at it.
    """

    async def test_a_nonblocking_lifecycle_callback_completes(self):
        fired: list[str] = []

        class _Monitor(HookBase):
            __registry_key__ = "lifecycle_nonblocking_monitor"

            @property
            def blocking(self) -> bool:
                return False

            async def on_run_end(self, **kw):
                # Yields control, so nothing but a drain can get it back.
                await asyncio.sleep(0.01)
                env = kw.get("env")
                fired.append(f"end(env_alive={getattr(env, 'alive', None)})")

        env = _make_env()
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), ReactLoop(), env, hooks=[_Monitor()]), AGENT_ID
        )
        await runtime.execute("t", task_id="t-1")

        # Drained before teardown — the env-alive contract on_run_end has.
        assert fired == ["end(env_alive=True)"]
        assert env.events == ["setup", "teardown"]

    async def test_a_nonblocking_method_intercept_handler_completes(self):
        """The other half, and the one nothing was watching.

        A lifecycle callback is spawned by ``AgentRuntime`` itself, so it
        lands on the run's own ``BackgroundTasks`` no matter what. A
        ``__intercepts__`` handler is spawned by the ``Weaver``, which
        only shares that owner because the runtime passes it in — delete
        that one argument and the handler goes back to being an
        abandoned ``create_task``, with every test still green.
        """
        fired: list[str] = []

        class _Watcher(HookBase):
            __registry_key__ = "lifecycle_nonblocking_intercept"
            __intercepts__ = [Intercept("ainvoke", role="llm", advice="after")]

            @property
            def blocking(self) -> bool:
                return False

            async def after_ainvoke(self, result: Any, **kw):
                await asyncio.sleep(0.01)
                fired.append("ainvoke")

        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), ReactLoop(), _make_env(), hooks=[_Watcher()]), AGENT_ID
        )
        await runtime.execute("t", task_id="t-1")

        assert fired == ["ainvoke"], (
            "the run ended with a spawned intercept handler that never ran — "
            "the Weaver is not sharing the run's BackgroundTasks"
        )

    async def test_a_failing_nonblocking_callback_does_not_fail_the_run(self):
        class _Broken(HookBase):
            __registry_key__ = "lifecycle_nonblocking_broken"

            @property
            def blocking(self) -> bool:
                return False

            async def on_run_end(self, **kw):
                raise RuntimeError("websocket died")

        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), ReactLoop(), _make_env(), hooks=[_Broken()]), AGENT_ID
        )
        result = await runtime.execute("t", task_id="t-1")
        assert result.agent_done is not None


class TestHookPriority:
    async def test_enrichers_run_before_persisters(self):
        order: list[str] = []

        class _Writer(HookBase):
            __registry_key__ = "lifecycle_prio_writer"
            priority = HookBase.PRIORITY_PERSIST

            async def on_run_end(self, **kw):
                order.append("write")

        class _Enricher(HookBase):
            __registry_key__ = "lifecycle_prio_enricher"
            priority = HookBase.PRIORITY_ENRICH

            async def on_run_end(self, **kw):
                order.append("enrich")

        # Declared in the *wrong* order on purpose — YAML order must not
        # decide which hook sees the result first.
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), ReactLoop(), _make_env(), hooks=[_Writer(), _Enricher()]),
            AGENT_ID,
        )
        await runtime.execute("t", task_id="t-1")
        assert order == ["enrich", "write"]


class TestPartialSteps:
    async def test_partial_steps_carries_surrendered_records(self):
        """on_run_error's contract is fulfillable now, not structurally empty."""

        class _TwoStepThenBoomLoop(_SlowLoop):
            loop_id = "lifecycle_two_step"

            def weave(self, agent, **deps):
                recorder = deps["step_recorder"]

                async def run(task, **kw):
                    await recorder.record_step(StepRecord(step=0), observation=None)
                    await recorder.record_step(StepRecord(step=1), observation=None)
                    raise RuntimeError("died at step 2")

                agent.run = run
                return agent

        hook = _RecordingHook([])
        runtime = AgentRuntime.for_agent(
            _mk(_Agent(), _TwoStepThenBoomLoop(), _make_env(), hooks=[hook]), AGENT_ID
        )
        result = await runtime.execute("t", task_id="t-1", raise_on_error=False)

        partial = hook.error_kwargs["partial_steps"]
        assert [r.step for r in partial] == [0, 1]
        assert result.metrics.steps == 2
        assert runtime.step_records == partial

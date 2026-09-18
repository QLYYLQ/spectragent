"""AgentRuntime — the framework's central orchestration engine.

AgentRuntime is loop-agnostic: it handles slot validation,
three-party observation negotiation, action negotiation + guarded_act,
AOP hook weaving, loop weaving, timeout control, and error handling.

``prepare()`` assembles all dependencies and weaves the Loop into the
agent instance — after which ``agent.run(task)`` is self-contained.
``execute()`` is a convenience wrapper that calls ``prepare()`` then
drives ``agent.run()`` with timeout + lifecycle hooks.

Not a registrar component. Not a singleton: one AgentRuntime drives one
agent *instance*, built via ``AgentRuntime.for_agent(run, agent_id)``, so
a multi-agent run has one runtime per agent.

See docs/architecture_design.md §4.2 and docs/execution_dag.md M08.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from spectragent.core.action_guard import build_guarded_act
from spectragent.core.aop.tasks import BackgroundTasks
from spectragent.core.aop.weaver import Weaver
from spectragent.core.composer import ComposedRun, ComposeError
from spectragent.core.hooks.base import RuntimeHook
from spectragent.core.loops.errors import RunAbort
from spectragent.core.negotiation import (
    negotiate_action,
    negotiate_memory,
    negotiate_observation,
)
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.agent_result import AgentMetrics, AgentResult

logger = logging.getLogger(__name__)

_DEFAULT_HOOK_PRIORITY = 100


def _model_name(agent: Any) -> str:
    """Model identifier of an agent's LLM, for result attribution."""
    llm = getattr(agent, "llm", None)
    for attr in ("model", "model_name", "name"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _component_id(obj: Any, accessor: str) -> str:
    """Registry key of one composed component (``agent_id``, ``loop_id``…).

    Every registrar base exposes it as a property returning
    ``__registry_key__`` (see ``agents/_registrar.py``,
    ``core/loops/_registrar.py``, ``core/environments/_registrar.py``), so
    this reads the declared accessor rather than reaching for the ClassVar
    — a Loop assembled for a test, or any object satisfying the Protocol
    without subclassing the base, answers the same question the same way.

    Resolved **here**, once, because AgentRuntime is what knows the run.
    It used to be redone inside every persistence hook, which made each
    hook responsible for discovering facts it should have been handed.
    """
    value = getattr(obj, accessor, "") if obj is not None else ""
    return value if isinstance(value, str) else ""


class AgentRuntime:
    """Loop-agnostic orchestration engine.

    Responsibilities:
    1. Validate Agent-Loop slot compatibility
    2. Run three-party observation negotiation
    3. Aggregate action space + build guarded_act
    4. Weave AOP hooks into agent/env/llm proxies
    5. Weave Loop execution logic into agent.run
    6. (execute only) Drive agent.run() with timeout + lifecycle hooks

    An AgentRuntime drives exactly **one** agent instance. Its loop, llm
    and memory come from that agent's own wired receptors, never from a
    run-global lookup — that is what lets two agents of the same class
    keep separate configs. The Environment may legitimately be shared
    across agents, but sharing is explicit: with more than one
    environment in the run the caller must name one.

    Args:
        run: The composed object graph (used for run-scoped hooks).
        agent: The agent instance this runtime drives.
        env: Environment for this agent, or ``None`` for env-less loops.
    """

    def __init__(
        self,
        run: ComposedRun,
        agent: Any,
        *,
        env: Any | None = None,
    ) -> None:
        self._run = run
        self._agent = agent
        self._env = env
        self._hooks: list[RuntimeHook] = list(run.hooks)
        self._negotiated_obs: Any = None
        self._negotiated_actions: Any = None
        self._active_descriptors: list[Any] = []
        self._memory: Any = None
        self._recorder = StepRecorder()
        self._env_needs_teardown = False
        # Owns every ``blocking=False`` handler task this run spawns —
        # method interceptions via the Weaver, lifecycle callbacks
        # directly. Per-runtime, so two runs in one process drain
        # independently.
        self._background = BackgroundTasks()

    # -- Factory ----------------------------------------------

    @classmethod
    def for_agent(
        cls,
        run: ComposedRun,
        agent_id: str,
        *,
        env_id: str | None = None,
    ) -> AgentRuntime:
        """Build a runtime around one agent instance of a :class:`ComposedRun`.

        Args:
            run: The composed object graph.
            agent_id: Instance id of the agent (``ComposedRun.ids("agent")``).
            env_id: Instance id of the environment to run against. Required
                when the run holds more than one environment.
        """
        agent = run.instance("agent", agent_id)
        if env_id is not None:
            env = run.instance("environment", env_id)
        else:
            environments = run.by_layer.get("environment", {})
            if len(environments) > 1:
                raise ComposeError(
                    f"Run holds {len(environments)} environments "
                    f"({sorted(environments)}); pass env_id= to say which one "
                    f"agent {agent_id!r} runs against."
                )
            env = next(iter(environments.values()), None)
        return cls(run, agent, env=env)

    # -- Bound instances ---------------------------------------

    @property
    def agent(self) -> Any:
        return self._agent

    @property
    def env(self) -> Any | None:
        return self._env

    @property
    def negotiated_observation(self) -> Any | None:
        """Observation negotiation result, available after :meth:`prepare`."""
        return self._negotiated_obs

    @property
    def negotiated_actions(self) -> Any | None:
        """Action negotiation result, available after :meth:`prepare`.

        Kept for the same reason its observation twin is: the result is
        what says which fully-qualified action keys this run could
        execute and what each relative declaration bound to. It used to
        be a local in ``prepare()``, so nothing downstream — the trace
        included — could say what the run had actually agreed on.
        """
        return self._negotiated_actions

    @property
    def active_descriptors(self) -> list[Any]:
        """Action descriptors surviving negotiation, after :meth:`prepare`.

        Lives on the runtime rather than being stamped onto the agent:
        writing framework bookkeeping onto a domain object is how
        ``__active_descriptors__`` ended up shadowing a proxy method.
        """
        return self._active_descriptors

    @property
    def step_records(self) -> list[Any]:
        """Steps the Loop has surrendered so far — the partial trajectory."""
        return self._recorder.records

    # -- environment lifecycle ---------------------------------

    async def setup_environment(self, context: Mapping[str, Any] | None = None) -> None:
        """Set up the bound environment, threading the negotiation result.

        Must run after :meth:`prepare` — negotiation is what tells each
        capability which fragments it will actually be asked for, and
        therefore how much work an observe cycle costs. Call it before
        ``prepare`` and capabilities fall back to resolving everything
        they can.

        ``execute()`` calls this for you. ``context`` is the task context
        (``TaskLoader.get_task_context(task_id)``), which is how a
        benchmark's container image, checkout commit or start URL reaches
        a Capability. The Environment routes it per capability rather than
        splatting it at all of them.
        """
        if self._env is None:
            return
        # Armed *before* the await, not after it. ``Environment.setup``
        # brings capabilities up one at a time, and each one may already
        # hold a Chrome process, a CDP websocket or a billable cloud
        # session by the time a later one raises — as ``BrowserCapability``
        # does, which opens the backend and connects CDP and *then* can
        # still fail on the task's ``start_url``. Gating teardown on a
        # *successful* setup orphaned every resource acquired before the
        # failure, and ``BenchmarkRunner`` swallows the error and composes
        # a fresh environment for the next task, so a sweep against a
        # flaky start URL leaked one browser per task.
        #
        # The flag therefore means "the environment may be holding
        # resources", not "setup succeeded".
        self._env_needs_teardown = True
        await self._env.setup(context, negotiation=self._negotiated_obs)

    async def teardown_environment(self) -> None:
        """Tear the bound environment down, at most once.

        Runs after *any* attempted setup, including one that raised part
        way through. ``teardown()`` is required to be safe on a
        capability that never came up — every one of them is None-guarded
        — and paying for a no-op teardown is strictly cheaper than
        leaking the capabilities that did come up.
        """
        if self._env is None or not self._env_needs_teardown:
            return
        self._env_needs_teardown = False
        await self._env.teardown()

    # -- prepare: assembly phase -------------------------------

    def _loop_policy_for(self, agent_prepare: Any) -> dict[str, Any]:
        """Loop execution limits the agent must advertise in its prompt.

        The Loop is the single source of truth for how many actions a step
        may contain — it is what actually truncates. An agent that also
        owned the number would tell the model one thing while the loop
        enforced another: the shipped config advertised ``max_actions=3``
        while ``ReactLoop.max_actions_per_step`` dropped everything past
        the first, silently, so the model kept re-issuing steps it
        believed had run.

        Passed only to agents whose ``prepare`` names the parameter, so an
        agent that has no use for it keeps a narrow signature rather than
        growing a ``**kwargs`` sink.
        """
        loop = getattr(self._agent, "agent_loop", None)
        if loop is None:
            return {}
        try:
            accepted = inspect.signature(agent_prepare).parameters
        except (TypeError, ValueError):
            return {}
        policy = {"max_actions_per_step": getattr(loop, "max_actions_per_step", None)}
        return {k: v for k, v in policy.items() if k in accepted and v is not None}

    def prepare(
        self,
        *,
        task_id: str = "unknown",
        action_filter: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Assemble all dependencies and weave Loop into agent.run.

        ``loop`` / ``llm`` / ``memory`` are read off the bound agent's own
        wired receptors; ``env`` is the one bound at construction time.
        After return, ``agent.run(task)`` is fully self-contained.
        """
        agent = self._agent
        loop = agent.agent_loop
        env = self._env
        llm = agent.llm
        memory = agent.memory
        # Step 1: Slot validation
        loop._validate_slots(agent)

        # Step 2: Observation three-party negotiation
        negotiated_obs = negotiate_observation(agent, loop, env)
        # Kept so ``setup_environment`` can hand it to the capabilities:
        # a capability that does not know the negotiated set resolves
        # every fragment it *could* provide, which is how a vision-only
        # agent ends up paying for a DOM extraction on every step.
        self._negotiated_obs = negotiated_obs

        # Step 2.5: Action two-party negotiation
        negotiated_actions = negotiate_action(agent, env)
        self._negotiated_actions = negotiated_actions

        # Step 2.75: Memory protocol negotiation (Loop ⊆ Memory)
        negotiate_memory(loop, memory)

        # Step 3: AOP weaving (must happen before guarded_act so
        #          act_fn captures wrapped_env.act, enabling AOP intercepts)
        weaver = Weaver(self._hooks, background=self._background)
        wrapped_agent = weaver.wrap(agent, role="agent")
        wrapped_env = weaver.wrap(env, role="env")
        wrapped_llm = weaver.wrap(llm, role="llm")
        # The Loop's step sink. Wrapped like any other role, so a hook
        # observes finished steps through an ordinary method call.
        wrapped_recorder = weaver.wrap(self._recorder, role="loop")
        # Anything left declaring a role nobody wrapped is a config bug,
        # and used to be a silent no-op.
        weaver.raise_for_unclaimed_roles()

        # Step 4: Narrow descriptors via the action negotiation result.
        #
        # The observation result travels with it. A parameter can depend on
        # a fragment — ``browser.scroll``'s ``index`` addresses the
        # selector map ``browser.dom`` builds — and only the capability
        # knows which. Handing it the active fragment set lets it drop
        # those params before they are advertised, so a vision-only run's
        # prompt never teaches an index it could not resolve. This is the
        # same ``**context`` channel a per-task tool schema rides
        # (architecture_design.md §4.9); the negotiation rules are
        # untouched, and a capability that ignores ``fragments`` behaves
        # exactly as before.
        narrowed_descriptors = wrapped_env.get_action_space(
            active_actions=negotiated_actions.active_actions,
            fragments=negotiated_obs.active_fragments,
        )
        active_descriptors, act_fn = build_guarded_act(
            wrapped_env.act,
            narrowed_descriptors,
            action_filter,
            bindings=negotiated_actions.action_bindings,
        )

        # Step 5: Loop weaving — patches wrapped_agent.run
        loop.weave(
            wrapped_agent,
            env=wrapped_env,
            llm=wrapped_llm,
            memory=memory,
            act_fn=act_fn,
            negotiated_obs=negotiated_obs,
            active_descriptors=active_descriptors,
            step_recorder=wrapped_recorder,
            task_id=task_id,
            **kwargs,
        )

        # Only now is every intercept's fate decided: ``agent.run`` did
        # not exist until the line above installed it. An intercept that
        # can still never fire — a typo'd method, an ``env`` role in an
        # env-less run — is a config bug, and used to be a silent no-op
        # or a warning in a log nobody reads.
        weaver.audit_intercepts()

        # Negotiation outputs stay on the runtime — see
        # :attr:`active_descriptors`.
        self._active_descriptors = active_descriptors
        self._memory = memory

        return wrapped_agent

    # -- execute: convenience one-shot -------------------------

    async def execute(
        self,
        task: str,
        *,
        timeout: float = 300,
        task_id: str = "unknown",
        benchmark: str = "",
        subset: str = "",
        run_id: str | None = None,
        env_context: Mapping[str, Any] | None = None,
        action_filter: list[str] | None = None,
        config: dict[str, Any] | None = None,
        raise_on_error: bool = True,
        **kwargs: Any,
    ) -> Any:
        """One-shot: prepare + env lifecycle + run + lifecycle hooks + timeout.

        The environment's ``setup``/``teardown`` are driven here rather
        than by the caller, so the ordering — negotiate, set up, start,
        run, score against a *live* env, tear down — is a framework
        guarantee instead of a convention every demo re-implements
        differently.

        Every termination path (done, max_steps, timeout, exception)
        dispatches ``on_run_end`` with a real ``AgentResult``. Persistence
        hooks write the result there, so skipping it on the failure paths
        is what left two thirds of the traces on disk permanently marked
        ``running`` with no ``result.json``.

        Args:
            task: Task text handed to the agent.
            timeout: Wall-clock budget for ``agent.run``.
            task_id: Dataset identity of this task.
            benchmark: Benchmark registry key, recorded in the trace so it
                can be joined back to its ``EvalReference``.
            subset: Benchmark subset/split.
            run_id: Trace identity. Generated by the persistence hook when
                omitted.
            env_context: Task context handed to ``Environment.setup``
                — typically ``TaskLoader.get_task_context(task_id)``. The
                environment routes each key to the capabilities whose
                ``setup()`` declares it; a key nobody declares raises.
            action_filter: Optional extra narrowing of the action space.
            config: Config snapshot recorded in the trace.
            raise_on_error: Re-raise an unexpected exception after the
                trace is closed out. Benchmark drivers pass ``False`` to
                get the failed ``AgentResult`` back and keep going.
        """
        prepared_agent = self.prepare(
            task_id=task_id,
            action_filter=action_filter,
            **kwargs,
        )

        # A ComposedRun may drive many tasks. Without this, task 1's whole
        # trajectory — plus a second copy of the system prompt — leaks into
        # task 2's context.
        memory = self._memory
        if memory is not None and callable(getattr(memory, "clear", None)):
            memory.clear()
        self._recorder.clear()

        # Agent.prepare is a pure builder: it produces seed messages for
        # the run (typically the system prompt) and AgentRuntime owns the
        # memory write. Skipped if the agent doesn't define ``prepare``.
        agent_prepare = getattr(prepared_agent, "prepare", None)
        if callable(agent_prepare):
            seed_messages = agent_prepare(
                task=task,
                action_descriptors=self._active_descriptors,
                **self._loop_policy_for(agent_prepare),
            )
            if seed_messages and memory is not None:
                memory.add_messages(seed_messages)

        # Hook lifecycle — run_start. Deliberately *before* env setup: a
        # setup failure must still land in a trace, and that needs the
        # trace to already exist.
        #
        # Dispatched *after* ``prepare()`` so the negotiation results
        # exist: a trace that cannot say which fragments and actions were
        # live cannot explain a single step it recorded.
        #
        # The identity, the composed config and both negotiation results
        # are resolved here and passed as values. A hook that had to
        # rediscover them would be doing the runtime's job with less
        # information — and did, by ``getattr``-ing ids off the agent and
        # dropping the configuration entirely.
        await self._dispatch_lifecycle(
            "on_run_start",
            agent=self._agent,
            env=self._env,
            loop=self._agent.agent_loop,
            llm=self._agent.llm,
            task=task,
            task_id=task_id,
            benchmark=benchmark,
            subset=subset,
            run_id=run_id,
            config=config or {},
            agent_id=_component_id(self._agent, "agent_id"),
            loop_id=_component_id(self._agent.agent_loop, "loop_id"),
            env_type=_component_id(self._env, "env_type"),
            llm_model=_model_name(self._agent),
            resolved_config=self._run.top_level_cfg,
            observation_negotiation=self._negotiated_obs,
            action_negotiation=self._negotiated_actions,
        )

        started = time.monotonic()
        try:
            try:
                await self.setup_environment(env_context)
            except Exception as exc:
                logger.exception("AgentRuntime: environment setup failed")
                result = self._terminal_result(
                    task=task,
                    task_id=task_id,
                    agent_done=AgentDoneStatus.ERROR,
                    env_status=EnvironmentStatus.FAILED,
                    error=f"Environment setup failed: {type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                await self._finish(result, error=exc)
                if raise_on_error:
                    raise
                return result

            try:
                result = await asyncio.wait_for(
                    prepared_agent.run(task),
                    timeout=timeout,
                )
                await self._finish(result)
                return result

            except TimeoutError:
                logger.warning("AgentRuntime: execution timed out after %.1fs", timeout)
                result = self._terminal_result(
                    task=task,
                    task_id=task_id,
                    agent_done=AgentDoneStatus.TIMEOUT,
                    env_status=self._probe_env_status(),
                    error=f"Execution timed out after {timeout}s",
                    elapsed_ms=int(timeout * 1000),
                )
                await self._finish(result, error=TimeoutError(result.error))
                return result

            except Exception as exc:
                logger.exception("AgentRuntime: execution failed")
                result = self._terminal_result(
                    task=task,
                    task_id=task_id,
                    agent_done=AgentDoneStatus.ERROR,
                    # ``agent_done`` already says the run failed. Asking the
                    # environment keeps ``env_status`` meaning what it says
                    # — a crashed browser, not just a crashed run.
                    env_status=self._probe_env_status(),
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                await self._finish(result, error=exc)
                # ``raise_on_error=False`` means "a task that fails must not
                # stop the sweep". A veto is the opposite instruction: a guard
                # inspected this run and said *stop*, so collapsing it into a
                # returned ``AgentResult`` would make the framework overrule
                # the guard it just obeyed inside the loop. It propagates
                # either way — but only after ``_finish``, so the veto and its
                # evidence are already in the trace when it does.
                if raise_on_error or isinstance(exc, RunAbort):
                    raise
                return result
        finally:
            # Backstop drain, for the paths that never reach ``_finish``
            # (cancellation). Normally a no-op — and it must not be able
            # to skip teardown, hence the bare ``finally``.
            try:
                await self._drain_background()
            finally:
                # Teardown always runs, and always after on_run_end — env-state
                # scoring needs the environment alive.
                try:
                    await self.teardown_environment()
                except Exception:
                    logger.exception("AgentRuntime: environment teardown failed")

    # -- Private helpers ---------------------------------------

    async def _finish(self, result: Any, error: BaseException | None = None) -> None:
        """Close a run out: ``on_run_end`` first, then error detail, then drain."""
        await self._dispatch_lifecycle("on_run_end", result=result, env=self._env)
        if error is not None:
            await self._dispatch_lifecycle(
                "on_run_error",
                error=error,
                partial_steps=self._recorder.records,
                env=self._env,
            )
        # The run's non-blocking handlers end with the run. Drained here
        # rather than after teardown so a handler still holding the env
        # sees it alive — the same contract ``on_run_end`` gets — and
        # because ``asyncio.run()`` returning is not a drain: it abandons
        # every pending task, which is how a ``blocking=False`` handler
        # could be spawned and simply never execute.
        await self._drain_background()

    async def _drain_background(self) -> None:
        """Wait out this run's non-blocking handler tasks, then move on."""
        try:
            await self._background.drain()
        except Exception:
            logger.exception("AgentRuntime: draining background hook handlers failed")

    def _terminal_result(
        self,
        *,
        task: str,
        task_id: str,
        agent_done: AgentDoneStatus,
        env_status: EnvironmentStatus,
        error: str,
        elapsed_ms: int,
    ) -> AgentResult:
        """Build the result for a run the Loop never got to finish.

        ``usage=None`` is the honest value: the Loop is the only component
        that sees completions, and it did not return one. A zero-filled
        ``AgentUsage`` here would claim the run was measured.
        """
        return AgentResult(
            task_id=task_id,
            task=task,
            timestamp=datetime.now(UTC),
            env_status=env_status,
            agent_done=agent_done,
            error=error,
            model_id=_model_name(self._agent),
            metrics=AgentMetrics(
                end_to_end_ms=elapsed_ms,
                steps=self._recorder.step_count,
                usage=None,
            ),
        )

    def _probe_env_status(self) -> EnvironmentStatus:
        """Ask the environment whether it is still healthy.

        A run that timed out because the browser died and one that timed
        out because the task was hard are different failures, and used to
        be indistinguishable in the artifacts.
        """
        if self._env is None:
            return EnvironmentStatus.SUCCESS
        try:
            alive = getattr(self._env, "is_alive", True)
        except Exception:
            return EnvironmentStatus.FAILED
        return EnvironmentStatus.SUCCESS if alive else EnvironmentStatus.FAILED

    async def _dispatch_lifecycle(
        self,
        method_name: str,
        **kwargs: Any,
    ) -> None:
        """Dispatch a lifecycle callback to all hooks, in priority order.

        Ordering is load-bearing: an enricher that mutates ``result``
        must run before a writer that serializes it. Relying on YAML order
        meant ``hook: [trace_writer, metrics]`` silently wrote
        ``total_tokens: 0`` into every ``result.json``.
        """
        ordered = sorted(
            self._hooks,
            key=lambda h: getattr(h, "priority", _DEFAULT_HOOK_PRIORITY),
        )
        for hook in ordered:
            handler = getattr(hook, method_name, None)
            if handler is None:
                continue

            if hook.blocking:
                await handler(**kwargs)
            else:
                hook_id = getattr(hook, "hook_id", type(hook).__name__)
                self._background.spawn(
                    self._safe_lifecycle_call(hook, handler, method_name, kwargs),
                    label=f"lifecycle:{method_name}:{hook_id}",
                )

    @staticmethod
    async def _safe_lifecycle_call(
        hook: Any,
        handler: Any,
        method_name: str,
        kwargs: dict[str, Any],
    ) -> None:
        """Call a non-blocking hook lifecycle method, catching exceptions."""
        try:
            await handler(**kwargs)
        except Exception:
            hook_id = getattr(hook, "hook_id", type(hook).__name__)
            logger.exception(
                "Non-blocking hook '%s' failed on lifecycle '%s'",
                hook_id,
                method_name,
            )

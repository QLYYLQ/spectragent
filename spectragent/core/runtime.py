"""AgentRuntime — the framework's central orchestration engine.

AgentRuntime is loop-agnostic: it handles slot validation,
three-party observation negotiation, action negotiation + guarded_act,
memory and archive negotiation, the environment's setup check, AOP hook
weaving, loop weaving, binding the environment's archivers, timeout
control, and error handling.

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
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from spectragent.core.action_guard import build_guarded_act
from spectragent.core.aop.tasks import BackgroundTasks
from spectragent.core.aop.weaver import Weaver
from spectragent.core.archive.protocols import ArchiveConsumer
from spectragent.core.composer import ComposedRun, ComposeError
from spectragent.core.hooks.base import RuntimeHook
from spectragent.core.loops.errors import RunAbort, carried_outcome
from spectragent.core.loops.validation import MissingEnvironmentError
from spectragent.core.negotiation import (
    negotiate_action,
    negotiate_archive,
    negotiate_memory,
    negotiate_observation,
)
from spectragent.core.step_recorder import RecordingFailedError, StepRecorder
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.agent_result import AgentMetrics, AgentResult
from spectragent.schemas.config_source import ConfigSource
from spectragent.schemas.task import resolve_env_context_paths

if TYPE_CHECKING:
    from spectragent.schemas.restoration import BranchProvenance

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


async def _no_environment_act(action: Any, **_kwargs: Any) -> Any:
    """The action executor of an env-less run: never reached, its guard advertises nothing."""
    raise RuntimeError(f"this run has no environment to execute {action!r} in")


def _outcome_under(timed_out: TimeoutError) -> RunAbort | AgentResult | None:
    """What the run had already ended in when its own timeout cut the Loop off, if anything.

    ``asyncio.wait_for`` raises its ``TimeoutError`` *from* the cancellation
    it delivered into the Loop — on 3.11 and 3.12 alike, and
    ``tests/test_core/test_loops/test_errors.py`` pins it — and a Loop cut
    off while recording the step its run ended on lets that cancellation
    carry the outcome (``loops.errors.carry_outcome``). ``execute`` ends the
    run with a ``RunAbort`` or an ``AgentResult``; anything else falls
    through to an ordinary timeout.
    """
    cancelled = timed_out.__cause__
    if not isinstance(cancelled, asyncio.CancelledError):
        return None
    return carried_outcome(cancelled)


class AgentRuntime:
    """Loop-agnostic orchestration engine.

    Responsibilities:
    1. Validate Agent-Loop slot compatibility (and the Loop's inputs and
       its need for an environment)
    2. Run three-party observation negotiation
    3. Run action, memory and archive negotiation
    4. Refuse what the environment's setup would refuse for this task
       (``Environment.check_setup``)
    5. Aggregate action space + build guarded_act
    6. Weave AOP hooks into agent/env/llm proxies
    7. Weave Loop execution logic into agent.run
    8. Bind the environment's archivers (only when a hook archives)
    9. (execute only) Drive agent.run() with timeout + lifecycle hooks

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
        # The archive axis: ``None`` unless some hook asked to archive
        # (see :meth:`prepare`), plus the privacy markers its archivers
        # declare (``None`` with it) — both handed to ``on_run_start`` by name.
        self._negotiated_archive: Any = None
        self._env_evidence_privacy: list[str] | None = None
        self._active_descriptors: list[Any] = []
        self._memory: Any = None
        # The woven agent :meth:`prepare` returned — whose optional ``prepare``
        # slot :meth:`seed_messages` calls.
        self._prepared_agent: Any = None
        self._recorder = StepRecorder()
        self._env_needs_teardown = False
        # Set the moment :meth:`_finish` starts closing out the current
        # ``execute``; reset per run. It is what stops a cancellation that
        # lands *inside* the close-out from dispatching the lifecycle twice.
        self._finish_started = False
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
    def negotiated_archive(self) -> Any | None:
        """Archive negotiation result, available after :meth:`prepare`.

        ``None`` when no hook declared a non-empty ``archive`` — the run
        records no environment evidence, and the environment was never
        asked about archiving at all.
        """
        return self._negotiated_archive

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
        loop_inputs: Mapping[str, Any] | None = None,
        env_context: Mapping[str, Any] | None = None,
    ) -> Any:
        """Assemble all dependencies and weave Loop into agent.run.

        ``loop`` / ``llm`` / ``memory`` are read off the bound agent's own
        wired receptors; ``env`` is the one bound at construction time.
        After return, ``agent.run(task)`` is fully self-contained.

        *loop_inputs* are this task's inputs to the Loop
        (``TaskContext.loop``): checked against the Loop's
        ``__loop_inputs__`` right after slot validation — before any
        negotiation — and handed to the Loop as a read-only
        ``ctx["loop_inputs"]``. There is no other channel into the Loop: an
        unexpected keyword is a ``TypeError`` here, not a ``ctx`` entry
        nobody reads.

        *env_context* is this task's environment setup context
        (``TaskContext.env``, a relative ``replay_archive`` resolved first):
        after the negotiations, ``Environment.check_setup`` refuses with it
        everything setup would refuse — a key no capability accepts, and
        each capability's own checks (browser: the cooperation rows and a
        replay's consistency with its source run, §4.14). A configuration
        that cannot run therefore fails here, before ``on_run_start`` opens
        a trace and before anything is launched — on a dry run too.

        No environment: a Loop that declares ``__requires_env__`` (the
        default) is refused by name; an env-less Loop's run negotiates
        against an environment that provides nothing.

        Archiving is validated early and bound last: when a hook declares
        a non-empty ``archive`` (an
        :class:`~spectragent.core.archive.protocols.ArchiveConsumer`),
        ``negotiate_archive`` runs right after memory negotiation and
        ``env.bind_archivers`` is the final statement. With no such hook
        the environment receives no archive call at all.

        Raises:
            IncompatibleError: The agent lacks a slot the Loop requires.
            IncompatibleLoopInputError: The loop inputs do not fit what the
                Loop declares (unknown, missing or mistyped keys).
            MissingEnvironmentError: The Loop needs an environment and the
                run has none.
            IncompatibleFragmentError: A required fragment no provider here
                serves — the environment, or the Loop for a loop-source
                fragment it does not build.
            IncompatibleArchiveError: The archivers a hook asks for cannot
                be bound to this run's environment — raised before
                anything is woven, and before ``on_run_start``.
            ValueError: A setup context key no capability accepts (or any
                key, in a run with no environment).
            BrowserCooperationError: (and any capability's own refusal)
                from ``Environment.check_setup``.
        """
        agent = self._agent
        loop = agent.agent_loop
        env = self._env
        llm = agent.llm
        memory = agent.memory
        # Step 1: Slot validation
        loop._validate_slots(agent)
        # Step 1b: The task's inputs to the Loop, refused before anything
        # else is negotiated: a checkpoint handed to a Loop that does not
        # declare one must fail here, not run the task from scratch.
        inputs = MappingProxyType(dict(loop_inputs or {}))
        loop.check_loop_inputs(inputs)
        # Step 1c: An environment, when the Loop needs one. A run without
        # one used to die in observation negotiation with an AttributeError
        # on ``None``; an env-less Loop's run negotiates against nothing.
        if env is None and getattr(type(loop), "__requires_env__", True):
            raise MissingEnvironmentError(_component_id(loop, "loop_id") or type(loop).__name__)

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

        # Step 2.8: Archive negotiation (archive consumer ↔ environment).
        # Validation only, and early, so a binding error surfaces during
        # assembly — on a dry run too — before anything is woven. The
        # binding itself is the last step of this method. A run whose
        # hooks archive nothing makes no archive call on the environment
        # at all: environments without the archive surface keep working.
        # The runtime knows the consumer only through ``ArchiveConsumer``.
        consumers = [h for h in self._hooks if isinstance(h, ArchiveConsumer) and h.archive]
        if consumers:
            negotiated_archive = negotiate_archive(consumers, env)
            # Negotiation guarantees there is exactly one consumer.
            archivers = list(consumers[0].archive)
        else:
            negotiated_archive, archivers = None, []
        self._negotiated_archive = negotiated_archive
        self._env_evidence_privacy = (
            sorted({marker for archiver in archivers for marker in archiver.privacy_markers})
            if negotiated_archive is not None
            else None
        )

        # Step 2.9: Everything setup would refuse, refused now — with the
        # task's setup context, the observation negotiation and the
        # archivers about to be bound. Decided by configuration and task
        # data alone, so it fails before ``on_run_start`` opens a trace and
        # before anything is launched (a dry run sees it too); setup checks
        # again before it acquires anything. Validation only: nothing is
        # set up, bound or narrowed.
        context = resolve_env_context_paths(env_context)
        if env is not None:
            env.check_setup(context, negotiation=negotiated_obs, archivers=archivers)
        elif context:
            raise ValueError(
                f"setup key(s) {sorted(context)} were given, but this run has no environment "
                f"to set up."
            )

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
        # exactly as before. An env-less run has no action space: the guard
        # refuses every action, so its executor is never reached.
        if env is None:
            narrowed_descriptors: list[Any] = []
            execute_action: Any = _no_environment_act
        else:
            narrowed_descriptors = wrapped_env.get_action_space(
                active_actions=negotiated_actions.active_actions,
                fragments=negotiated_obs.active_fragments,
            )
            execute_action = wrapped_env.act
        action_space, act_fn = build_guarded_act(
            execute_action,
            narrowed_descriptors,
            action_filter,
            bindings=negotiated_actions.action_bindings,
        )

        active_descriptors = action_space.descriptors

        # Step 5: Loop weaving — patches wrapped_agent.run. A Loop with
        # assembly checks of its own makes them here (``BranchLoop`` checks
        # its checkpoint against this assembly), still before
        # ``on_run_start`` and before any environment setup.
        loop.weave(
            wrapped_agent,
            env=wrapped_env,
            llm=wrapped_llm,
            memory=memory,
            act_fn=act_fn,
            negotiated_obs=negotiated_obs,
            negotiated_actions=negotiated_actions,
            active_descriptors=active_descriptors,
            step_recorder=wrapped_recorder,
            task_id=task_id,
            loop_inputs=inputs,
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
        self._prepared_agent = wrapped_agent

        # Step 6: Bind the archivers — the last statement, so no later
        # assembly check can fail after a capability's state changed. On
        # the *raw* environment (no hook intercepts it), still before
        # ``on_run_start`` and ``setup_environment``: a capability decides
        # at setup whether to build its capture channel. Binding acquires
        # nothing, so a dry run that never sets up costs nothing here.
        # (``env`` is never ``None`` here — negotiation refuses an archive
        # without an environment — the check is for the type checker.)
        if negotiated_archive is not None and env is not None:
            env.bind_archivers(archivers)

        return wrapped_agent

    def seed_messages(self, task: str) -> list[Any]:
        """The run's seed messages for *task*, after :meth:`prepare` (arch §4.2).

        Calls the prepared agent's optional ``prepare`` slot with the
        negotiated, guarded descriptors this assembly advertises and the
        Loop's execution limits its signature names (:meth:`_loop_policy_for`);
        ``[]`` when the agent has no ``prepare``. Writes no memory, touches no
        environment: :meth:`execute` clears the memory and writes these, and
        the training export's student renderer seeds each context segment
        with them — the same assembly as a live run, without running one.

        Raises:
            RuntimeError: :meth:`prepare` has not run.
        """
        agent = self._prepared_agent
        if agent is None:
            raise RuntimeError("AgentRuntime.seed_messages() needs prepare() first")
        agent_prepare = getattr(agent, "prepare", None)
        if not callable(agent_prepare):
            return []
        seeds: Any = agent_prepare(
            task=task,
            action_descriptors=self._active_descriptors,
            **self._loop_policy_for(agent_prepare),
        )
        return list(seeds or [])

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
        loop_inputs: Mapping[str, Any] | None = None,
        branch: BranchProvenance | None = None,
        config_source: ConfigSource | None = None,
        action_filter: list[str] | None = None,
        config: dict[str, Any] | None = None,
        raise_on_error: bool = True,
    ) -> Any:
        """One-shot: prepare + env lifecycle + run + lifecycle hooks + timeout.

        The environment's ``setup``/``teardown`` are driven here rather
        than by the caller, so the ordering — negotiate, set up, start,
        run, score against a *live* env, tear down — is a framework
        guarantee instead of a convention every demo re-implements
        differently.

        Every termination path (done, max_steps, timeout, exception,
        external cancellation) dispatches ``on_run_end`` with a real
        ``AgentResult``. Persistence hooks write the result there, so
        skipping it on the failure paths is what left two thirds of the
        traces on disk permanently open (never closed out) with no
        ``result.json``.

        External cancellation — ``asyncio.CancelledError`` from Ctrl-C or a
        sweep being cancelled, during setup or the run — is closed out with
        an ``agent_done=ERROR``, ``error="Execution cancelled"`` result
        through the same ``on_run_end`` → ``on_run_error`` → drain, and then
        **re-raised unchanged**, whatever ``raise_on_error`` says: a caller's
        ``asyncio.timeout`` or ``TaskGroup`` must still see its own
        cancellation. That holds even when the close-out itself fails (a
        blocking hook's disk error): the failure is logged and the
        cancellation still wins, or a cancelled sweep would record one task
        as failed and go on to the next. A cancellation that arrives after
        the close-out has started does not dispatch it a second time.

        A guard's veto (``RunAbort`` out of the Loop, or out of the
        environment's setup) is closed out with an ``agent_done=vetoed``
        result and re-raised whatever ``raise_on_error`` says — and it too
        outranks a close-out that fails under it: the failure is logged and
        attached to the veto as a note, or a vetoed sweep would record one
        task as failed and go on to the next.

        A step the recorder could not record stops the run
        (``RecordingFailedError`` out of the Loop): its result is
        ``agent_done=error`` with ``error`` reading ``recording failed at
        step t: …``, and the trace says it is incomplete. A recording failure
        never replaces an outcome the run had already reached — the Loop and
        the recorder keep it (``docs/architecture_design.md`` §4.6).

        The run's own ``timeout`` is not an external cancellation, and it
        does not outrank an outcome the run had already reached. When it
        expires while the Loop is still recording the step its run ended
        on, the cancellation that cut the Loop off carries that outcome
        (``loops.errors.RunEnded``): a veto is closed out and re-raised as
        above, with the timeout as a note on it and a remark in the result's
        ``error``; a terminal result the Loop had built — an infrastructure
        failure naming its root cause, say — is closed out and returned with
        the same remark. Only a timeout that cut off a run still under way
        produces an ``agent_done=TIMEOUT`` result.

        A run is closed out once. When a blocking hook raises during the
        close-out of a run that finished, timed out or failed (other than by
        a veto), that error propagates as it is — the run is not closed out
        again as if the hook's failure had been the run's.

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
                — typically ``TaskLoader.get_task_context(task_id).env``.
                The environment routes each key to the capabilities whose
                ``setup()`` declares it. A relative ``replay_archive`` is
                resolved against the current directory first
                (``resolve_env_context_paths``), so ``prepare()`` checks, the
                trace's ``task_setup`` records and setup opens the same
                directory. ``prepare()`` checks it before anything else
                happens (``Environment.check_setup``): a key nobody declares,
                or a configuration this task cannot run with, raises before
                the trace is opened, whatever ``raise_on_error`` says.
            loop_inputs: The task's inputs to the Loop
                (``TaskContext.loop``), checked by :meth:`prepare` against
                the Loop's ``__loop_inputs__``.
            branch: Where a branch task came from (``TaskContext.branch``),
                handed to ``on_run_start`` by name and recorded as
                ``TraceConfig.branch``; the runtime does not read it.
                ``None`` for every other run.
            action_filter: Optional extra narrowing of the action space.
            config_source: Explicit source YAML file identity, if the caller has one.
                Passed through to persistence; never inferred from the resolved settings.
            config: Config snapshot recorded in the trace.
            raise_on_error: Re-raise an unexpected exception after the
                trace is closed out. Benchmark drivers pass ``False`` to
                get the failed ``AgentResult`` back and keep going.
        """
        # A relative ``replay_archive`` is relative to the launch directory,
        # whoever passed it — the CLI, a task loader, a Python caller. Made
        # absolute once, here, so ``prepare()`` checks, the trace records and
        # setup opens the same directory (§4.10).
        env_context = resolve_env_context_paths(env_context)

        prepared_agent = self.prepare(
            task_id=task_id,
            action_filter=action_filter,
            loop_inputs=loop_inputs,
            env_context=env_context,
        )

        # A ComposedRun may drive many tasks. Without this, task 1's whole
        # trajectory — plus a second copy of the system prompt — leaks into
        # task 2's context.
        memory = self._memory
        if memory is not None and callable(getattr(memory, "clear", None)):
            memory.clear()
        self._recorder.clear()

        # The seed messages (typically the system prompt) come from the
        # agent's ``prepare`` slot; AgentRuntime owns the memory write.
        seed_messages = self.seed_messages(task)
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
        # The identity, the composed config and the negotiation results
        # are resolved here and passed as values. A hook that had to
        # rediscover them would be doing the runtime's job with less
        # information — and did, by ``getattr``-ing ids off the agent and
        # dropping the configuration entirely. The same goes for the
        # archive facts: whether evidence is recorded and what its
        # archivers declare, and the setup context this task's environment
        # is started from (``task_setup`` — exactly what is routed to
        # ``setup_environment`` below; ``None`` when there is none). A
        # branch task's provenance (``branch``) rides along by name for the
        # trace; nothing here interprets it.
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
            archive_negotiation=self._negotiated_archive,
            env_evidence_privacy=(
                list(self._env_evidence_privacy) if self._env_evidence_privacy is not None else None
            ),
            task_setup=env_context,
            branch=branch,
            config_source=config_source,
        )

        started = time.monotonic()
        self._finish_started = False
        try:
            try:
                await self.setup_environment(env_context)
            except Exception as exc:
                if isinstance(exc, RunAbort):
                    # A veto raised while the environment was being set up
                    # is a veto like any other: closed out as vetoed and
                    # re-raised whatever ``raise_on_error`` says, so a sweep
                    # stops instead of recording one more failed setup and
                    # starting the next task.
                    logger.warning("AgentRuntime: run vetoed during environment setup: %s", exc)
                    result = self._terminal_result(
                        task=task,
                        task_id=task_id,
                        agent_done=AgentDoneStatus.VETOED,
                        env_status=self._probe_env_status(),
                        error=f"{type(exc).__name__}: {exc}",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                    await self._close_out_vetoed(result, exc)
                    raise
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
            except TimeoutError as timed_out:
                # The run's own timeout — but the run may already have ended
                # by then: the Loop was recording the step a guard vetoed, or
                # the step a failure ended the run on (a slow archive
                # collection of the very page the guard refused), and the
                # cancellation that cut it off carries that outcome
                # (``loops.errors.RunEnded``). The outcome came first, so it
                # is the run's; the timeout is a remark on it. Reporting a
                # timeout instead is how such a veto got lost — returned
                # whatever ``raise_on_error`` says, recorded by a sweep as
                # one more timed-out task, and followed by the next task
                # against the site the guard had just refused.
                ended = _outcome_under(timed_out)
                if isinstance(ended, RunAbort):
                    logger.warning(
                        "AgentRuntime: the run's timeout (%.1fs) expired while its vetoed step "
                        "was being recorded; the veto stands: %s",
                        timeout,
                        ended,
                    )
                    ended.add_note(
                        f"The run's own timeout ({timeout}s) expired while the vetoed step was "
                        "still being recorded; the veto stands."
                    )
                    result = self._terminal_result(
                        task=task,
                        task_id=task_id,
                        agent_done=AgentDoneStatus.VETOED,
                        env_status=self._probe_env_status(),
                        error=(
                            f"{type(ended).__name__}: {ended} (the run's timeout of {timeout}s "
                            "expired while the vetoed step was being recorded)"
                        ),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                    await self._close_out_vetoed(result, ended)
                    # From the timeout: it is why the veto leaves from here
                    # rather than from the Loop, and its traceback shows
                    # where the Loop was cut off.
                    raise ended from timed_out
                if isinstance(ended, AgentResult):
                    logger.warning(
                        "AgentRuntime: the run's timeout (%.1fs) expired while the step its run "
                        "ended on was being recorded; the run's own result stands: %s",
                        timeout,
                        ended.error,
                    )
                    cut_off = f"recording this step was cut off by the run's timeout of {timeout}s"
                    result = ended.model_copy(
                        update={"error": f"{ended.error} ({cut_off})" if ended.error else cut_off}
                    )
                    # Closed out as the Loop would have returned it.
                    await self._finish(result)
                    return result
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
                if isinstance(exc, RunAbort):
                    # A veto. ``raise_on_error=False`` means "a task that
                    # fails must not stop the sweep"; a veto is the opposite
                    # instruction: a guard inspected this run and said
                    # *stop*, so collapsing it into a returned
                    # ``AgentResult`` would make the framework overrule the
                    # guard it just obeyed inside the loop. It propagates
                    # either way — after the close-out, so the veto and its
                    # evidence are in the trace when it does, and even if
                    # that close-out fails (``_close_out_vetoed``).
                    logger.warning("AgentRuntime: run vetoed: %s", exc)
                    result = self._terminal_result(
                        task=task,
                        task_id=task_id,
                        agent_done=AgentDoneStatus.VETOED,
                        env_status=self._probe_env_status(),
                        error=f"{type(exc).__name__}: {exc}",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                    await self._close_out_vetoed(result, exc)
                    raise
                logger.exception("AgentRuntime: execution failed")
                result = self._terminal_result(
                    task=task,
                    task_id=task_id,
                    agent_done=AgentDoneStatus.ERROR,
                    # ``agent_done`` already says the run failed. Asking the
                    # environment keeps ``env_status`` meaning what it says
                    # — a crashed browser, not just a crashed run.
                    env_status=self._probe_env_status(),
                    # A step that could not be recorded stopped the run: its
                    # message already reads "recording failed at step t: …".
                    error=(
                        str(exc)
                        if isinstance(exc, RecordingFailedError)
                        else f"{type(exc).__name__}: {exc}"
                    ),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                await self._finish(result, error=exc)
                if raise_on_error:
                    raise
                return result
            else:
                # Outside the ``try`` above on purpose: a blocking hook that
                # raises while closing out a finished run must not land in
                # ``except Exception`` and close the same run out a second
                # time, as a failure, with every hook seeing ``on_run_end``
                # twice.
                await self._finish(result)
                return result

        except asyncio.CancelledError as exc:
            # Ctrl-C, a cancelled sweep: not an exception of the run's own
            # (``CancelledError`` is a ``BaseException``, so neither arm
            # above sees it), but a termination path all the same. Without
            # this the trace stayed ``writing`` with no ``result.json``, and
            # the prompt of the call in flight never reached disk. Not
            # dispatched again if the cancellation landed inside a close-out
            # that had already begun.
            if not self._finish_started:
                result = self._terminal_result(
                    task=task,
                    task_id=task_id,
                    agent_done=AgentDoneStatus.ERROR,
                    env_status=self._probe_env_status(),
                    error="Execution cancelled",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                try:
                    await self._finish(result, error=exc)
                except Exception:
                    # The cancellation outranks a close-out that failed
                    # under it (a full disk in ``TraceWriter.on_run_end``).
                    # Raising the hook's error instead would turn Ctrl-C
                    # into an ordinary task failure — a sweep's
                    # ``except Exception`` would record it and start the
                    # next task — and a caller's ``asyncio.timeout`` would
                    # see an ``OSError`` rather than its ``TimeoutError``.
                    logger.exception("AgentRuntime: closing out a cancelled run failed")
            # Unchanged, and regardless of ``raise_on_error``: the
            # cancellation belongs to whoever asked for it.
            raise
        finally:
            # Backstop drain, for a close-out that did not get as far as its
            # own drain (a blocking hook raising in ``on_run_end``, or a
            # second cancellation landing inside ``_finish``). Normally a
            # no-op — and it must not be able to skip teardown, hence the
            # bare ``finally``.
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
        self._finish_started = True
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

    async def _close_out_vetoed(self, result: Any, veto: RunAbort) -> None:
        """Close a vetoed run out; a failure doing so is noted on the veto, never raised.

        The caller re-raises *veto* afterwards. Nothing the close-out raises
        takes its place: a veto outranks a close-out that fails under it, as
        a cancellation does (a disk still full when ``TraceWriter`` flushes
        the vetoed step's calls). Raising the hook's error instead would turn
        the veto into an ordinary task failure — a sweep's ``except
        Exception`` would record it and start the next task, against the
        site the guard just refused. The failure is logged and travels on
        the veto as a note. A cancellation that lands in the close-out is a
        ``BaseException`` and is not caught: it still outranks the veto.
        """
        try:
            await self._finish(result, error=veto)
        except Exception as failure:
            logger.exception("AgentRuntime: closing out a vetoed run failed")
            veto.add_note(
                f"Closing the vetoed run out failed too: {type(failure).__name__}: {failure}"
            )

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
                steps=self._recorder.policy_step_count,
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

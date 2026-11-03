"""AgentLoop registrar — conscribe-driven auto-registration.

Each concrete Loop subclass declares ``__required_slots__`` and
``__optional_slots__`` to specify which agent methods it needs. The
base class provides a universal ``validate_slots`` check and a
``weave`` method that patches ``agent.run`` with the Loop's execution
logic.

``weave()`` is stateless: runtime dependencies (env, llm, memory,
act_fn, …) are captured in the ``agent.run`` closure, never stored on
the loop instance. One loop instance can therefore be woven onto N
agents without the Nth weave rewriting the 1st agent's environment.

See docs/execution_dag.md M03 for the registrar design.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

from conscribe import create_registrar
from pydantic import Field

from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.actions._registrar import is_loop_action
from spectragent.core.loops._loop_fragments import LOOP_FRAGMENT_KEYS
from spectragent.core.loops.actions import LOOP_ACTION_KEYS, loop_action
from spectragent.core.loops.base import AgentLoop
from spectragent.core.loops.errors import RunAbort, carry_outcome
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.run import LoopRun
from spectragent.core.loops.validation import IncompatibleError, IncompatibleLoopInputError
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult
from spectragent.schemas.observation.dynamic_model import summarize_observation
from spectragent.schemas.step_record import StepRecord

if TYPE_CHECKING:
    from spectragent.core.environments.base import Environment
    from spectragent.core.step_recorder import StepSink

logger = logging.getLogger(__name__)

AgentLoopRegistrar = create_registrar(
    "agent_loop",
    AgentLoop,
    discriminator_field="loop_id",
    inline_only=True,
)


@dataclass(frozen=True)
class LoopInput:
    """One per-task input a Loop accepts (``AgentLoopBase.__loop_inputs__``).

    A task loader supplies loop inputs as ``TaskContext.loop``; the runtime
    checks them against the Loop's declaration at ``prepare()`` and hands
    them to ``_execute`` as ``ctx["loop_inputs"]``
    (``docs/architecture_design.md`` §4.1 "Loop 输入").

    Attributes:
        type: The value's type, checked with ``isinstance`` at ``prepare()``.
        required: Whether every task must supply it.
        description: What it is, for a reader of the Loop's declaration.
    """

    type: type
    required: bool = True
    description: str = ""


@dataclass
class AgentLoopBase(metaclass=AgentLoopRegistrar.Meta):
    """Concrete base for all Loop implementations.

    Subclasses must set:
    - ``__registry_key__``: conscribe key (e.g. ``"react"``, ``"codeact"``)
    - ``__required_slots__``: set of agent method names that MUST exist
    - ``__optional_slots__``: set of agent method names used if present

    Optionally:
    - ``__requires_env__``: whether this loop needs an Environment (default
      True). ``AgentRuntime.prepare()`` refuses a run without one
      (``MissingEnvironmentError``); a Loop declaring False runs env-less,
      and negotiation then treats the missing environment as providing
      nothing.
    - ``__loop_inputs__``: the per-task inputs it accepts, name ->
      :class:`LoopInput` (default: none). A plain ``dict`` in a subclass
      body is frozen into a read-only mapping when the class is created.
    - ``__loop_fragments__``: the loop-source fragment keys it fills in
      after ``env.observe()`` (default: the four the shared
      ``_loop_fragments.loop_fragment_builders`` builds, which every shipped
      Loop populates through). Observation negotiation offers only these on
      the Loop's side, so an agent requiring another loop-source fragment is
      refused at ``prepare()``, naming the fragment and the Loop. A Loop
      that builds other loop-source fragments, or none, overrides it.
    - ``__loop_actions__``: the actions it provides and executes itself, all
      ``loop.*`` (default: ``loop.done``, which ends the run with the agent's
      answer and its claim). Action negotiation offers these on the Loop's
      side, :meth:`get_action_space` describes them and the guard hands them
      to :meth:`execute_action`, never to ``env.act``
      (``docs/architecture_design.md`` §4.5, "Loop 提供的动作"). A Loop that
      provides none overrides it.

    ``weave()`` hands runtime deps to ``_execute`` as the ``ctx``
    parameter. Nothing is stored on the loop instance, so the same loop
    instance can drive several agents concurrently.

    Example::

        class ReactLoop(AgentLoopBase):
            __registry_key__ = "react"
            __required_slots__ = {"build_react_prompt", "parse_react_output"}
            __optional_slots__ = {"get_react_output_model", "prepare"}
            __requires_env__ = True

            async def _execute(self, agent, task, *, ctx):
                env = ctx["env"]
                llm = ctx["llm"]
                ...
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __required_slots__: ClassVar[set[str]] = set()
    __optional_slots__: ClassVar[set[str]] = set()
    __required_message_protocols__: ClassVar[frozenset[MessageProtocol]] = frozenset()
    __requires_env__: ClassVar[bool] = True
    __loop_inputs__: ClassVar[Mapping[str, LoopInput]] = MappingProxyType({})
    __loop_fragments__: ClassVar[frozenset[str]] = LOOP_FRAGMENT_KEYS
    __loop_actions__: ClassVar[frozenset[str]] = LOOP_ACTION_KEYS

    id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Instance identity. Unique across the whole config; "
                "defaults to '<loop_id>#<ordinal>' when omitted."
            ),
        ),
    ] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        fragments = cls.__dict__.get("__loop_fragments__")
        if fragments is not None:
            # Frozen where it is written, like the inputs below: a bare string
            # (iterated character by character) fails at import.
            if isinstance(fragments, str) or not all(isinstance(key, str) for key in fragments):
                raise TypeError(
                    f"{cls!r}: __loop_fragments__ must be a set of fragment keys, got {fragments!r}"
                )
            cls.__loop_fragments__ = frozenset(fragments)
        actions = cls.__dict__.get("__loop_actions__")
        if actions is not None:
            # The guard routes a ``loop.*`` name to the Loop, so a key without
            # that prefix would be handed to the environment instead.
            if isinstance(actions, str) or not all(
                isinstance(key, str) and is_loop_action(key) for key in actions
            ):
                raise TypeError(
                    f"{cls!r}: __loop_actions__ must be a set of 'loop.*' action keys, "
                    f"got {actions!r}"
                )
            cls.__loop_actions__ = frozenset(actions)
        declared = cls.__dict__.get("__loop_inputs__")
        if declared is None:
            return
        # Checked and frozen where it is written: a set of names or a bare
        # type where a ``LoopInput`` belongs fails at import, and no task
        # can mutate the declaration every instance of the class shares.
        if not isinstance(declared, Mapping) or not all(
            isinstance(key, str) and isinstance(spec, LoopInput) for key, spec in declared.items()
        ):
            raise TypeError(
                f"{cls!r}: __loop_inputs__ must map input names to LoopInput, got {declared!r}"
            )
        cls.__loop_inputs__ = MappingProxyType(dict(declared))

    @property
    def loop_id(self) -> str:
        return self.__registry_key__

    # ── slot validation (shared by all loops) ────────────────

    def validate_slots(self, agent: Any) -> None:
        """Check that *agent* has all required callable slots.

        Raises:
            IncompatibleError: with the list of missing method names.
        """
        missing = sorted(
            s for s in self.__required_slots__ if not callable(getattr(agent, s, None))
        )
        if missing:
            agent_name = getattr(agent, "agent_name", type(agent).__name__)
            raise IncompatibleError(
                agent_name=agent_name,
                loop_id=self.__registry_key__,
                missing_slots=missing,
            )

    # ── loop inputs (shared by all loops) ────────────────────

    def check_loop_inputs(self, inputs: Mapping[str, Any]) -> None:
        """Check a task's loop inputs against ``__loop_inputs__``.

        Unknown keys, missing required keys, values of the wrong type and
        values of the right type this Loop still refuses
        (:meth:`loop_input_refusal`) are reported together, in one error
        naming this Loop and every key.

        Raises:
            IncompatibleLoopInputError: when anything does not fit.
        """
        declared = type(self).__loop_inputs__
        unknown = [key for key in inputs if key not in declared]
        missing = [key for key, spec in declared.items() if spec.required and key not in inputs]
        mistyped = {
            key: (declared[key].type.__name__, type(value).__name__)
            for key, value in inputs.items()
            if key in declared and not isinstance(value, declared[key].type)
        }
        refused = {
            key: reason
            for key, value in inputs.items()
            if key in declared and key not in mistyped
            if (reason := self.loop_input_refusal(key, value)) is not None
        }
        if unknown or missing or mistyped or refused:
            raise IncompatibleLoopInputError(
                self.__registry_key__,
                unknown=unknown,
                missing=missing,
                mistyped=mistyped,
                refused=refused,
                accepted=declared,
            )

    def loop_input_refusal(self, key: str, value: object) -> str | None:
        """Why this Loop cannot take *value*, of the declared type, for its input *key*.

        ``None`` (the default) takes it. A Loop whose input has a range
        overrides this (ReAct's ``max_steps`` must be at least 1); the reason
        completes the sentence "``<key>`` ...".
        """
        return None

    # ── the actions this Loop provides (shared by all loops) ──

    def get_action_space(
        self, *, active_actions: AbstractSet[str], **context: Any
    ) -> list[ActionDescriptor]:
        """The descriptors of this Loop's actions that negotiation activated, by key.

        The Loop's half of the run's action table: ``AgentRuntime.prepare``
        puts them after the environment's (``env.get_action_space``), so the
        prompt advertises ``loop.done`` like any other action. *context* is
        the same channel the environment's descriptors get (the run's active
        ``fragments``).
        """
        return [
            loop_action(key).descriptor(**context)
            for key in sorted(type(self).__loop_actions__ & active_actions)
        ]

    async def execute_action(self, action: Action) -> ActionResult:
        """Execute one of this Loop's actions: the guard's route for every ``loop.*`` name.

        The guard hands over only names this run activated, so a name this
        Loop does not provide comes only from a direct caller; it is refused,
        as a capability refuses an action it does not hold.
        """
        provided = type(self).__loop_actions__
        if action.name not in provided:
            return ActionResult(
                success=False,
                error=(
                    f"loop {self.loop_id!r} does not execute {action.name!r} "
                    f"(its actions: {sorted(provided)})"
                ),
            )
        return await loop_action(action.name).execute(action.params)

    # ── weave: bind deps + patch agent.run ───────────────────

    def weave(self, agent: Any, **deps: Any) -> Any:
        """Validate slots and loop inputs, capture deps in a closure, patch ``agent.run``.

        **Stateless on purpose**: *deps* live only in the ``run`` closure
        built here. Weaving the same loop instance onto a second agent
        cannot retroactively rewrite the first agent's env/llm/memory.

        ``deps["loop_inputs"]`` (absent means none) is checked against
        ``__loop_inputs__`` — the same check ``AgentRuntime.prepare()``
        runs before negotiation, repeated here because ``weave`` is public —
        and reaches ``_execute`` as a read-only ``ctx["loop_inputs"]``.

        Returns the same *agent* instance (instance-level patch).

        Raises:
            IncompatibleError: the agent lacks a required slot.
            IncompatibleLoopInputError: the loop inputs do not fit.
        """
        self.validate_slots(agent)
        loop_inputs = MappingProxyType(dict(deps.get("loop_inputs") or {}))
        self.check_loop_inputs(loop_inputs)

        ctx = dict(deps)
        ctx["loop_inputs"] = loop_inputs
        ctx.setdefault("step_executor", self)
        loop = self

        async def run(task: str, **kw: Any) -> Any:
            return await loop._execute(agent, task, ctx=ctx, **kw)

        agent.run = run  # instance-level monkey-patch
        return agent

    def execution_limits(self) -> ExecutionLimits:
        """Limits a consuming Agent must describe through its existing prompt inputs."""
        return ExecutionLimits()

    def step_budget(self, loop_inputs: Mapping[str, object]) -> int | None:
        """The most policy steps a run with these Loop inputs takes; ``None``: no budget.

        *loop_inputs* are checked (:meth:`check_loop_inputs`). Runtime hands
        the number to ``on_run_start`` for the trace (``TraceConfig.max_steps``)
        and the live view, never to the Agent: an Agent learns its budget from
        ``loop.step_info``, which a branch and the training export pin to the
        source run's (``docs/architecture_design.md`` §4.1 "Loop 输入").
        """
        return None

    async def surrender(
        self, recorder: StepSink | None, record: StepRecord, observation: Any
    ) -> None:
        """Hand execution facts to the step sink; recording failures are its responsibility.

        This public extension point is used for normal, failed and vetoed steps.
        The Loop neither inspects recorder health nor owns a persistence policy.
        """
        if recorder is not None:
            await recorder.record_step(record, observation=observation)

    async def surrender_vetoed(
        self, recorder: StepSink | None, record: StepRecord, observation: Any, veto: RunAbort
    ) -> None:
        """Hand over a vetoed step without losing its outcome to cancellation."""
        try:
            await self.surrender(recorder, record, observation)
        except asyncio.CancelledError as cancelled:
            logger.warning(
                "recording step %d was cut off by a cancellation; the run had already ended (the veto: %s)",
                record.step,
                veto,
            )
            carry_outcome(cancelled, veto)
            raise

    async def end_step(
        self, run: LoopRun, record: StepRecord, observation: Any, **outcome: Any
    ) -> AgentResult:
        """Create the decided result before handing over its final step."""
        result = self.build_result(run, **outcome)
        try:
            await self.surrender(run.recorder, record, observation)
        except asyncio.CancelledError as cancelled:
            carry_outcome(cancelled, result)
            raise
        return result

    def build_result(
        self,
        run: LoopRun,
        *,
        agent_done: str,
        env_status: str,
        answer: str = "",
        agent_reported_success: bool | None = None,
        error: str | None = None,
    ) -> AgentResult:
        """Build an execution result; Runtime and instrumentation supply measurements."""
        return AgentResult(
            task_id=run.identity.task_id,
            task=run.task,
            timestamp=datetime.now(UTC),
            env_status=EnvironmentStatus(env_status),
            agent_done=AgentDoneStatus(agent_done),
            agent_reported_success=agent_reported_success,
            answer=answer,
            error=error,
            model_id=run.identity.model_id,
            metrics=AgentMetrics(steps=run.policy_steps),
        )

    async def end_if_episode_over(self, run: LoopRun) -> AgentResult | None:
        """After a policy step that did not end the run: its result when the environment ends it.

        Some benchmarks end an episode themselves (AndroidWorld ends a MiniWoB++
        episode once the page reports a reward), so after every policy step that
        did not end the run, a Loop asks its environment (``env.episode_over``,
        ``docs/architecture_design.md`` §4.1). Over: an ``episode_over`` result,
        apart from the agent's ``done`` and from running out of steps, keeping
        the best answer seen as ``max_steps`` does; the oracles are handed no
        answer for it, which only ``loop.done`` gives. Not over: ``None``. A
        question that fails is an environment failure: an ``error`` result with
        ``env_status: failed`` naming it and the step it followed. A veto and a
        cancellation propagate. An env-less run has no episode to end.

        The caller applies its own terminal observation to an ``episode_over``
        result, as to any normal ending.
        """
        if run.env is None:
            return None
        try:
            over = await run.env.episode_over()
        except RunAbort:
            raise
        except Exception as exc:
            step = run.step_records[-1].step if run.step_records else None
            logger.warning(
                "Loop %r: asking whether the episode is over after step %s failed: %s: %s",
                self.loop_id,
                step,
                type(exc).__name__,
                exc,
            )
            return self.build_result(
                run,
                agent_done="error",
                env_status="failed",
                answer=run.last_answer,
                error=(
                    f"asking the environment whether the episode is over after step {step} "
                    f"failed: {type(exc).__name__}: {exc}"
                ),
            )
        if not over:
            return None
        return self.build_result(
            run, agent_done="episode_over", env_status="success", answer=run.last_answer
        )

    async def observe_after_result(self, run: LoopRun, result: AgentResult) -> AgentResult:
        """Observe after a decided outcome; keep it when observation is interrupted."""
        step = run.step_records[-1].step + 1 if run.step_records else 0
        try:
            record, observation = await run.step_executor.observe_terminal(
                run.env, run.negotiated_obs, step=step
            )
        except RunAbort as veto:
            record = StepRecord(
                step=step,
                origin="terminal",
                timestamp=time.time(),
                error_phase="observe",
                error=str(veto),
            )
            run.step_records.append(record)
            await self.surrender_vetoed(run.recorder, record, None, veto)
            raise
        except asyncio.CancelledError as cancelled:
            carry_outcome(cancelled, result)
            raise
        run.step_records.append(record)
        try:
            await self.surrender(run.recorder, record, observation)
        except asyncio.CancelledError as cancelled:
            carry_outcome(cancelled, result)
            raise
        return result

    # ── terminal observation (any loop may use it) ───────────

    async def observe_terminal(
        self,
        env: Environment,
        negotiated_obs: Any,
        *,
        step: int,
    ) -> tuple[StepRecord, Any]:
        """Look at the world once more after the run ended; build that step's record.

        For a Loop that records the state its run ended in (``ReactLoop``'s
        ``terminal_observation``): one ``env.observe`` — env-source
        fragments only, since no agent will read this observation — and an
        ``origin="terminal"`` record numbered *step*, with no model call and
        no action. It surrenders nothing: handing the record over, and what
        a failure to do so means for an outcome already decided, is the
        calling Loop's (``docs/architecture_design.md`` §4.1 "终止观测").

        Not fatal: an ordinary exception from ``observe`` becomes a record
        with ``error_phase="observe"`` and no observation, and the run's
        outcome stands. A ``RunAbort`` propagates — a guard's veto outranks
        the outcome — for the caller to record and re-raise.

        Returns:
            The terminal record and the observation (``None`` when the
            observe failed).
        """
        try:
            observation = await env.observe(negotiated_obs)
        except RunAbort:
            raise
        except Exception as exc:
            logger.warning(
                "Loop %r: the terminal observation (step %d) failed: %s: %s",
                self.loop_id,
                step,
                type(exc).__name__,
                exc,
            )
            record = StepRecord(
                step=step,
                origin="terminal",
                timestamp=time.time(),
                error_phase="observe",
                error=str(exc),
            )
            return record, None
        record = StepRecord(
            step=step,
            origin="terminal",
            observation_summary=summarize_observation(observation),
            timestamp=time.time(),
        )
        return record, observation

    # ── subclass hook ────────────────────────────────────────

    async def _execute(
        self,
        agent: Any,
        task: str,
        *,
        ctx: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        """Drive the full agent loop. Subclasses MUST override.

        Args:
            agent: Agent instance satisfying this loop's required slots.
            task: The task description string.
            ctx: Runtime deps captured at weave time (env, llm, memory,
                act_fn, negotiated_obs, …). Passed per call, never stored.
            **kwargs: Per-run overrides from ``agent.run(task, ...)``.
        """
        raise NotImplementedError

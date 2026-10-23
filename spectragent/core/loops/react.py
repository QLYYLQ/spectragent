"""ReactLoop — standard ReAct (Reason + Act) execution strategy.

Drives the observe -> build_prompt -> LLM -> parse_output -> act cycle.
Extracted from ``BaseReactAgent.run()`` as an independent Loop that
calls Agent Protocol methods for domain knowledge.

**AOP-transparent design**: ReactLoop has NO emitter/hook parameter.
AOP hooks are woven into agent/env/llm by the Weaver (M04) at
runtime. The Loop is completely unaware of hooks.

See docs/architecture_design.md 4.1 and docs/execution_dag.md M09.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

from pydantic import Field

from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.action_guard import params_refusal
from spectragent.core.llm.exceptions import (
    ModelOutputTruncatedError,
    StructuredOutputValidationError,
)
from spectragent.core.llm.messages import AssistantMessage, UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops._loop_fragments import (
    loop_fragment_builders,
    populate_loop_fragments,
)
from spectragent.core.loops._registrar import AgentLoopBase
from spectragent.core.loops.errors import ParseError, RunAbort
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.run import LoopRun
from spectragent.core.loops.validation import IncompatibleError
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.observation.dynamic_model import summarize_observation
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.restoration import RestorationCheck
from spectragent.schemas.step_record import StepRecord

if TYPE_CHECKING:
    from spectragent.agents.memory.base import BaseMemoryManager

logger = logging.getLogger(__name__)


def serialize_llm_output(raw_output: Any) -> str:
    """Render one LLM completion as the text stored in memory + StepRecord.

    ``llm.ainvoke`` returns ``ChatInvokeCompletion[str]`` when the agent
    advertises no output model and ``ChatInvokeCompletion[T]`` when it
    does, so *both* the success path and the ParseError path have to
    handle a Pydantic model. They must agree: this text is replayed to
    the LLM as its own previous assistant turn, so a structured
    completion has to come back as the JSON it emitted — never a Python
    ``repr``, which the model cannot imitate and would learn from. They
    now agree by construction: there is one call site, above the
    success/failure fork, and every path reads its ``output_text``.
    """
    if isinstance(raw_output, str):
        return raw_output
    model_dump_json = getattr(raw_output, "model_dump_json", None)
    if callable(model_dump_json):
        return str(model_dump_json())
    return str(raw_output)


def parse_feedback(exc: ParseError) -> str:
    """The user-role turn that tells the model what it got wrong.

    Identical text whether the re-ask happens inside this step (see
    ``parse_retries_per_step``) or on the next one — the model must not be
    able to tell the two apart, or an in-step retry would be training on a
    prompt shape the cross-step path never produces.
    """
    return (
        f"[SYSTEM] Your previous output could not be parsed into actions. "
        f"Error: {exc.detail}\n"
        f"Please try again with valid output format."
    )


@dataclass(kw_only=True)
class ReactRun(LoopRun):
    """ReAct retry state belonging to one execution."""

    parse_error_streak: int = 0


@dataclass
class ReactLoop(AgentLoopBase):
    """Standard ReAct loop: observe -> think -> act -> repeat.

    Requires agents to have ``build_react_prompt`` and ``parse_react_output``
    callable slots. Optionally uses ``get_react_output_model`` and ``prepare``.
    ``format_react_output`` (the inverse of ``parse_react_output``) and
    ``parse_react_thought`` are ReAct slots too, but the training export calls
    them, not this Loop, and checks them before it does (arch §4.1 "可选 slot").

    Configuration fields (conscribe config-annotated):
    - ``max_steps``: maximum number of steps before termination
    - ``max_actions_per_step``: maximum actions per LLM response
    - ``max_consecutive_parse_errors``: consecutive unparseable *steps*
      before termination
    - ``parse_retries_per_step``: extra re-asks inside one step, on the
      observation that step already took
    - ``today_override``: pins ``StepInfoObs.today`` for replay
    - ``terminal_observation``: observe once more after the run ended and
      record it as an ``origin="terminal"`` step
    """

    __registry_key__ = "react"
    __required_slots__ = {"build_react_prompt", "parse_react_output"}
    __optional_slots__ = {
        "get_react_output_model",
        "prepare",
        "format_react_output",
        "format_action_fact",
        "parse_react_thought",
    }
    __required_message_protocols__: ClassVar[frozenset[MessageProtocol]] = frozenset(
        {"user_assistant"}
    )
    __requires_env__ = True

    # ── conscribe config-annotated fields ─────────────────────
    max_steps: Annotated[int, Field(default=100, description="Maximum number of steps.")] = 100
    max_actions_per_step: Annotated[
        int, Field(default=1, description="Maximum actions per LLM response.")
    ] = 1
    max_consecutive_parse_errors: Annotated[
        int,
        Field(
            default=3,
            description="Consecutive ParseError failures before termination.",
        ),
    ] = 3
    parse_retries_per_step: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Extra LLM re-asks inside one step after a ParseError, "
                "before the step is spent. The re-ask reuses the "
                "observation this step already took — no new observe "
                "cycle — so a format mistake costs a round trip instead "
                "of a step. 0 keeps the cross-step behaviour only."
            ),
        ),
    ] = 0
    today_override: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "ISO date string overriding 'today' in StepInfoObs. "
                "Use for WAL replay reproducibility and deterministic tests; "
                "leave None in production to use the real clock."
            ),
        ),
    ] = None
    terminal_observation: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "After the run ends (done, max_steps, or the last tolerated "
                "parse error), observe the environment once more and record "
                "it as an origin='terminal' step: no LLM call, no action, "
                "env-source fragments only. Never on an infrastructure "
                "failure, a failed action or a veto. The run's result is "
                "decided before it and does not change (Runtime execution_ms "
                "includes it)."
            ),
        ),
    ] = False

    def validate_slots(self, agent: Any) -> None:
        """Validate the asynchronous prompt boundary before an execution can start."""
        super().validate_slots(agent)
        builder = agent.build_react_prompt
        context = inspect.signature(builder).parameters.get("context")
        if (
            not inspect.iscoroutinefunction(builder)
            or context is None
            or (context.kind is not inspect.Parameter.KEYWORD_ONLY)
        ):
            raise IncompatibleError(
                agent_name=getattr(agent, "agent_name", type(agent).__name__),
                loop_id=self.loop_id,
                missing_slots=["async build_react_prompt(observation, *, context)"],
            )

    def prompt_context(self, run: ReactRun, step_idx: int) -> ReactPromptContext:
        """Describe this policy turn; specialized ReAct loops may add source facts."""
        return ReactPromptContext(
            step=step_idx,
            phase="policy",
            policy_step=run.policy_steps,
            is_branch=run.is_branch,
        )

    def execution_limits(self) -> ExecutionLimits:
        """The action cap the Agent must communicate in its own prompt."""
        return ExecutionLimits(max_actions_per_step=self.max_actions_per_step)

    # -- AgentLoopBase._execute() implementation ----------------

    async def _execute(
        self,
        agent: Any,
        task: str,
        *,
        ctx: dict[str, Any],
        **kwargs: Any,
    ) -> AgentResult:
        """Drive the full ReAct loop.

        Runtime deps arrive in *ctx* (captured by ``weave`` into the
        ``agent.run`` closure): env, llm, memory, act_fn,
        negotiated_obs, active_descriptors, task_id. The run's state lives
        in one :class:`~spectragent.core.loops.run.LoopRun` per call, never
        on the Loop.

        Returns:
            AgentResult with execution trace and metrics.
        """
        run = ReactRun.from_ctx(agent, task, ctx, step_executor=ctx.get("step_executor", self))
        for step_idx in range(self.max_steps):
            result = await run.step_executor.execute_step(run, step_idx)
            if result is not None:
                return result

        # Max steps reached. The best answer seen so far is carried onto
        # *every* termination path — an agent that produced the deliverable
        # and then ran out of steps must not have it thrown away.
        return await self.with_terminal(
            run,
            self.build_result(
                run,
                agent_done="max_steps",
                env_status="success",
                answer=run.last_answer,
            ),
        )

    async def execute_step(
        self,
        run: ReactRun,
        step_idx: int,
        *,
        observation: Any = None,
        restoration: RestorationCheck | None = None,
    ) -> AgentResult | None:
        """One ReAct step: observe, prompt, think (with in-step re-asks), parse, act, record.

        Returns the run's terminal ``AgentResult`` when the run ends on this
        step, ``None`` when it goes on. A ``RunAbort`` propagates, after the
        step it interrupted has been recorded.

        Args:
            run: The run's dependencies and books.
            step_idx: The step's number in the trace.
            observation: The env-source observation to decide on, already
                taken. ``env.observe`` is then not called — a branch run's
                first policy step decides on the observation its restorer
                verified, and observing again would open a new cycle on a
                world nobody verified. Loop-source fragments are populated
                either way.
            restoration: The check that observation passed (a branch run's
                checkpoint step); set on every record this step builds.
        """
        # 1. Observe — env fills env-source fragments; loop fills
        #    loop-source fragments (task / step_info / history /
        #    action_result) immediately after.
        try:
            if observation is None:
                observation = await run.env.observe(run.negotiated_obs)
            loop_sourced = getattr(run.negotiated_obs, "loop_sourced", frozenset())
            if loop_sourced:
                observation = populate_loop_fragments(
                    observation,
                    loop_sourced=loop_sourced,
                    builders=self.loop_fragment_builders(run, step_idx),
                )
        except RunAbort as exc:
            await self._note_abort(
                run,
                phase="observe",
                exc=exc,
                observation=None,
                step_idx=step_idx,
                restoration=restoration,
            )
            raise
        except Exception as exc:
            return await self._abort(
                run,
                phase="observe",
                exc=exc,
                observation=None,
                step_idx=step_idx,
                restoration=restoration,
            )

        observation_summary = summarize_observation(observation)

        # 2. Agent builds new step messages (pure builder — returns
        #    only what the agent wants appended this step). Loop owns
        #    the memory write and assembles the full LLM context.
        #
        # Builder exceptions remain errors for the caller, while the step
        # preserves their phase, observation and generated contribution facts.
        agent = run.agent
        memory = run.memory
        prompt_context: ReactPromptContext | None = None
        try:
            prompt_context = self.prompt_context(run, step_idx)
            new_msgs = await agent.build_react_prompt(observation, context=prompt_context)
        except RunAbort as exc:
            await self._note_abort(
                run,
                phase="prompt",
                prompt_context=prompt_context,
                exc=exc,
                observation=observation,
                observation_summary=observation_summary,
                step_idx=step_idx,
                restoration=restoration,
            )
            raise
        except Exception as exc:
            record = StepRecord(
                step=step_idx,
                observation_summary=observation_summary,
                error_phase="prompt",
                error=str(exc),
                timestamp=time.time(),
                prompt_contributions=list(prompt_context.contributions) if prompt_context else [],
                restoration=restoration,
            )
            run.step_records.append(record)
            await self.end_step(
                run,
                record,
                observation,
                agent_done="error",
                env_status="success",
                error=f"prompt failed at step {step_idx}: {type(exc).__name__}: {exc}",
                answer=run.last_answer,
            )
            raise
        memory.add_messages(new_msgs)
        context = memory.get_context_messages()

        # 3-5. Think: LLM call + parse, re-asked in place.
        #
        #    ``output_format=None`` yields a
        #    ``ChatInvokeCompletion[str]``; a non-None model yields a
        #    ``ChatInvokeCompletion[T]`` already validated by the
        #    provider.
        #
        #    A ParseError inside this while-loop is answered by
        #    re-asking the *same* observation up to
        #    ``parse_retries_per_step`` times. ``env.observe`` is not
        #    called again and ``build_react_prompt`` is not rebuilt:
        #    one step of the ReAct loop is one look at the world, and
        #    a badly formatted reply is not a reason to look again.
        #    The cycle boundary is announced by ``Environment.observe``
        #    (``BaseCapability.begin_observation``), so an extra
        #    ``env.observe`` here would open a new cycle, re-capture,
        #    and hand the model a fresh screenshot mid-question — a
        #    different world from the one its unparseable reply was
        #    about.
        output_model = (
            agent.get_react_output_model() if hasattr(agent, "get_react_output_model") else None
        )
        retries_left = self.parse_retries_per_step
        parse_failure: ParseError | None = None
        actions: list[Action] = []
        output_text = ""

        while True:
            try:
                completion = await run.llm.ainvoke(context, output_format=output_model)
            except StructuredOutputValidationError as exc:
                # The call succeeded; the *reply* did not fit the
                # schema. That is the one provider error the model can
                # fix, so it rejoins the ordinary path with the text
                # the model wrote and is refused a second time by the
                # agent's own parser — which knows this run's verbs and
                # answers with one located line, where the provider can
                # only report a schema mismatch. Aborting here instead
                # ended an episode over a single mistyped parameter.
                #
                # The call stays visible to the hooks even though it
                # raised: ``around_ainvoke`` on the recorder sees what ``after`` cannot, so the
                # exchange and provider usage reach the unified recorder.
                completion = ChatInvokeCompletion(
                    completion=exc.raw_output,
                    usage=exc.usage,
                    stop_reason="structured_output_validation_error",
                )
            except RunAbort as exc:
                await self._note_abort(
                    run,
                    phase="llm",
                    prompt_context=prompt_context,
                    exc=exc,
                    observation=observation,
                    observation_summary=observation_summary,
                    step_idx=step_idx,
                    restoration=restoration,
                )
                raise
            except ModelOutputTruncatedError as exc:
                # Our ceiling, not the model's mistake — so this must
                # not get what a ParseError gets. Re-asking cannot help
                # (the budget is unchanged and the history is now
                # longer, so the retry truncates sooner) and each
                # attempt costs a maximum-length completion. Ending here
                # also keeps the trace honest: routed through the parse
                # path, a run died reporting "Parse failed 3 consecutive
                # times", which on an evaluation platform charges our
                # configuration to the model under test.
                #
                return await self._abort(
                    run,
                    phase="llm",
                    prompt_context=prompt_context,
                    exc=exc,
                    observation=observation,
                    observation_summary=observation_summary,
                    step_idx=step_idx,
                    env_status="success",
                    restoration=restoration,
                )
            except Exception as exc:
                # The environment is fine; the model call is not. A 429
                # should not be indistinguishable from a dead browser.
                return await self._abort(
                    run,
                    phase="llm",
                    prompt_context=prompt_context,
                    exc=exc,
                    observation=observation,
                    observation_summary=observation_summary,
                    step_idx=step_idx,
                    env_status="success",
                    restoration=restoration,
                )

            raw_output = completion.completion
            output_text = serialize_llm_output(raw_output)

            # Agent parses LLM output (domain knowledge). ParseError is
            # a framework-level control signal — it does NOT go through
            # action dispatch. The error detail is fed back so the LLM
            # can self-correct, in this step if retries remain and on
            # the next one otherwise.
            try:
                actions = agent.parse_react_output(raw_output)
                # The actions that would run must fit their ``Params``; one
                # that does not is a reply to rewrite, not an action to fail.
                self.check_action_params(
                    run, actions[: self.max_actions_per_step], raw_output=output_text
                )
            except RunAbort as exc:
                # First, so the veto cannot be read as a parse failure
                # even if a guard ever subclasses both. A parser is an
                # agent method and therefore woven: an agent that
                # inspects the model's own reply and refuses to go on
                # vetoes from exactly here, and the reply it refused is
                # the evidence — hence ``llm_output``/``token_usage``
                # on the record, which no other abort path has.
                await self._note_abort(
                    run,
                    phase="parse",
                    prompt_context=prompt_context,
                    exc=exc,
                    observation=observation,
                    observation_summary=observation_summary,
                    step_idx=step_idx,
                    llm_output=output_text,
                    restoration=restoration,
                )
                raise
            except ParseError as exc:
                parse_failure = exc
                # The failed attempt and the reason it failed both land
                # in memory. The in-step retry and the next step read
                # exactly the same two messages — the only difference
                # between them is whether a new observation follows.
                self.remember_rejected(memory, output_text, exc)
                if retries_left <= 0:
                    break
                retries_left -= 1
                logger.info(
                    "ReactLoop: step %d re-asking after parse error (%d retr%s left): %s",
                    step_idx,
                    retries_left,
                    "y" if retries_left == 1 else "ies",
                    exc.detail,
                )
                context = memory.get_context_messages()
                continue

            parse_failure = None
            break

        if parse_failure is not None:
            # Every in-step retry is spent. Only now does the failure
            # cost a step — and only now does it count towards the
            # cross-step streak, which stays "consecutive *steps* that
            # could not be parsed", not consecutive LLM calls.
            run.parse_error_streak += 1
            logger.warning(
                "ReactLoop: parse error (%d/%d): %s",
                run.parse_error_streak,
                self.max_consecutive_parse_errors,
                parse_failure.detail,
            )

            # Keep an exhausted parse distinct from a successful empty action list.
            record = StepRecord(
                step=step_idx,
                observation_summary=observation_summary,
                llm_output=output_text,
                actions=[],
                action_results=[],
                error_phase="parse",
                error=str(parse_failure),
                prompt_contributions=list(prompt_context.contributions) if prompt_context else [],
                timestamp=time.time(),
                restoration=restoration,
            )
            run.step_records.append(record)
            run.prev_step_record = record

            if run.parse_error_streak < self.max_consecutive_parse_errors:
                await self.surrender(run.recorder, record, observation)
                return None
            # The run ends on this step, in failure: recording it must
            # not be what decides how the run ended (``end_step``).
            ending = await self.end_step(
                run,
                record,
                observation,
                error=(
                    f"Parse failed {run.parse_error_streak} "
                    f"consecutive times: {parse_failure.detail}"
                ),
                agent_done="error",
                env_status="success",
                answer=run.last_answer,
            )
            return await self.with_terminal(run, ending)

        # Successful parse resets the streak.
        run.parse_error_streak = 0

        # 6. Enforce max_actions_per_step. Silent truncation makes the
        #    model re-issue steps it believes already ran, so the drop
        #    is logged, marked on the record, and fed back to the LLM.
        actions, dropped = self.cap_actions(actions, step_idx)

        # 7. Store assistant response in memory. ``output_text`` is the
        #    reply that *parsed* — on a step that took retries, the
        #    rejected attempts are already in memory, each followed by
        #    the error that rejected it.
        self.remember_reply(memory, output_text, dropped)

        # 8. Execute actions via guarded_act
        step_results: list[ActionResult] = []
        last_done_result: ActionResult | None = None
        act_error: Exception | None = None
        for action in actions:
            try:
                result = await run.act_fn(action)
            except Exception as exc:  # noqa: PERF203 — per-action attribution
                act_error = exc
                break
            step_results.append(result)
            if result.extracted_content:
                run.last_answer = result.extracted_content
            if result.is_done:
                last_done_result = result
                break

        # Build step record
        record = StepRecord(
            step=step_idx,
            observation_summary=observation_summary,
            llm_output=output_text,
            actions=actions,
            action_results=step_results,
            timestamp=time.time(),
            truncated_actions=len(dropped),
            prompt_contributions=list(prompt_context.contributions) if prompt_context else [],
            error_phase="act" if act_error is not None else "",
            error=str(act_error) if act_error is not None else None,
            restoration=restoration,
        )
        run.step_records.append(record)
        run.prev_step_record = record
        if act_error is None:
            if last_done_result is not None:
                ending = await self.end_step(
                    run,
                    record,
                    observation,
                    agent_done="done",
                    env_status="success",
                    answer=last_done_result.extracted_content or "",
                    agent_reported_success=last_done_result.reported_success,
                )
                return await self.with_terminal(run, ending)
            await self.surrender(run.recorder, record, observation)
        elif isinstance(act_error, RunAbort):
            # Not an action failure — a veto. Something woven onto
            # ``act`` (a safety guard watching for a risk-control
            # response, say) has decided the run must not continue, and
            # turning that into an ordinary ``agent_done="error"``
            # result is exactly how the veto used to get lost: the
            # caller could not tell it from a click that happened to
            # raise, and a benchmark driver went on to the next task.
            #
            # Logged first, so the veto is on record whatever becomes of
            # the hand-over below. Then the step is surrendered — the
            # evidence the guard fired on is the most valuable thing in
            # the trace — and only then does the exception leave the
            # Loop, so AgentRuntime closes the run out (``on_run_end`` +
            # ``on_run_error``) and re-raises to whoever started it.
            # ``surrender_vetoed`` keeps the veto the outcome even if
            # that hand-over fails or is cut off by the run's timeout.
            #
            # This is the second of the two abort shapes: the record is
            # already written, with the actions and results ``_note_abort``
            # has no way to know about, so this phase does not call it.
            # See :meth:`_note_abort` for when to use which.
            logger.warning(
                "ReactLoop: run aborted during act at step %d: %s",
                step_idx,
                act_error,
            )
            await self.surrender_vetoed(run.recorder, record, observation, act_error)
            raise act_error
        else:
            # ``exc_info=act_error`` explicitly: the ``except`` handler
            # that captured it exited several statements ago, so
            # ``logger.exception`` here would read an already-popped
            # ``sys.exc_info()`` and print ``NoneType: None`` — no
            # stack at all for the one branch whose whole purpose is
            # per-action attribution.
            logger.error(
                "ReactLoop: action execution failed at step %d: %s: %s",
                step_idx,
                type(act_error).__name__,
                act_error,
                exc_info=act_error,
            )
            # The run ends on this step, in failure (``end_step``).
            return await self.end_step(
                run,
                record,
                observation,
                error=f"Action execution failed at step {step_idx}: {act_error}",
                agent_done="error",
                env_status="failed",
                answer=run.last_answer,
            )

        return None

    # -- ReAct's memory protocol ---------------------------------
    #
    # The live step and a branch run's replay of a recorded step write
    # memory through these and nothing else, so the two can never disagree
    # about what a rejected reply, an accepted one or a truncation looks
    # like to the model.

    def check_action_params(
        self, run: LoopRun, actions: Sequence[Action], *, raw_output: str = ""
    ) -> None:
        """Refuse a reply whose actions do not fit their ``Params``, before any of them runs.

        Each action is validated against the ``Params`` of the action the
        run's guard executes it as (``core/action_guard.py::params_refusal``).
        A misfit is a :class:`ParseError` naming every action that misfits and
        why, so the reply goes back to the model through the parse retries
        (``parse_retries_per_step``) and, when they are spent, is recorded as
        the step's parse failure — never executed, never a failed
        ``ActionResult`` (the user's ruling of 2026-10-23). A name the guard
        refuses is not judged here: the guard refuses it when it runs.

        Raises:
            ParseError: An action's params do not fit.
        """
        problems = [
            f"action {position}, {refusal}"
            for position, action in enumerate(actions, start=1)
            if (refusal := params_refusal(run.action_space, action)) is not None
        ]
        if problems:
            raise ParseError(
                "the parameters of an action do not fit what it takes: " + "; ".join(problems),
                raw_output=raw_output[:500],
            )

    def cap_actions(self, actions: list[Action], step_idx: int) -> tuple[list[Action], list[str]]:
        """Keep the first ``max_actions_per_step`` actions; name the ones dropped.

        Returns:
            The actions that will run, and the names of the ones that will
            not (empty when nothing was cut).
        """
        if len(actions) <= self.max_actions_per_step:
            return actions, []
        dropped = [a.name for a in actions[self.max_actions_per_step :]]
        logger.warning(
            "ReactLoop: step %d emitted %d actions, max_actions_per_step=%d — discarded %s",
            step_idx,
            len(actions),
            self.max_actions_per_step,
            dropped,
        )
        return actions[: self.max_actions_per_step], dropped

    def remember_rejected(
        self, memory: BaseMemoryManager, output_text: str, exc: ParseError
    ) -> None:
        """A reply the parser refused: the reply as the assistant turn, then why it was refused."""
        memory.add_message(AssistantMessage(content=output_text))
        memory.add_message(UserMessage(content=parse_feedback(exc)))

    def remember_reply(
        self, memory: BaseMemoryManager, output_text: str, dropped: list[str]
    ) -> None:
        """A reply that parsed: the assistant turn, then a notice of any actions cut from it."""
        memory.add_message(AssistantMessage(content=output_text))
        if dropped:
            memory.add_message(
                UserMessage(
                    content=(
                        f"[SYSTEM] Only the first {self.max_actions_per_step} "
                        f"action(s) of your response were executed. "
                        f"{len(dropped)} action(s) were discarded and did NOT "
                        f"run: {', '.join(dropped)}. Re-issue them if still "
                        f"needed."
                    )
                )
            )

    # -- the terminal observation --------------------------------

    async def with_terminal(self, run: ReactRun, result: AgentResult) -> AgentResult:
        """Apply ReAct's terminal-observation policy to an already decided result."""
        if not self.terminal_observation:
            return result
        return await self.observe_after_result(run, result)

    async def _note_abort(
        self,
        run: ReactRun,
        *,
        phase: str,
        exc: RunAbort,
        observation: Any,
        step_idx: int,
        observation_summary: str = "",
        llm_output: str = "",
        restoration: RestorationCheck | None = None,
        prompt_context: ReactPromptContext | None = None,
    ) -> None:
        """Record the step a ``RunAbort`` interrupted, then let it fly.

        The counterpart of :meth:`_abort`, and deliberately *not* it.
        ``_abort`` converts a failure into a terminal ``AgentResult``;
        this converts nothing. It writes the partial step to the sink so
        the trace still shows what the run was doing when it was vetoed,
        and returns — the caller re-raises. Recording errors are isolated
        by the recording component. A cancellation cutting off the hand-over carries
        the veto with it (:meth:`surrender_vetoed`): the veto is what the
        caller has to see.

        **This is one of two abort shapes in ``execute_step``, not the only
        one.** Used by the phases that handle their exception where it is
        raised: observe, prompt, llm, parse. The act phase does *not* come
        through here — its per-action ``except`` captures the exception so
        the failure can be attributed to one action among several, builds
        the ordinary (and richer) ``StepRecord``, hands it over through the
        same :meth:`surrender_vetoed`, and only then re-raises on
        ``isinstance(act_error, RunAbort)``. Calling ``_note_abort`` there
        would write the step twice.

        Nothing enforces either shape. ``tests/test_core/test_loops/
        test_abort_handler_hygiene.py`` walks this package's AST and fails
        when a ``try`` gains a blanket ``except Exception`` without one of
        the two — that test, not this docstring, is the guard rail.
        """
        logger.warning(
            "ReactLoop: run aborted during %s at step %d: %s",
            phase,
            step_idx,
            exc,
        )
        record = StepRecord(
            step=step_idx,
            observation_summary=observation_summary,
            llm_output=llm_output,
            timestamp=time.time(),
            error_phase=phase,
            error=str(exc),
            prompt_contributions=list(prompt_context.contributions) if prompt_context else [],
            restoration=restoration,
        )
        run.step_records.append(record)
        await self.surrender_vetoed(run.recorder, record, observation, exc)

    async def _abort(
        self,
        run: ReactRun,
        *,
        phase: str,
        exc: Exception,
        observation: Any,
        step_idx: int,
        observation_summary: str = "",
        env_status: str = "failed",
        restoration: RestorationCheck | None = None,
        prompt_context: ReactPromptContext | None = None,
    ) -> AgentResult:
        """Terminate on an infrastructure failure, with the step recorded.

        A transient CDP disconnect or a single 429 used to take the whole
        run down as an unhandled exception. Now it produces a normal
        terminal result whose ``env_status`` says whether the environment
        was the thing that broke.

        Provider usage and recording health belong to the recording component.
        """
        logger.exception("ReactLoop: %s failed at step %d", phase, step_idx)
        record = StepRecord(
            step=step_idx,
            observation_summary=observation_summary,
            timestamp=time.time(),
            error_phase=phase,
            error=str(exc),
            prompt_contributions=list(prompt_context.contributions) if prompt_context else [],
            restoration=restoration,
        )
        run.step_records.append(record)
        return await self.end_step(
            run,
            record,
            observation,
            error=f"{phase} failed at step {step_idx}: {type(exc).__name__}: {exc}",
            agent_done="error",
            env_status=env_status,
            answer=run.last_answer,
        )

    def today_iso(self) -> str:
        """ISO date string for ``StepInfoObs.today``.

        Honours the ``today_override`` config field so WAL replay and
        deterministic tests reproduce identical observations.
        """
        return self.today_override or date.today().isoformat()

    def loop_fragment_builders(self, run: ReactRun, step_idx: int) -> dict[str, Any]:
        """Builders for every loop-source fragment, from this run's values.

        The construction itself is ``_loop_fragments.loop_fragment_builders``
        (shared with ``BranchLoop`` and the training export); this Loop hands
        it ``max_steps`` and today's date (``today_override`` first).
        """
        return loop_fragment_builders(
            task=run.task,
            step=step_idx,
            max_steps=self.max_steps,
            today=self.today_iso(),
            history=run.step_records,
            prev_step=run.prev_step_record,
        )

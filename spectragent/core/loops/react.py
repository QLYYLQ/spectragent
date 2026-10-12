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

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.agents.memory.protocols import MessageProtocol
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
from spectragent.core.loops.errors import ParseError, RunAbort, carry_outcome
from spectragent.core.loops.run import LoopRun
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult, AgentUsage
from spectragent.schemas.observation.dynamic_model import summarize_observation
from spectragent.schemas.restoration import RestorationCheck
from spectragent.schemas.step_record import StepRecord, TokenUsage

logger = logging.getLogger(__name__)


def _model_name(llm: Any) -> str:
    """Best-effort model identifier for result attribution.

    Recorded on every ``AgentResult`` so cross-model comparison is
    possible from the artifacts alone — ``model_id`` used to be ``""`` in
    every trace on disk.
    """
    for attr in ("model", "model_name", "name"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _serialize_llm_output(raw_output: Any) -> str:
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


def _parse_feedback(exc: ParseError) -> str:
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


# TODO 这里的class为什么是react specific的？按理来说这些内容每个agent loop都有，而且差不多的
@dataclass
class _UsageTally:
    """The run's token totals, holding "not measured" apart from "measured zero".

    ``AgentMetrics.usage is None`` is the schema's sentinel for a run nobody
    instrumented; a zero-filled ``AgentUsage`` is the different claim that the
    run genuinely cost nothing. Keeping the flag beside the totals is what
    makes that distinction unlosable — it used to live in a separate local,
    spelled out again at each of the seven ``_build_result`` / ``_abort`` call
    sites, where one of them written as a bare ``total_usage`` would have made
    an uninstrumented run report confident zeros. Exactly the failure the
    ``AgentUsage`` docstring warns about, one spelling mistake away.

    It is deliberately not on ``AgentLoopBase``. Accumulation would generalise
    (it never needs to know when a run ends — a caller asks for
    :meth:`snapshot` when it is building a result), but there is one Loop, and
    the rule is to generalise at the second instance. This is the shape to
    lift when that instance exists.
    """

    _total: AgentUsage = field(default_factory=AgentUsage)
    _measured: bool = False

    def add(self, usage: Any, *, model: str) -> None:
        """Fold in one LLM call, measured or not.

        A ``None`` usage still counts the call: ``add_llm_usage`` leaves
        ``entry_count`` behind as evidence it happened, which is how a
        provider that reports nothing stays visible instead of vanishing.
        Only the totals go unrecorded, which is what ``_measured`` says.
        """
        if usage is not None:
            self._measured = True
        self._total.add_llm_usage(usage, model=model)

    def snapshot(self) -> AgentUsage | None:
        """What to report — or ``None`` if nothing measurable ever arrived."""
        return self._total if self._measured else None


def _unrecorded_suffix(unrecorded: Exception | None) -> str:
    """What a failure result's ``error`` adds when its step could not be recorded.

    The trace is then missing exactly the step the error names, and
    ``result.json`` is where a reader of that trace finds out why.
    """
    if unrecorded is None:
        return ""
    return f" (recording this step failed too: {type(unrecorded).__name__}: {unrecorded})"


def _note_unrecorded(abort: BaseException, unrecorded: Exception | None) -> None:
    """Attach a recording failure to the veto it must not replace.

    A note, not a new message: the veto's text is the guard's, and what
    ``AgentRuntime`` puts in the result is ``str()`` of it, unchanged. The
    note travels with the exception into every traceback that prints it.
    """
    if unrecorded is not None:
        abort.add_note(
            f"Recording the vetoed step failed too: {type(unrecorded).__name__}: {unrecorded}"
        )


def _log_cut_off(record: StepRecord, outcome: str) -> None:
    """Say that a cancellation cut off the recording of the step a run ended on.

    The cancellation propagates and carries the outcome; this line is what
    the log keeps of it whoever turns out to have cancelled.
    """
    logger.warning(
        "ReactLoop: recording step %d was cut off by a cancellation; the run had already "
        "ended (%s), and the cancellation carries that outcome",
        record.step,
        outcome,
    )


def _merge_token_usage(base: TokenUsage | None, addition: TokenUsage | None) -> TokenUsage | None:
    """Accumulate the LLM calls of one step into a single ``TokenUsage``.

    A step used to be exactly one call, so ``StepRecord.token_usage`` was
    that call's numbers. ``parse_retries_per_step`` can make a step cost
    several, and the record must say what the *step* cost — otherwise the
    retries are free in every per-step cost report while showing up in
    ``AgentResult.metrics.usage``, and the two stop adding up.

    ``None`` is the schema's "not measured" sentinel and is preserved:
    merging nothing into nothing stays nothing, and a single measured call
    is returned as-is rather than copied.

    Only the three declared counters are summed. ``TokenUsage`` is
    ``extra="allow"`` and ``model_copy`` carries whatever *base* happens to
    hold, but on this Loop's path nothing is there to carry: every operand
    comes from ``TokenUsage.from_llm_usage``
    (``schemas/step_record.py:19``), which builds a fresh model out of
    exactly those three fields, so a provider's extras are already gone
    before this helper sees anything. Preserving them is a property of the
    helper, not a guarantee the pipeline delivers — do not describe it as
    one.
    """
    if addition is None:
        return base
    if base is None:
        return addition
    return base.model_copy(
        update={
            "prompt_tokens": base.prompt_tokens + addition.prompt_tokens,
            "completion_tokens": base.completion_tokens + addition.completion_tokens,
            "total_tokens": base.total_tokens + addition.total_tokens,
        }
    )


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
                "decided before it and does not change (its end_to_end_ms "
                "excludes it)."
            ),
        ),
    ] = False

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
        # Reported as ``usage=None`` unless some provider actually reported
        # numbers: an all-zero AgentUsage masquerades as "measured, cost
        # nothing" and destroys the schema's own unmeasured sentinel.
        run = LoopRun.from_ctx(
            agent, task, ctx, model_id=_model_name(ctx["llm"]), usage=_UsageTally()
        )
        for step_idx in range(self.max_steps):
            result = await self._run_step(run, step_idx)
            if result is not None:
                return result

        # Max steps reached. The best answer seen so far is carried onto
        # *every* termination path — an agent that produced the deliverable
        # and then ran out of steps must not have it thrown away.
        return await self._with_terminal(
            run,
            self._build_result(
                run,
                agent_done="max_steps",
                env_status="success",
                answer=run.last_answer,
            ),
        )

    async def _run_step(
        self,
        run: LoopRun,
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
                    builders=self._loop_fragment_builders(run, step_idx),
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
        #    Only ``RunAbort`` is caught here. An ordinary exception out
        #    of the agent's own builder is a bug in the agent, not an
        #    infrastructure failure, and it propagates exactly as it
        #    always has — there is deliberately no blanket handler. But
        #    ``build_react_prompt`` is an agent method, so it is woven
        #    by the AOP proxy like any other (``runtime.py`` wraps the
        #    agent with ``role="agent"``), and a guard can veto from
        #    here. When it does, the observation it fired on is the
        #    evidence, so the step is recorded before the veto flies.
        agent = run.agent
        memory = run.memory
        try:
            new_msgs = agent.build_react_prompt(observation)
        except RunAbort as exc:
            await self._note_abort(
                run,
                phase="prompt",
                exc=exc,
                observation=observation,
                observation_summary=observation_summary,
                step_idx=step_idx,
                restoration=restoration,
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
        # Accumulated over every attempt this step makes, so the
        # StepRecord reports what the *step* cost, not its last call.
        step_token_usage: TokenUsage | None = None
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
                # raised: ``around_ainvoke`` on TraceWriter and
                # MetricsCollector sees what ``after`` cannot, so the
                # exchange reaches the WAL and the tokens below are
                # counted once on both sides rather than only here.
                completion = ChatInvokeCompletion(
                    completion=exc.raw_output,
                    usage=exc.usage,
                    stop_reason="structured_output_validation_error",
                )
            except RunAbort as exc:
                await self._note_abort(
                    run,
                    phase="llm",
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
                # The call was billed, and this is the one abort path
                # that knows what it cost, so its usage joins the totals
                # before the result is built.
                run.llm_calls += 1
                run.usage.add(exc.usage, model=run.model_id)
                step_token_usage = _merge_token_usage(
                    step_token_usage, TokenUsage.from_llm_usage(exc.usage)
                )
                return await self._abort(
                    run,
                    phase="llm",
                    exc=exc,
                    observation=observation,
                    observation_summary=observation_summary,
                    step_idx=step_idx,
                    env_status="success",
                    token_usage=step_token_usage,
                    restoration=restoration,
                )
            except Exception as exc:
                # The environment is fine; the model call is not. A 429
                # should not be indistinguishable from a dead browser.
                return await self._abort(
                    run,
                    phase="llm",
                    exc=exc,
                    observation=observation,
                    observation_summary=observation_summary,
                    step_idx=step_idx,
                    env_status="success",
                    restoration=restoration,
                )

            run.llm_calls += 1
            # Duck-typed: a provider that reports nothing yields None,
            # and None must stay None all the way to ``metrics.usage``.
            call_usage = getattr(completion, "usage", None)
            run.usage.add(call_usage, model=run.model_id)
            step_token_usage = _merge_token_usage(
                step_token_usage, TokenUsage.from_llm_usage(call_usage)
            )

            raw_output = completion.completion
            output_text = _serialize_llm_output(raw_output)

            # Agent parses LLM output (domain knowledge). ParseError is
            # a framework-level control signal — it does NOT go through
            # action dispatch. The error detail is fed back so the LLM
            # can self-correct, in this step if retries remain and on
            # the next one otherwise.
            try:
                actions = agent.parse_react_output(raw_output)
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
                    exc=exc,
                    observation=observation,
                    observation_summary=observation_summary,
                    step_idx=step_idx,
                    llm_output=output_text,
                    token_usage=step_token_usage,
                    restoration=restoration,
                )
                raise
            except ParseError as exc:
                parse_failure = exc
                # The failed attempt and the reason it failed both land
                # in memory. The in-step retry and the next step read
                # exactly the same two messages — the only difference
                # between them is whether a new observation follows.
                self._remember_rejected(memory, output_text, exc)
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

            # Record a no-op step for tracing.
            record = StepRecord(
                step=step_idx,
                observation_summary=observation_summary,
                llm_output=output_text,
                actions=[],
                action_results=[],
                timestamp=time.time(),
                token_usage=step_token_usage,
                restoration=restoration,
            )
            run.step_records.append(record)
            run.prev_step_record = record

            if run.parse_error_streak < self.max_consecutive_parse_errors:
                await self._surrender(run.recorder, record, observation)
                return None
            # The run ends on this step, in failure: recording it must
            # not be what decides how the run ended (``_end_failing``).
            ending = await self._end_failing(
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
            return await self._with_terminal(run, ending)

        # Successful parse resets the streak.
        run.parse_error_streak = 0

        # 6. Enforce max_actions_per_step. Silent truncation makes the
        #    model re-issue steps it believes already ran, so the drop
        #    is logged, marked on the record, and fed back to the LLM.
        actions, dropped = self._cap_actions(actions, step_idx)

        # 7. Store assistant response in memory. ``output_text`` is the
        #    reply that *parsed* — on a step that took retries, the
        #    rejected attempts are already in memory, each followed by
        #    the error that rejected it.
        self._remember_reply(memory, output_text, dropped)

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
            run.action_history.append(f"{action.name}({action.params})")
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
            token_usage=step_token_usage,
            truncated_actions=len(dropped),
            error_phase="act" if act_error is not None else "",
            error=str(act_error) if act_error is not None else None,
            restoration=restoration,
        )
        run.step_records.append(record)
        run.prev_step_record = record
        if act_error is None:
            await self._surrender(run.recorder, record, observation)
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
            # ``_surrender_vetoed`` keeps the veto the outcome even if
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
            await self._surrender_vetoed(run.recorder, record, observation, act_error)
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
            # The run ends on this step, in failure (``_end_failing``).
            return await self._end_failing(
                run,
                record,
                observation,
                error=f"Action execution failed at step {step_idx}: {act_error}",
                agent_done="error",
                env_status="failed",
                answer=run.last_answer,
            )

        # 9. Check termination
        if last_done_result is not None:
            return await self._with_terminal(
                run,
                self._build_result(
                    run,
                    agent_done="done",
                    env_status="success",
                    answer=last_done_result.extracted_content or "",
                    agent_reported_success=last_done_result.success,
                ),
            )
        return None

    # -- ReAct's memory protocol ---------------------------------
    #
    # The live step and a branch run's replay of a recorded step write
    # memory through these and nothing else, so the two can never disagree
    # about what a rejected reply, an accepted one or a truncation looks
    # like to the model.

    def _cap_actions(self, actions: list[Action], step_idx: int) -> tuple[list[Action], list[str]]:
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

    def _remember_rejected(self, memory: Any, output_text: str, exc: ParseError) -> None:
        """A reply the parser refused: the reply as the assistant turn, then why it was refused."""
        memory.add_message(AssistantMessage(content=output_text))
        memory.add_message(UserMessage(content=_parse_feedback(exc)))

    def _remember_reply(self, memory: Any, output_text: str, dropped: list[str]) -> None:
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

    async def _with_terminal(self, run: LoopRun, result: AgentResult) -> AgentResult:
        """Record the terminal observation after an ending, when configured; *result* stands.

        A no-op unless ``terminal_observation`` is on. Called on the endings
        that leave the world in a state worth recording — done,
        ``max_steps``, the last tolerated parse error (and a branch run's
        budget end) — never on an infrastructure failure, a failed action
        or a veto. The record is numbered after the last record of the run.

        *result* was decided first and is what the run ends with, with two
        exceptions that keep the framework's precedence (§4.2, §4.6): a guard
        that vetoes the terminal observe outranks it — the terminal record
        is handed over as the vetoed step and the veto raised; and a failure
        to record the terminal step is appended to a failing result's
        ``error`` (a successful one only logs it). A cancellation of either
        await carries *result* (``carry_outcome``).
        """
        if not self.terminal_observation:
            return result
        step = run.step_records[-1].step + 1 if run.step_records else 0
        outcome = result.error or f"agent_done={result.agent_done}"
        try:
            record, observation = await self._observe_terminal(
                run.env, run.negotiated_obs, step=step
            )
        except RunAbort as veto:
            logger.warning(
                "ReactLoop: run aborted during the terminal observation (step %d): %s",
                step,
                veto,
            )
            vetoed = StepRecord(
                step=step,
                origin="terminal",
                timestamp=time.time(),
                error_phase="observe",
                error=str(veto),
            )
            run.step_records.append(vetoed)
            await self._surrender_vetoed(run.recorder, vetoed, None, veto)
            raise
        except asyncio.CancelledError as cancelled:
            logger.warning(
                "ReactLoop: the terminal observation (step %d) was cut off by a cancellation; "
                "the run had already ended (%s), and the cancellation carries that outcome",
                step,
                outcome,
            )
            carry_outcome(cancelled, result)
            raise
        run.step_records.append(record)
        try:
            unrecorded = await self._surrender_failing(run.recorder, record, observation)
        except asyncio.CancelledError as cancelled:
            _log_cut_off(record, outcome)
            carry_outcome(cancelled, result)
            raise
        if unrecorded is not None and result.error:
            return result.model_copy(
                update={"error": f"{result.error}{_unrecorded_suffix(unrecorded)}"}
            )
        return result

    # -- private helpers -----------------------------------------

    @staticmethod
    async def _surrender(
        recorder: Any,
        record: StepRecord,
        observation: Any,
    ) -> None:
        """Hand a finished step to the framework's step sink.

        The Loop does not know or care who is listening — ``record_step``
        is an ordinary Protocol call that the AOP layer intercepts. This
        is the only path by which actions, action results, per-step token
        usage and the observation *the agent actually saw* reach a trace.
        """
        if recorder is None:
            return
        await recorder.record_step(record, observation=observation)

    @staticmethod
    async def _surrender_failing(
        recorder: Any,
        record: StepRecord,
        observation: Any,
    ) -> Exception | None:
        """Hand over the step a failing run ends on; return a recording failure, don't raise it.

        For the steps after which the run is over because it failed — a
        veto in flight (``_note_abort``, the act phase), an infrastructure
        failure (``_abort``), a failed action, the last tolerated parse
        error — always through :meth:`_surrender_vetoed` or
        :meth:`_end_failing`. On any other step a failure to record
        propagates and ends the run: a recorder that cannot write is a run
        that must stop (``hooks/base.py``). Here the run is stopping
        already, and letting the recorder's error take the place of the
        outcome is how a guard's veto used to vanish: the sink raised (an
        archive collection broken by the very page the guard refused, a full
        disk), that error left the Loop instead of the ``RunAbort``,
        ``AgentRuntime.execute`` saw no veto to propagate past
        ``raise_on_error=False``, and the sweep started the next task; an
        ``_abort`` result likewise came back naming the recorder's error
        instead of the failure that ended the run.

        So the failure is logged and returned, and the caller attaches it to
        the outcome (:func:`_note_unrecorded`, :func:`_unrecorded_suffix`).
        Nothing is lost by not raising it: the recorder knows it could not
        write (``TraceWriter`` keeps the step's calls owed, commits them as
        trailing at close-out and closes the trace out as ``error``, as far
        as the disk lets it). When that close-out fails too — a disk that
        stays full — ``AgentRuntime.execute`` keeps the same precedence one
        layer up: the veto is still what it raises, the close-out failure
        one more note on it. A veto raised *by* the sink is still a veto and
        propagates. A cancellation is not a recording failure: it propagates
        too, and the two callers make it carry the outcome.
        """
        try:
            await ReactLoop._surrender(recorder, record, observation)
        except RunAbort:
            raise
        except Exception as exc:
            logger.error(
                "ReactLoop: step %d could not be recorded (%s: %s); the run's own outcome stands",
                record.step,
                type(exc).__name__,
                exc,
                exc_info=exc,
            )
            return exc
        return None

    async def _surrender_vetoed(
        self,
        recorder: Any,
        record: StepRecord,
        observation: Any,
        veto: RunAbort,
    ) -> None:
        """Hand over the step a veto interrupted; the veto stays the outcome, whatever happens.

        The caller re-raises *veto* afterwards. A recording failure becomes
        a note on it (:meth:`_surrender_failing`, :func:`_note_unrecorded`).
        A cancellation of the recording — the run's own timeout expiring
        while a slow archive collection takes the page the guard refused,
        or Ctrl-C — is re-raised unchanged but carries the veto
        (:func:`~spectragent.core.loops.errors.carry_outcome`): when it is
        the run's own timeout, ``AgentRuntime.execute`` still closes the run
        out as vetoed and re-raises the veto, so a sweep stops instead of
        recording one more timed-out task and starting the next.
        """
        try:
            unrecorded = await self._surrender_failing(recorder, record, observation)
        except asyncio.CancelledError as cancelled:
            _log_cut_off(record, f"the veto: {veto}")
            carry_outcome(cancelled, veto)
            raise
        _note_unrecorded(veto, unrecorded)

    async def _end_failing(
        self,
        run: LoopRun,
        record: StepRecord,
        observation: Any,
        *,
        error: str,
        **result: Any,
    ) -> AgentResult:
        """Hand over the step a failing run ends on, then build the terminal result naming *error*.

        *result* is the rest of :meth:`_build_result`'s arguments. A
        recording failure is appended to *error* (:meth:`_surrender_failing`,
        :func:`_unrecorded_suffix`). A cancellation of the recording is
        re-raised unchanged but carries the result as it stood
        (:func:`~spectragent.core.loops.errors.carry_outcome`): when it is
        the run's own timeout, ``AgentRuntime.execute`` returns that result
        instead of an ordinary timeout — which would charge a dead browser
        or a refused action to a slow model.
        """
        try:
            unrecorded = await self._surrender_failing(run.recorder, record, observation)
        except asyncio.CancelledError as cancelled:
            _log_cut_off(record, error)
            carry_outcome(cancelled, self._build_result(run, error=error, **result))
            raise
        return self._build_result(run, error=f"{error}{_unrecorded_suffix(unrecorded)}", **result)

    async def _note_abort(
        self,
        run: LoopRun,
        *,
        phase: str,
        exc: RunAbort,
        observation: Any,
        step_idx: int,
        observation_summary: str = "",
        llm_output: str = "",
        token_usage: TokenUsage | None = None,
        restoration: RestorationCheck | None = None,
    ) -> None:
        """Record the step a ``RunAbort`` interrupted, then let it fly.

        The counterpart of :meth:`_abort`, and deliberately *not* it.
        ``_abort`` converts a failure into a terminal ``AgentResult``;
        this converts nothing. It writes the partial step to the sink so
        the trace still shows what the run was doing when it was vetoed,
        and returns — the caller re-raises. If the sink cannot take the
        step, that failure becomes a note on the veto, never its
        replacement, and a cancellation that cuts the hand-over off carries
        the veto with it (:meth:`_surrender_vetoed`): the veto is what the
        caller has to see.

        **This is one of two abort shapes in ``_run_step``, not the only
        one.** Used by the phases that handle their exception where it is
        raised: observe, prompt, llm, parse. The act phase does *not* come
        through here — its per-action ``except`` captures the exception so
        the failure can be attributed to one action among several, builds
        the ordinary (and richer) ``StepRecord``, hands it over through the
        same :meth:`_surrender_vetoed`, and only then re-raises on
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
            token_usage=token_usage,
            error_phase=phase,
            error=str(exc),
            restoration=restoration,
        )
        run.step_records.append(record)
        await self._surrender_vetoed(run.recorder, record, observation, exc)

    async def _abort(
        self,
        run: LoopRun,
        *,
        phase: str,
        exc: Exception,
        observation: Any,
        step_idx: int,
        observation_summary: str = "",
        env_status: str = "failed",
        token_usage: TokenUsage | None = None,
        restoration: RestorationCheck | None = None,
    ) -> AgentResult:
        """Terminate on an infrastructure failure, with the step recorded.

        A transient CDP disconnect or a single 429 used to take the whole
        run down as an unhandled exception. Now it produces a normal
        terminal result whose ``env_status`` says whether the environment
        was the thing that broke.

        ``token_usage`` defaults to ``None`` because most failures here cost
        nothing — a dead socket bills no tokens, and the schema's ``None``
        already means "not measured". A caller passes it only when the
        failed call did have a price, which is why a truncated reply hands
        one over and a 429 does not.
        """
        logger.exception("ReactLoop: %s failed at step %d", phase, step_idx)
        record = StepRecord(
            step=step_idx,
            observation_summary=observation_summary,
            timestamp=time.time(),
            error_phase=phase,
            error=str(exc),
            token_usage=token_usage,
            restoration=restoration,
        )
        run.step_records.append(record)
        return await self._end_failing(
            run,
            record,
            observation,
            error=f"{phase} failed at step {step_idx}: {type(exc).__name__}: {exc}",
            agent_done="error",
            env_status=env_status,
            answer=run.last_answer,
        )

    def _today_iso(self) -> str:
        """ISO date string for ``StepInfoObs.today``.

        Honours the ``today_override`` config field so WAL replay and
        deterministic tests reproduce identical observations.
        """
        return self.today_override or date.today().isoformat()

    def _loop_fragment_builders(self, run: LoopRun, step_idx: int) -> dict[str, Any]:
        """Builders for every loop-source fragment, from this run's values.

        The construction itself is ``_loop_fragments.loop_fragment_builders``
        (shared with ``BranchLoop`` and the training export); this Loop hands
        it ``max_steps`` and today's date (``today_override`` first).
        """
        return loop_fragment_builders(
            task=run.task,
            step=step_idx,
            max_steps=self.max_steps,
            today=self._today_iso(),
            history=run.step_records,
            prev_step=run.prev_step_record,
        )

    def _build_result(
        self,
        run: LoopRun,
        *,
        agent_done: str,
        env_status: str,
        answer: str = "",
        agent_reported_success: bool | None = None,
        error: str | None = None,
    ) -> AgentResult:
        """Build AgentResult from the run's books.

        Usage is ``None`` when no provider reported token counts — that is
        the schema's "not measured" sentinel and must not be replaced by a
        zero-filled object. ``steps`` counts the run's policy steps only
        (:attr:`~spectragent.core.loops.run.LoopRun.policy_steps`).
        """
        elapsed_ms = int((time.monotonic() - run.start_time) * 1000)
        return AgentResult(
            task_id=run.task_id,
            task=run.task,
            timestamp=datetime.now(UTC),
            env_status=EnvironmentStatus(env_status),
            agent_done=AgentDoneStatus(agent_done),
            agent_reported_success=agent_reported_success,
            answer=answer,
            error=error,
            model_id=run.model_id,
            action_history=run.action_history,
            metrics=AgentMetrics(
                end_to_end_ms=elapsed_ms,
                steps=run.policy_steps,
                llm_calls=run.llm_calls,
                usage=run.usage.snapshot(),
            ),
        )

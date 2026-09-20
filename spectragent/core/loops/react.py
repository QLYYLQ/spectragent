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
    last_action_result_obs,
    populate_loop_fragments,
)
from spectragent.core.loops._registrar import AgentLoopBase
from spectragent.core.loops.errors import ParseError, RunAbort
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult, AgentUsage
from spectragent.schemas.observation.dynamic_model import summarize_observation
from spectragent.schemas.observation.fragments import (
    HistoryObs,
    StepInfoObs,
    TaskObs,
)
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

    Configuration fields (conscribe config-annotated):
    - ``max_steps``: maximum number of steps before termination
    - ``max_actions_per_step``: maximum actions per LLM response
    - ``max_consecutive_parse_errors``: consecutive unparseable *steps*
      before termination
    - ``parse_retries_per_step``: extra re-asks inside one step, on the
      observation that step already took
    - ``today_override``: pins ``StepInfoObs.today`` for replay
    """

    __registry_key__ = "react"
    __required_slots__ = {"build_react_prompt", "parse_react_output"}
    __optional_slots__ = {"get_react_output_model", "prepare"}
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
        negotiated_obs, active_descriptors, task_id.

        Returns:
            AgentResult with execution trace and metrics.
        """
        env = ctx["env"]
        llm = ctx["llm"]
        memory = ctx["memory"]
        act_fn = ctx["act_fn"]
        negotiated_obs = ctx.get("negotiated_obs")
        task_id: str = ctx.get("task_id", "unknown")
        # The framework's step sink. Absent only in narrow unit tests that
        # weave a loop by hand; the run is otherwise identical, just
        # unrecorded.
        recorder: Any = ctx.get("step_recorder")
        model_id = _model_name(llm)

        start_time = time.monotonic()
        step_records: list[StepRecord] = []
        action_history: list[str] = []
        # Reported as ``usage=None`` unless some provider actually reported
        # numbers: an all-zero AgentUsage masquerades as "measured, cost
        # nothing" and destroys the schema's own unmeasured sentinel.
        tally = _UsageTally()
        llm_calls = 0
        parse_error_streak = 0
        prev_step_record: StepRecord | None = None
        # Best answer seen so far. Carried onto *every* termination path,
        # including max_steps — an agent that produced the deliverable and
        # then ran out of steps must not have it thrown away.
        last_answer = ""

        for step_idx in range(self.max_steps):
            # 1. Observe — env fills env-source fragments; loop fills
            #    loop-source fragments (task / step_info / history /
            #    action_result) immediately after.
            try:
                observation = await env.observe(negotiated_obs)
                loop_sourced = getattr(negotiated_obs, "loop_sourced", frozenset())
                if loop_sourced:
                    observation = populate_loop_fragments(
                        observation,
                        loop_sourced=loop_sourced,
                        builders=self._loop_fragment_builders(
                            task=task,
                            step_idx=step_idx,
                            step_records=step_records,
                            prev_step_record=prev_step_record,
                        ),
                    )
            except RunAbort as exc:
                await self._note_abort(
                    phase="observe",
                    exc=exc,
                    recorder=recorder,
                    observation=None,
                    step_idx=step_idx,
                    step_records=step_records,
                )
                raise
            except Exception as exc:
                return await self._abort(
                    phase="observe",
                    exc=exc,
                    recorder=recorder,
                    observation=None,
                    step_idx=step_idx,
                    step_records=step_records,
                    action_history=action_history,
                    task_id=task_id,
                    task=task,
                    start_time=start_time,
                    usage=tally.snapshot(),
                    llm_calls=llm_calls,
                    model_id=model_id,
                    answer=last_answer,
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
            try:
                new_msgs = agent.build_react_prompt(observation)
            except RunAbort as exc:
                await self._note_abort(
                    phase="prompt",
                    exc=exc,
                    recorder=recorder,
                    observation=observation,
                    observation_summary=observation_summary,
                    step_idx=step_idx,
                    step_records=step_records,
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
                    completion = await llm.ainvoke(context, output_format=output_model)
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
                        phase="llm",
                        exc=exc,
                        recorder=recorder,
                        observation=observation,
                        observation_summary=observation_summary,
                        step_idx=step_idx,
                        step_records=step_records,
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
                    llm_calls += 1
                    tally.add(exc.usage, model=model_id)
                    step_token_usage = _merge_token_usage(
                        step_token_usage, TokenUsage.from_llm_usage(exc.usage)
                    )
                    return await self._abort(
                        phase="llm",
                        exc=exc,
                        recorder=recorder,
                        observation=observation,
                        observation_summary=observation_summary,
                        step_idx=step_idx,
                        step_records=step_records,
                        action_history=action_history,
                        task_id=task_id,
                        task=task,
                        start_time=start_time,
                        usage=tally.snapshot(),
                        llm_calls=llm_calls,
                        model_id=model_id,
                        answer=last_answer,
                        env_status="success",
                        token_usage=step_token_usage,
                    )
                except Exception as exc:
                    # The environment is fine; the model call is not. A 429
                    # should not be indistinguishable from a dead browser.
                    return await self._abort(
                        phase="llm",
                        exc=exc,
                        recorder=recorder,
                        observation=observation,
                        observation_summary=observation_summary,
                        step_idx=step_idx,
                        step_records=step_records,
                        action_history=action_history,
                        task_id=task_id,
                        task=task,
                        start_time=start_time,
                        usage=tally.snapshot(),
                        llm_calls=llm_calls,
                        model_id=model_id,
                        answer=last_answer,
                        env_status="success",
                    )

                llm_calls += 1
                # Duck-typed: a provider that reports nothing yields None,
                # and None must stay None all the way to ``metrics.usage``.
                call_usage = getattr(completion, "usage", None)
                tally.add(call_usage, model=model_id)
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
                        phase="parse",
                        exc=exc,
                        recorder=recorder,
                        observation=observation,
                        observation_summary=observation_summary,
                        step_idx=step_idx,
                        step_records=step_records,
                        llm_output=output_text,
                        token_usage=step_token_usage,
                    )
                    raise
                except ParseError as exc:
                    parse_failure = exc
                    # The failed attempt and the reason it failed both land
                    # in memory. The in-step retry and the next step read
                    # exactly the same two messages — the only difference
                    # between them is whether a new observation follows.
                    memory.add_message(AssistantMessage(content=output_text))
                    memory.add_message(UserMessage(content=_parse_feedback(exc)))
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
                parse_error_streak += 1
                logger.warning(
                    "ReactLoop: parse error (%d/%d): %s",
                    parse_error_streak,
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
                )
                step_records.append(record)
                prev_step_record = record
                await self._surrender(recorder, record, observation)

                if parse_error_streak >= self.max_consecutive_parse_errors:
                    return self._build_result(
                        task_id=task_id,
                        task=task,
                        start_time=start_time,
                        step_records=step_records,
                        action_history=action_history,
                        usage=tally.snapshot(),
                        llm_calls=llm_calls,
                        model_id=model_id,
                        agent_done="error",
                        env_status="success",
                        answer=last_answer,
                        error=(
                            f"Parse failed {parse_error_streak} "
                            f"consecutive times: {parse_failure.detail}"
                        ),
                    )
                continue

            # Successful parse resets the streak.
            parse_error_streak = 0

            # 6. Enforce max_actions_per_step. Silent truncation makes the
            #    model re-issue steps it believes already ran, so the drop
            #    is logged, marked on the record, and fed back to the LLM.
            truncated = 0
            if len(actions) > self.max_actions_per_step:
                truncated = len(actions) - self.max_actions_per_step
                dropped = [a.name for a in actions[self.max_actions_per_step :]]
                actions = actions[: self.max_actions_per_step]
                logger.warning(
                    "ReactLoop: step %d emitted %d actions, max_actions_per_step=%d — discarded %s",
                    step_idx,
                    truncated + self.max_actions_per_step,
                    self.max_actions_per_step,
                    dropped,
                )

            # 7. Store assistant response in memory. ``output_text`` is the
            #    reply that *parsed* — on a step that took retries, the
            #    rejected attempts are already in memory, each followed by
            #    the error that rejected it.
            memory.add_message(AssistantMessage(content=output_text))

            if truncated:
                memory.add_message(
                    UserMessage(
                        content=(
                            f"[SYSTEM] Only the first {self.max_actions_per_step} "
                            f"action(s) of your response were executed. "
                            f"{truncated} action(s) were discarded and did NOT "
                            f"run: {', '.join(dropped)}. Re-issue them if still "
                            f"needed."
                        )
                    )
                )

            # 8. Execute actions via guarded_act
            step_results: list[ActionResult] = []
            last_done_result: ActionResult | None = None
            act_error: Exception | None = None
            for action in actions:
                try:
                    result = await act_fn(action)
                except Exception as exc:  # noqa: PERF203 — per-action attribution
                    act_error = exc
                    break
                step_results.append(result)
                action_history.append(f"{action.name}({action.params})")
                if result.extracted_content:
                    last_answer = result.extracted_content
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
                truncated_actions=truncated,
                error_phase="act" if act_error is not None else "",
                error=str(act_error) if act_error is not None else None,
            )
            step_records.append(record)
            prev_step_record = record
            await self._surrender(recorder, record, observation)

            if isinstance(act_error, RunAbort):
                # Not an action failure — a veto. Something woven onto
                # ``act`` (a safety guard watching for a risk-control
                # response, say) has decided the run must not continue, and
                # turning that into an ordinary ``agent_done="error"``
                # result is exactly how the veto used to get lost: the
                # caller could not tell it from a click that happened to
                # raise, and a benchmark driver went on to the next task.
                #
                # The step is surrendered *above* first — the evidence the
                # guard fired on is the most valuable thing in the trace —
                # and only then does the exception leave the Loop, so
                # AgentRuntime closes the run out (``on_run_end`` +
                # ``on_run_error``) and re-raises to whoever started it.
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
                raise act_error

            if act_error is not None:
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
                return self._build_result(
                    task_id=task_id,
                    task=task,
                    start_time=start_time,
                    step_records=step_records,
                    action_history=action_history,
                    usage=tally.snapshot(),
                    llm_calls=llm_calls,
                    model_id=model_id,
                    agent_done="error",
                    env_status="failed",
                    answer=last_answer,
                    error=f"Action execution failed at step {step_idx}: {act_error}",
                )

            # 9. Check termination
            if last_done_result is not None:
                return self._build_result(
                    task_id=task_id,
                    task=task,
                    start_time=start_time,
                    step_records=step_records,
                    action_history=action_history,
                    usage=tally.snapshot(),
                    llm_calls=llm_calls,
                    model_id=model_id,
                    agent_done="done",
                    env_status="success",
                    answer=last_done_result.extracted_content or "",
                    agent_reported_success=last_done_result.success,
                )

        # Max steps reached
        return self._build_result(
            task_id=task_id,
            task=task,
            start_time=start_time,
            step_records=step_records,
            action_history=action_history,
            usage=tally.snapshot(),
            llm_calls=llm_calls,
            model_id=model_id,
            agent_done="max_steps",
            env_status="success",
            answer=last_answer,
        )

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

    async def _note_abort(
        self,
        *,
        phase: str,
        exc: RunAbort,
        recorder: Any,
        observation: Any,
        step_idx: int,
        step_records: list[StepRecord],
        observation_summary: str = "",
        llm_output: str = "",
        token_usage: TokenUsage | None = None,
    ) -> None:
        """Record the step a ``RunAbort`` interrupted, then let it fly.

        The counterpart of :meth:`_abort`, and deliberately *not* it.
        ``_abort`` converts a failure into a terminal ``AgentResult``;
        this converts nothing. It writes the partial step to the sink so
        the trace still shows what the run was doing when it was vetoed,
        and returns — the caller re-raises.

        **This is one of two abort shapes in ``_execute``, not the only
        one.** Used by the phases that handle their exception where it is
        raised: observe, prompt, llm, parse. The act phase does *not* come
        through here — its per-action ``except`` captures the exception so
        the failure can be attributed to one action among several, builds
        the ordinary (and richer) ``StepRecord``, ``_surrender``s it, and
        only then re-raises on ``isinstance(act_error, RunAbort)``. Calling
        ``_note_abort`` there would write the step twice.

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
        )
        step_records.append(record)
        await self._surrender(recorder, record, observation)

    async def _abort(
        self,
        *,
        phase: str,
        exc: Exception,
        recorder: Any,
        observation: Any,
        step_idx: int,
        step_records: list[StepRecord],
        action_history: list[str],
        task_id: str,
        task: str,
        start_time: float,
        usage: AgentUsage | None,
        llm_calls: int,
        model_id: str,
        answer: str,
        observation_summary: str = "",
        env_status: str = "failed",
        token_usage: TokenUsage | None = None,
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
        )
        step_records.append(record)
        await self._surrender(recorder, record, observation)
        return self._build_result(
            task_id=task_id,
            task=task,
            start_time=start_time,
            step_records=step_records,
            action_history=action_history,
            usage=usage,
            llm_calls=llm_calls,
            model_id=model_id,
            agent_done="error",
            env_status=env_status,
            answer=answer,
            error=f"{phase} failed at step {step_idx}: {type(exc).__name__}: {exc}",
        )

    def _today_iso(self) -> str:
        """ISO date string for ``StepInfoObs.today``.

        Honours the ``today_override`` config field so WAL replay and
        deterministic tests reproduce identical observations.
        """
        return self.today_override or date.today().isoformat()

    def _loop_fragment_builders(
        self,
        *,
        task: str,
        step_idx: int,
        step_records: list[StepRecord],
        prev_step_record: StepRecord | None,
    ) -> dict[str, Any]:
        """Builders for every loop-source fragment ReactLoop owns.

        ``populate_loop_fragments`` only invokes the builders whose keys
        are in the agent's negotiated ``loop_sourced`` set, so unused
        builders incur zero cost.
        """
        return {
            "loop.task": lambda: TaskObs(user_request=task),
            "loop.step_info": lambda: StepInfoObs(
                step=step_idx + 1,
                max_steps=self.max_steps,
                today=self._today_iso(),
            ),
            "loop.history": lambda: HistoryObs(items=list(step_records)),
            "loop.action_result": lambda: last_action_result_obs(prev_step_record),
        }

    def _build_result(
        self,
        *,
        task_id: str,
        task: str,
        start_time: float,
        step_records: list[StepRecord],
        action_history: list[str],
        usage: AgentUsage | None,
        agent_done: str,
        env_status: str,
        llm_calls: int = 0,
        model_id: str = "",
        answer: str = "",
        agent_reported_success: bool | None = None,
        error: str | None = None,
    ) -> AgentResult:
        """Build AgentResult from step records and metrics.

        *usage* is ``None`` when no provider reported token counts —
        that is the schema's "not measured" sentinel and must not be
        replaced by a zero-filled object.
        """
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return AgentResult(
            task_id=task_id,
            task=task,
            timestamp=datetime.now(UTC),
            env_status=EnvironmentStatus(env_status),
            agent_done=AgentDoneStatus(agent_done),
            agent_reported_success=agent_reported_success,
            answer=answer,
            error=error,
            model_id=model_id,
            action_history=action_history,
            metrics=AgentMetrics(
                end_to_end_ms=elapsed_ms,
                steps=len(step_records),
                llm_calls=llm_calls,
                usage=usage,
            ),
        )

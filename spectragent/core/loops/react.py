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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.llm.messages import AssistantMessage
from spectragent.core.loops._loop_fragments import (
    last_action_result_obs,
    populate_loop_fragments,
)
from spectragent.core.loops._registrar import AgentLoopBase
from spectragent.core.loops.errors import ParseError
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import ActionResult
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
    ``repr``, which the model cannot imitate and would learn from.
    """
    if isinstance(raw_output, str):
        return raw_output
    model_dump_json = getattr(raw_output, "model_dump_json", None)
    if callable(model_dump_json):
        return str(model_dump_json())
    return str(raw_output)


@dataclass
class ReactLoop(AgentLoopBase):
    """Standard ReAct loop: observe -> think -> act -> repeat.

    Requires agents to have ``build_react_prompt`` and ``parse_react_output``
    callable slots. Optionally uses ``get_react_output_model`` and ``prepare``.

    Configuration fields (conscribe config-annotated):
    - ``max_steps``: maximum number of steps before termination
    - ``max_actions_per_step``: maximum actions per LLM response
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
        total_usage = AgentUsage()
        usage_measured = False
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
                    usage=total_usage if usage_measured else None,
                    llm_calls=llm_calls,
                    model_id=model_id,
                    answer=last_answer,
                )

            observation_summary = summarize_observation(observation)

            # 2. Agent builds new step messages (pure builder — returns
            #    only what the agent wants appended this step). Loop owns
            #    the memory write and assembles the full LLM context.
            new_msgs = agent.build_react_prompt(observation)
            memory.add_messages(new_msgs)
            context = memory.get_context_messages()

            # 3. Loop drives LLM call. ``output_format=None`` yields a
            #    ``ChatInvokeCompletion[str]``; a non-None model yields a
            #    ``ChatInvokeCompletion[T]`` already validated by the
            #    provider.
            output_model = (
                agent.get_react_output_model()
                if hasattr(agent, "get_react_output_model")
                else None
            )
            try:
                completion = await llm.ainvoke(context, output_format=output_model)
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
                    usage=total_usage if usage_measured else None,
                    llm_calls=llm_calls,
                    model_id=model_id,
                    answer=last_answer,
                    env_status="success",
                )

            llm_calls += 1
            # Duck-typed: a provider that reports nothing yields None, and
            # None must stay None all the way to ``metrics.usage``.
            call_usage = getattr(completion, "usage", None)
            step_token_usage = TokenUsage.from_llm_usage(call_usage)
            if call_usage is not None:
                usage_measured = True
            total_usage.add_llm_usage(call_usage, model=model_id)

            raw_output = completion.completion

            # 5. Agent parses LLM output (domain knowledge).
            #    ParseError is a framework-level control signal — it
            #    does NOT go through action dispatch. The error detail
            #    is fed back into the next prompt so the LLM can
            #    self-correct. Consecutive failures terminate the run.
            try:
                actions = agent.parse_react_output(raw_output)
            except ParseError as exc:
                parse_error_streak += 1
                logger.warning(
                    "ReactLoop: parse error (%d/%d): %s",
                    parse_error_streak,
                    self.max_consecutive_parse_errors,
                    exc.detail,
                )

                # Store the LLM output in memory so the next prompt
                # includes the failed attempt.
                output_text = _serialize_llm_output(raw_output)
                memory.add_message(AssistantMessage(content=output_text))

                # Feed the parse error back as a user-role message so
                # the LLM sees what went wrong on its next turn.
                from spectragent.core.llm.messages import UserMessage

                memory.add_message(
                    UserMessage(
                        content=(
                            f"[SYSTEM] Your previous output could not be "
                            f"parsed into actions. Error: {exc.detail}\n"
                            f"Please try again with valid output format."
                        )
                    )
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
                        usage=total_usage if usage_measured else None,
                        llm_calls=llm_calls,
                        model_id=model_id,
                        agent_done="error",
                        env_status="success",
                        answer=last_answer,
                        error=(
                            f"Parse failed {parse_error_streak} "
                            f"consecutive times: {exc.detail}"
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
                    "ReactLoop: step %d emitted %d actions, max_actions_per_step=%d "
                    "— discarded %s",
                    step_idx,
                    truncated + self.max_actions_per_step,
                    self.max_actions_per_step,
                    dropped,
                )

            # 7. Store assistant response in memory
            output_text = _serialize_llm_output(raw_output)
            memory.add_message(AssistantMessage(content=output_text))

            if truncated:
                from spectragent.core.llm.messages import UserMessage

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
                    usage=total_usage if usage_measured else None,
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
                    usage=total_usage if usage_measured else None,
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
            usage=total_usage if usage_measured else None,
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
    ) -> AgentResult:
        """Terminate on an infrastructure failure, with the step recorded.

        A transient CDP disconnect or a single 429 used to take the whole
        run down as an unhandled exception. Now it produces a normal
        terminal result whose ``env_status`` says whether the environment
        was the thing that broke.
        """
        logger.exception("ReactLoop: %s failed at step %d", phase, step_idx)
        record = StepRecord(
            step=step_idx,
            observation_summary=observation_summary,
            timestamp=time.time(),
            error_phase=phase,
            error=str(exc),
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

"""ReactLoop — standard ReAct (Reason + Act) execution strategy.

Drives the observe -> build_prompt -> LLM -> parse_output -> act cycle.
Extracted from ``BaseReactAgent.run()`` as an independent Loop that
calls Agent Protocol methods for domain knowledge.

**AOP-transparent design**: ReactLoop has NO emitter/hook parameter.
AOP hooks are woven into agent/env/llm by the Weaver (M04) at
runtime. The Loop is completely unaware of hooks.

See docs/architecture_design.md §4.1 and docs/execution_dag.md M09.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import Field

from spectragent.core.llm.messages import AssistantMessage
from spectragent.core.loops._registrar import AgentLoopBase
from spectragent.core.loops.errors import ParseError
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult, AgentUsage
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)


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

    # -- AgentLoopBase._execute() implementation ----------------

    async def _execute(
        self,
        agent: Any,
        task: str,
        **kwargs: Any,
    ) -> AgentResult:
        """Drive the full ReAct loop.

        Runtime deps are read from ``self.ctx`` (bound at weave time):
        env, llm, memory, act_fn, negotiated_obs, active_descriptors,
        session_id, task_id.

        Returns:
            AgentResult with execution trace and metrics.
        """
        ctx = self.ctx
        env = ctx["env"]
        llm = ctx["llm"]
        memory = ctx["memory"]
        act_fn = ctx["act_fn"]
        negotiated_obs = ctx.get("negotiated_obs")
        active_descriptors = ctx.get("active_descriptors", [])
        session_id: str | None = ctx.get("session_id")
        task_id: str = ctx.get("task_id", "unknown")

        # Optional one-shot agent setup. Agents may define
        # ``prepare(task, action_descriptors)`` to seed their system
        # prompt and internal state before the first
        # ``build_react_prompt`` call (declared in __optional_slots__).
        prepare = getattr(agent, "prepare", None)
        if callable(prepare):
            prepare(task=task, action_descriptors=active_descriptors)

        start_time = time.monotonic()
        step_records: list[StepRecord] = []
        action_history: list[str] = []
        total_usage = AgentUsage()
        parse_error_streak = 0

        for step_idx in range(self.max_steps):
            # 1. Observe
            if negotiated_obs is not None:
                observation = await env.observe(negotiated_obs, session_id)
            else:
                observation = await env.observe(session_id)

            # 2. Agent builds prompt (domain knowledge — pure, no memory side-effect)
            messages = agent.build_react_prompt(observation)

            # 3. Get structured output model (optional slot)
            get_output_model = getattr(agent, "get_react_output_model", None)
            output_model = get_output_model() if callable(get_output_model) else None

            # 4. Loop drives LLM call
            if output_model is not None:
                completion = await llm.ainvoke(messages, output_format=output_model)
            else:
                completion = await llm.ainvoke(messages)

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
                output_text = (
                    raw_output
                    if isinstance(raw_output, str)
                    else str(raw_output)
                )
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
                    llm_output=output_text,
                    actions=[],
                    action_results=[],
                    timestamp=time.time(),
                )
                step_records.append(record)

                if parse_error_streak >= self.max_consecutive_parse_errors:
                    return self._build_result(
                        task_id=task_id,
                        task=task,
                        start_time=start_time,
                        step_records=step_records,
                        action_history=action_history,
                        usage=total_usage,
                        agent_done="error",
                        env_status="success",
                        error=(
                            f"Parse failed {parse_error_streak} "
                            f"consecutive times: {exc.detail}"
                        ),
                    )
                continue

            # Successful parse resets the streak.
            parse_error_streak = 0

            # 6. Enforce max_actions_per_step
            if len(actions) > self.max_actions_per_step:
                actions = actions[: self.max_actions_per_step]

            # 7. Store assistant response in memory
            output_text = (
                raw_output
                if isinstance(raw_output, str)
                else raw_output.model_dump_json()
            )
            memory.add_message(AssistantMessage(content=output_text))

            # 8. Execute actions via guarded_act
            step_results: list[ActionResult] = []
            last_done_result: ActionResult | None = None
            for action in actions:
                result = await act_fn(action, session_id=session_id)
                step_results.append(result)
                action_history.append(f"{action.name}({action.params})")
                if result.is_done:
                    last_done_result = result
                    break

            # Build step record
            record = StepRecord(
                step=step_idx,
                llm_output=output_text,
                actions=actions,
                action_results=step_results,
                timestamp=time.time(),
            )
            step_records.append(record)

            # 9. Check termination
            if last_done_result is not None:
                return self._build_result(
                    task_id=task_id,
                    task=task,
                    start_time=start_time,
                    step_records=step_records,
                    action_history=action_history,
                    usage=total_usage,
                    agent_done="done",
                    env_status="success",
                    answer=last_done_result.extracted_content or "",
                    agent_success=last_done_result.success,
                )

        # Max steps reached
        return self._build_result(
            task_id=task_id,
            task=task,
            start_time=start_time,
            step_records=step_records,
            action_history=action_history,
            usage=total_usage,
            agent_done="max_steps",
            env_status="success",
        )

    # -- private helpers -----------------------------------------

    def _build_result(
        self,
        *,
        task_id: str,
        task: str,
        start_time: float,
        step_records: list[StepRecord],
        action_history: list[str],
        usage: AgentUsage,
        agent_done: str,
        env_status: str,
        answer: str = "",
        agent_success: bool | None = None,
        error: str | None = None,
    ) -> AgentResult:
        """Build AgentResult from step records and metrics."""
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return AgentResult(
            task_id=task_id,
            task=task,
            timestamp=datetime.now(timezone.utc),
            env_status=EnvironmentStatus(env_status),
            agent_done=AgentDoneStatus(agent_done),
            agent_success=agent_success,
            answer=answer,
            error=error,
            action_history=action_history,
            metrics=AgentMetrics(
                end_to_end_ms=elapsed_ms,
                steps=len(step_records),
                usage=usage,
            ),
        )

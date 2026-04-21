"""AgentRuntime — the framework's central orchestration engine.

AgentRuntime is loop-agnostic: it handles slot validation,
three-party observation negotiation, action negotiation + guarded_act,
AOP hook weaving, loop weaving, timeout control, and error handling.

``prepare()`` assembles all dependencies and weaves the Loop into the
agent instance — after which ``agent.run(task)`` is self-contained.
``execute()`` is a convenience wrapper that calls ``prepare()`` then
drives ``agent.run()`` with timeout + lifecycle hooks.

Not a registrar component — each framework has one AgentRuntime.
Not a singleton — each ``execute()`` / ``prepare()`` call is independent.

See docs/architecture_design.md §4.2 and docs/execution_dag.md M08.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from typing import Any

from spectragent.core.action_guard import build_guarded_act
from spectragent.core.actions.negotiation import (
    ActionNegotiationResult,
    extract_action_wiring,
    negotiate_actions,
)
from spectragent.core.aop.weaver import Weaver
from spectragent.core.composer import ComposedRun
from spectragent.core.hooks.base import RuntimeHook
from spectragent.schemas.observation.negotiation import (
    extract_observation_wiring,
    negotiate,
)

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Loop-agnostic orchestration engine.

    Responsibilities:
    1. Validate Agent-Loop slot compatibility
    2. Run three-party observation negotiation
    3. Aggregate action space + build guarded_act
    4. Weave AOP hooks into agent/env/llm proxies
    5. Weave Loop execution logic into agent.run
    6. (execute only) Drive agent.run() with timeout + lifecycle hooks

    Args:
        hooks: Active RuntimeHook instances for this run.
    """

    def __init__(self, run: ComposedRun) -> None:
        self._run = run
        self._hooks: list[RuntimeHook] = list(run.hooks)

    # -- Factory ----------------------------------------------

    @classmethod
    def from_composed(cls, run: ComposedRun) -> AgentRuntime:
        """Build an AgentRuntime from a :class:`ComposedRun`."""
        return cls(run)

    # -- prepare: assembly phase -------------------------------

    def prepare(
        self,
        *,
        session_id: str | None = None,
        task_id: str = "unknown",
        action_filter: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Assemble all dependencies and weave Loop into agent.run.

        Reads ``agent``, ``loop``, ``env``, ``llm``, ``memory`` off the
        ``ComposedRun`` bound at construction time.  After return,
        ``run.agent.run(task)`` is fully self-contained.
        """
        run = self._run
        agent = run.agent
        loop = run.loop
        env = run.env
        llm = run.llm
        memory = run.memory
        # Step 1: Slot validation
        loop._validate_slots(agent)

        # Step 2: Observation three-party negotiation
        negotiated_obs = self._negotiate_observation(agent, loop, env)

        # Step 2.5: Action two-party negotiation
        negotiated_actions = self._negotiate_actions(agent, env)

        # Step 3: AOP weaving (must happen before guarded_act so
        #          act_fn captures wrapped_env.act, enabling AOP intercepts)
        weaver = Weaver(self._hooks)
        wrapped_agent = weaver.wrap(agent, role="agent")
        wrapped_env = weaver.wrap(env, role="env")
        wrapped_llm = weaver.wrap(llm, role="llm")

        # Step 4: Narrow descriptors via the action negotiation result.
        narrowed_descriptors = wrapped_env.get_action_space(
            active_actions=negotiated_actions.active_actions,
        )
        active_descriptors, act_fn = build_guarded_act(
            wrapped_env.act,
            narrowed_descriptors,
            action_filter,
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
            session_id=session_id,
            task_id=task_id,
            **kwargs,
        )

        return wrapped_agent

    # -- execute: convenience one-shot -------------------------

    async def execute(
        self,
        task: str,
        *,
        timeout: float = 300,
        session_id: str | None = None,
        task_id: str = "unknown",
        action_filter: list[str] | None = None,
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """One-shot: prepare + run + lifecycle hooks + timeout."""
        run = self._run
        prepared_agent = self.prepare(
            session_id=session_id,
            task_id=task_id,
            action_filter=action_filter,
            **kwargs,
        )

        # Hook lifecycle — run_start
        await self._dispatch_lifecycle(
            "on_run_start",
            agent=run.agent,
            env=run.env,
            task=task,
            config=config or {},
        )

        # Drive agent.run with timeout
        try:
            result = await asyncio.wait_for(
                prepared_agent.run(task),
                timeout=timeout,
            )

            # Success — notify hooks
            await self._dispatch_lifecycle("on_run_end", result=result)
            return result

        except TimeoutError:
            logger.warning("AgentRuntime: execution timed out after %.1fs", timeout)
            from datetime import datetime

            from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
            from spectragent.schemas.agent_result import (
                AgentMetrics,
                AgentResult,
                AgentUsage,
            )

            timeout_result = AgentResult(
                task_id=task_id,
                task=task,
                timestamp=datetime.now(UTC),
                env_status=EnvironmentStatus.SUCCESS,
                agent_done=AgentDoneStatus.TIMEOUT,
                error=f"Execution timed out after {timeout}s",
                metrics=AgentMetrics(
                    end_to_end_ms=int(timeout * 1000),
                    steps=0,
                    usage=AgentUsage(),
                ),
            )
            await self._dispatch_lifecycle(
                "on_run_error",
                error=TimeoutError(),
                partial_steps=[],
            )
            return timeout_result

        except Exception as exc:
            logger.exception("AgentRuntime: execution failed")
            await self._dispatch_lifecycle(
                "on_run_error",
                error=exc,
                partial_steps=[],
            )
            raise

    # -- Private helpers ---------------------------------------

    def _negotiate_observation(
        self,
        agent: Any,
        loop: Any,
        env: Any,
    ) -> Any:
        """Run three-party observation negotiation."""
        env_provides = set(env.provided_fragments)

        agent_required, agent_optional = extract_observation_wiring(type(agent))
        loop_required, loop_optional = extract_observation_wiring(type(loop))

        return negotiate(
            env_provides=env_provides,
            agent_required=agent_required,
            agent_optional=agent_optional,
            loop_required=loop_required,
            loop_optional=loop_optional,
        )

    def _negotiate_actions(
        self,
        agent: Any,
        env: Any,
    ) -> ActionNegotiationResult:
        """Run two-party action negotiation."""
        env_provides = set(env.provided_actions)
        agent_required, agent_optional = extract_action_wiring(type(agent))
        return negotiate_actions(
            env_provides=env_provides,
            agent_required=agent_required,
            agent_optional=agent_optional,
        )

    async def _dispatch_lifecycle(
        self,
        method_name: str,
        **kwargs: Any,
    ) -> None:
        """Dispatch a lifecycle callback to all hooks."""
        for hook in self._hooks:
            handler = getattr(hook, method_name, None)
            if handler is None:
                continue

            if hook.blocking:
                await handler(**kwargs)
            else:
                asyncio.create_task(self._safe_lifecycle_call(hook, handler, method_name, kwargs))

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

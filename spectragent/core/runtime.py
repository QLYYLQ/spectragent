"""AgentRuntime — the framework's central orchestration engine.

AgentRuntime is loop-agnostic: it handles compatibility validation,
three-party observation negotiation, action negotiation + guarded_act,
AOP hook weaving, timeout control, and error handling. The actual
observe-think-act cycle is delegated to the Loop.

Not a registrar component — each framework has one AgentRuntime.
Not a singleton — each ``execute()`` call is an independent context.

See docs/architecture_design.md §4.2 and docs/execution_dag.md M08.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from spectragent.core.action_guard import build_guarded_act
from spectragent.core.aop.weaver import Weaver
from spectragent.core.hooks.base import RuntimeHook
from spectragent.core.loops.validation import validate_compatibility
from spectragent.schemas.observation.negotiation import (
    extract_observation_wiring,
    negotiate,
)

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Loop-agnostic orchestration engine.

    Responsibilities:
    1. Validate Agent-Loop compatibility
    2. Run three-party observation negotiation
    3. Aggregate action space + build guarded_act
    4. Weave AOP hooks into agent/env/llm proxies
    5. Drive Loop.run() with timeout control
    6. Dispatch hook lifecycle callbacks (run_start/end/error)

    Args:
        hooks: Active RuntimeHook instances for this run.
    """

    def __init__(self, hooks: list[RuntimeHook] | None = None) -> None:
        self._hooks = hooks or []

    async def execute(
        self,
        agent: Any,
        loop: Any,
        env: Any,
        task: str,
        *,
        llm: Any,
        memory: Any,
        timeout: float = 300,
        session_id: str | None = None,
        task_id: str = "unknown",
        action_filter: list[str] | None = None,
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute the full agent pipeline.

        All components (agent, loop, env, llm, memory) are injected
        by the caller. AgentRuntime does not instantiate any component.

        Args:
            agent: Agent instance (must satisfy loop's required Protocol).
            loop: AgentLoop instance (e.g. ReactLoop).
            env: Environment instance (EnvironmentBase subclass).
            task: Task description string.
            llm: BaseChatModel instance.
            memory: BaseMemoryManager instance.
            timeout: Maximum execution time in seconds.
            session_id: Optional session/tab routing.
            task_id: Task identifier for result tracking.
            action_filter: Optional action name allowlist for ablation.
            config: Optional config snapshot for hooks.
            **kwargs: Passed through to Loop.run().

        Returns:
            AgentResult from the Loop.
        """
        # Step 1: Compatibility validation
        validate_compatibility(agent, loop)

        # Step 2: Observation three-party negotiation
        negotiated_obs = self._negotiate_observation(agent, loop, env)

        # Step 3: AOP weaving (must happen before guarded_act so
        #          act_fn captures wrapped_env.act, enabling AOP intercepts)
        weaver = Weaver(self._hooks)
        wrapped_agent = weaver.wrap(agent, role="agent")
        wrapped_env = weaver.wrap(env, role="env")
        wrapped_llm = weaver.wrap(llm, role="llm")

        # Step 4: Action negotiation + guarded_act (uses wrapped env)
        all_descriptors = env.get_action_space()
        active_descriptors, act_fn = build_guarded_act(
            wrapped_env.act, all_descriptors, action_filter,
        )

        # Step 5: Hook lifecycle — run_start
        await self._dispatch_lifecycle(
            "on_run_start",
            agent=agent, env=env, task=task, config=config or {},
        )

        # Step 6: Drive Loop with timeout
        try:
            result = await asyncio.wait_for(
                loop.run(
                    wrapped_agent,
                    wrapped_env,
                    task,
                    llm=wrapped_llm,
                    memory=memory,
                    act_fn=act_fn,
                    negotiated_obs=negotiated_obs,
                    active_descriptors=active_descriptors,
                    session_id=session_id,
                    task_id=task_id,
                    **kwargs,
                ),
                timeout=timeout,
            )

            # Step 7a: Success — notify hooks
            await self._dispatch_lifecycle("on_run_end", result=result)
            return result

        except asyncio.TimeoutError:
            # Step 7b: Timeout — build timeout result, notify hooks
            logger.warning("AgentRuntime: execution timed out after %.1fs", timeout)
            from datetime import datetime, timezone

            from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
            from spectragent.schemas.agent_result import (
                AgentMetrics,
                AgentResult,
                AgentUsage,
            )

            timeout_result = AgentResult(
                task_id=task_id,
                task=task,
                timestamp=datetime.now(timezone.utc),
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
                "on_run_error", error=asyncio.TimeoutError(), partial_steps=[],
            )
            return timeout_result

        except Exception as exc:
            # Step 7c: Error — notify hooks, re-raise
            logger.exception("AgentRuntime: execution failed")
            await self._dispatch_lifecycle(
                "on_run_error", error=exc, partial_steps=[],
            )
            raise

    # ── Private helpers ───────────────────────────────────────

    def _negotiate_observation(
        self, agent: Any, loop: Any, env: Any,
    ) -> Any:
        """Run three-party observation negotiation.

        Collects provides from Environment's capabilities and
        requires from Agent and Loop ``__wiring__`` declarations.
        """
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

    async def _dispatch_lifecycle(
        self, method_name: str, **kwargs: Any,
    ) -> None:
        """Dispatch a lifecycle callback to all hooks.

        Blocking hooks are awaited sequentially; non-blocking hooks
        are fired as background tasks. Blocking hook exceptions
        propagate; non-blocking hook exceptions are logged.
        """
        for hook in self._hooks:
            handler = getattr(hook, method_name, None)
            if handler is None:
                continue

            if hook.blocking:
                await handler(**kwargs)
            else:
                asyncio.create_task(
                    self._safe_lifecycle_call(hook, handler, method_name, kwargs)
                )

    @staticmethod
    async def _safe_lifecycle_call(
        hook: Any, handler: Any, method_name: str, kwargs: dict[str, Any],
    ) -> None:
        """Call a non-blocking hook lifecycle method, catching exceptions."""
        try:
            await handler(**kwargs)
        except Exception:
            hook_id = getattr(hook, "hook_id", type(hook).__name__)
            logger.exception(
                "Non-blocking hook '%s' failed on lifecycle '%s'",
                hook_id, method_name,
            )

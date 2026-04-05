"""AgentLoop Protocol — execution strategy contract.

The Loop drives the observe → think → act cycle. It calls Agent Protocol
methods for domain knowledge (prompt building, output parsing) and
Environment methods for interaction (observe, act).

Key design: Loop's ``run()`` has NO emitter/hook parameter. AOP hooks
are woven into agent/env/llm methods by the Weaver (M04) at runtime.
The Loop is completely unaware of hooks.

See docs/architecture_design.md §4.1 for the Agent-Loop separation design.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentLoop(Protocol):
    """Execution strategy contract.

    Implementations drive the agent loop (ReAct, CodeAct, FunctionCalling,
    PlanExecute). Each Loop declares its required Agent Protocol via
    ``__requires_protocol__`` on the base class.

    The ``run()`` method receives pre-wrapped agent/env/llm objects —
    AOP hooks fire automatically when Protocol methods are called.
    """

    loop_id: str

    async def run(
        self,
        agent: Any,
        env: Any,
        task: str,
        **kwargs: Any,
    ) -> Any:
        """Drive the full agent loop.

        Args:
            agent: Agent instance satisfying this Loop's required Protocol.
            env: Environment instance (observe/act/get_action_space).
            task: The task description string.
            **kwargs: Additional context (llm, negotiated_obs, active_descriptors,
                      act_fn, max_steps, etc.) injected by AgentRuntime.

        Returns:
            AgentResult (or compatible dict/object).
        """
        ...

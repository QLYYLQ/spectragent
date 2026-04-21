"""AgentLoop Protocol — execution strategy contract.

The Loop drives the observe -> think -> act cycle. It calls Agent Protocol
methods for domain knowledge (prompt building, output parsing) and
Environment methods for interaction (observe, act).

Key design: Loop's ``_execute()`` has NO emitter/hook parameter. AOP hooks
are woven into agent/env/llm methods by the Weaver (M04) at runtime.
The Loop is completely unaware of hooks.

``weave()`` binds runtime deps to ``self.ctx`` and patches the Loop's
execution logic onto the agent instance as ``agent.run``, making the Loop
both an IoC container (controls the execution flow) and an AOP-style
enhancer (injects behaviour into the agent at runtime).

See docs/architecture_design.md §4.1 for the Agent-Loop separation design.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentLoop(Protocol):
    """Execution strategy contract.

    Implementations drive the agent loop (ReAct, CodeAct, FunctionCalling,
    PlanExecute). Each Loop declares its required agent method slots via
    ``__required_slots__`` / ``__optional_slots__`` on the base class.

    ``weave()`` validates slots, binds deps to ``self.ctx``, and patches
    ``agent.run`` with a closure that drives ``_execute()``.
    """

    loop_id: str
    ctx: dict[str, Any]

    def weave(
        self,
        agent: Any,
        **deps: Any,
    ) -> Any:
        """Validate slots, bind deps to ``self.ctx``, patch ``agent.run``.

        Args:
            agent: Agent instance whose method slots are checked against
                   ``__required_slots__``.
            **deps: Runtime dependencies (env, llm, memory, act_fn, etc.)
                    bound to ``self.ctx`` for ``_execute`` to read.

        Returns:
            The same agent instance (with ``run`` patched).

        Raises:
            IncompatibleError: If the agent is missing required slots.
        """
        ...

    async def _execute(
        self,
        agent: Any,
        task: str,
        **kwargs: Any,
    ) -> Any:
        """Drive the full agent loop (internal — called via ``agent.run``).

        Runtime deps are on ``self.ctx`` (bound at weave time).
        ``**kwargs`` receives only per-run overrides from ``agent.run(task, ...)``.

        Args:
            agent: Agent instance satisfying this Loop's required slots.
            task: The task description string.
            **kwargs: Per-run overrides (rare).

        Returns:
            AgentResult (or compatible dict/object).
        """
        ...

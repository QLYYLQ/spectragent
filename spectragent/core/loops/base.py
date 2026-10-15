"""AgentLoop Protocol — execution strategy contract.

The Loop drives the observe -> think -> act cycle. It calls Agent Protocol
methods for domain knowledge (prompt building, output parsing) and
Environment methods for interaction (observe, act).

Key design: Loop's ``_execute()`` has NO emitter/hook parameter. AOP hooks
are woven into agent/env/llm methods by the Weaver (M04) at runtime.
The Loop is completely unaware of hooks.

``weave()`` captures runtime deps in a closure and patches the Loop's
execution logic onto the agent instance as ``agent.run``, making the Loop
both an IoC container (controls the execution flow) and an AOP-style
enhancer (injects behaviour into the agent at runtime). It stores
nothing on the Loop, so one Loop instance can drive many agents.

See docs/architecture_design.md §4.1 for the Agent-Loop separation design.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Protocol, runtime_checkable

from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.loops.limits import ExecutionLimits


@runtime_checkable
class AgentLoop(Protocol):
    """Execution strategy contract.

    Implementations drive the agent loop (ReAct, CodeAct, FunctionCalling,
    PlanExecute). Each Loop declares its required agent method slots via
    ``__required_slots__`` / ``__optional_slots__`` on the base class.

    ``weave()`` validates slots, captures deps in a closure, and patches
    ``agent.run`` with that closure, which drives ``_execute()``.
    """

    loop_id: str
    id: str | None
    __required_slots__: ClassVar[set[str]]
    __optional_slots__: ClassVar[set[str]]
    __required_message_protocols__: ClassVar[frozenset[MessageProtocol]]
    __requires_env__: ClassVar[bool]
    __loop_fragments__: ClassVar[frozenset[str]]

    def validate_slots(self, agent: Any) -> None:
        """Refuse an Agent missing one of this Loop's declared methods."""
        ...

    def check_loop_inputs(self, inputs: Mapping[str, Any]) -> None:
        """Refuse unknown, missing or mistyped task inputs."""
        ...

    def execution_limits(self) -> ExecutionLimits:
        """Declare the typed execution limits supplied to the Agent."""
        ...

    def weave(
        self,
        agent: Any,
        **deps: Any,
    ) -> Any:
        """Validate slots, capture deps in a closure, patch ``agent.run``.

        Args:
            agent: Agent instance whose method slots are checked against
                   ``__required_slots__``.
            **deps: Runtime dependencies (env, llm, memory, act_fn, etc.)
                    handed to ``_execute`` as ``ctx`` on every call.

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
        *,
        ctx: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        """Drive the full agent loop (internal — called via ``agent.run``).

        Args:
            agent: Agent instance satisfying this Loop's required slots.
            task: The task description string.
            ctx: Runtime deps captured at weave time. Passed per call so
                the Loop instance stays stateless.
            **kwargs: Per-run overrides (rare).

        Returns:
            AgentResult (or compatible dict/object).
        """
        ...

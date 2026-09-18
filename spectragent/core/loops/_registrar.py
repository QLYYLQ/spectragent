"""AgentLoop registrar — conscribe-driven auto-registration.

Each concrete Loop subclass declares ``__required_slots__`` and
``__optional_slots__`` to specify which agent methods it needs. The
base class provides a universal ``_validate_slots`` check and a
``weave`` method that patches ``agent.run`` with the Loop's execution
logic.

``weave()`` is stateless: runtime dependencies (env, llm, memory,
act_fn, …) are captured in the ``agent.run`` closure, never stored on
the loop instance. One loop instance can therefore be woven onto N
agents without the Nth weave rewriting the 1st agent's environment.

See docs/execution_dag.md M03 for the registrar design.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

from conscribe import create_registrar
from pydantic import Field

from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.loops.base import AgentLoop
from spectragent.core.loops.validation import IncompatibleError

logger = logging.getLogger(__name__)

AgentLoopRegistrar = create_registrar(
    "agent_loop",
    AgentLoop,
    discriminator_field="loop_id",
)


@dataclass
class AgentLoopBase(metaclass=AgentLoopRegistrar.Meta):
    """Concrete base for all Loop implementations.

    Subclasses must set:
    - ``__registry_key__``: conscribe key (e.g. ``"react"``, ``"codeact"``)
    - ``__required_slots__``: set of agent method names that MUST exist
    - ``__optional_slots__``: set of agent method names used if present

    Optionally:
    - ``__requires_env__``: whether this loop needs an Environment (default True)

    ``weave()`` hands runtime deps to ``_execute`` as the ``ctx``
    parameter. Nothing is stored on the loop instance, so the same loop
    instance can drive several agents concurrently.

    Example::

        class ReactLoop(AgentLoopBase):
            __registry_key__ = "react"
            __required_slots__ = {"build_react_prompt", "parse_react_output"}
            __optional_slots__ = {"get_react_output_model", "prepare"}
            __requires_env__ = True

            async def _execute(self, agent, task, *, ctx):
                env = ctx["env"]
                llm = ctx["llm"]
                ...
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __required_slots__: ClassVar[set[str]] = set()
    __optional_slots__: ClassVar[set[str]] = set()
    __required_message_protocols__: ClassVar[frozenset[MessageProtocol]] = frozenset()
    __requires_env__: ClassVar[bool] = True

    id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Instance identity. Unique across the whole config; "
                "defaults to '<loop_id>#<ordinal>' when omitted."
            ),
        ),
    ] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    @property
    def loop_id(self) -> str:
        return self.__registry_key__

    # ── slot validation (shared by all loops) ────────────────

    def _validate_slots(self, agent: Any) -> None:
        """Check that *agent* has all required callable slots.

        Raises:
            IncompatibleError: with the list of missing method names.
        """
        missing = sorted(
            s for s in self.__required_slots__ if not callable(getattr(agent, s, None))
        )
        if missing:
            agent_name = getattr(agent, "agent_name", type(agent).__name__)
            raise IncompatibleError(
                agent_name=agent_name,
                loop_id=self.__registry_key__,
                missing_slots=missing,
            )

    # ── weave: bind deps + patch agent.run ───────────────────

    def weave(self, agent: Any, **deps: Any) -> Any:
        """Validate slots, capture deps in a closure, patch ``agent.run``.

        **Stateless on purpose**: *deps* live only in the ``run`` closure
        built here. Weaving the same loop instance onto a second agent
        cannot retroactively rewrite the first agent's env/llm/memory.

        Returns the same *agent* instance (instance-level patch).
        """
        self._validate_slots(agent)

        ctx = dict(deps)
        loop = self

        async def run(task: str, **kw: Any) -> Any:
            return await loop._execute(agent, task, ctx=ctx, **kw)

        agent.run = run  # instance-level monkey-patch
        return agent

    # ── subclass hook ────────────────────────────────────────

    async def _execute(
        self,
        agent: Any,
        task: str,
        *,
        ctx: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        """Drive the full agent loop. Subclasses MUST override.

        Args:
            agent: Agent instance satisfying this loop's required slots.
            task: The task description string.
            ctx: Runtime deps captured at weave time (env, llm, memory,
                act_fn, negotiated_obs, …). Passed per call, never stored.
            **kwargs: Per-run overrides from ``agent.run(task, ...)``.
        """
        raise NotImplementedError

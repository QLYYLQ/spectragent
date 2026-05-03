"""AgentLoop registrar — conscribe-driven auto-registration.

Each concrete Loop subclass declares ``__required_slots__`` and
``__optional_slots__`` to specify which agent methods it needs. The
base class provides a universal ``_validate_slots`` check and a
``weave`` method that patches ``agent.run`` with the Loop's execution
logic.

At weave time, runtime dependencies (env, llm, memory, act_fn, etc.)
are bound to the loop instance as ``self.ctx``. The loop stays
per-instance — multi-loop uses ``copy.deepcopy`` to avoid conflicts.

See docs/execution_dag.md M03 for the registrar design.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar

from conscribe import create_registrar

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

    After ``weave()``, runtime deps are accessible via ``self.ctx``
    (a plain dict). Subclass ``_execute`` reads ``self.ctx["llm"]``,
    ``self.ctx["env"]``, etc. — no kwargs unpacking needed.

    Example::

        class ReactLoop(AgentLoopBase):
            __registry_key__ = "react"
            __required_slots__ = {"build_react_prompt", "parse_react_output"}
            __optional_slots__ = {"get_react_output_model", "prepare"}
            __requires_env__ = True

            async def _execute(self, agent, task):
                env = self.ctx["env"]
                llm = self.ctx["llm"]
                ...
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __required_slots__: ClassVar[set[str]] = set()
    __optional_slots__: ClassVar[set[str]] = set()
    __required_message_protocols__: ClassVar[frozenset[MessageProtocol]] = frozenset()
    __requires_env__: ClassVar[bool] = True

    # Runtime deps bound at weave time. Empty until weave() is called.
    ctx: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

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
            s for s in self.__required_slots__
            if not callable(getattr(agent, s, None))
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
        """Validate slots, bind deps to ``self.ctx``, patch ``agent.run``.

        After weave, ``self.ctx`` holds all runtime deps (env, llm,
        memory, act_fn, etc.) and ``agent.run(task)`` drives the loop.

        Returns the same *agent* instance (instance-level patch).
        """
        self._validate_slots(agent)

        # Bind deps to loop instance — _execute reads via self.ctx
        self.ctx = deps
        loop = self

        async def run(task: str, **kw: Any) -> Any:
            return await loop._execute(agent, task, **kw)

        agent.run = run  # instance-level monkey-patch
        return agent

    # ── subclass hook ────────────────────────────────────────

    async def _execute(
        self,
        agent: Any,
        task: str,
        **kwargs: Any,
    ) -> Any:
        """Drive the full agent loop. Subclasses MUST override.

        Runtime deps are on ``self.ctx`` (bound at weave time).
        ``**kwargs`` receives only per-run overrides from ``agent.run(task, ...)``.
        """
        raise NotImplementedError

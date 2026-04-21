"""Agent registry using conscribe.

M12: AgentBase declares ``__wiring__`` for cross-registry dependencies.
Default wiring uses Mode 1 (auto-discovery) for agent_loop and llm,
allowing all registered values. Subclasses narrow via MRO merge.

See docs/architecture_design.md §4.1 and docs/execution_dag.md M12.
"""

from typing import Any, ClassVar

from conscribe import create_registrar

from spectragent.agents.base import BaseAgent

AgentRegistrar = create_registrar(
    "agent",
    BaseAgent,
    discriminator_field="agent_id",
)


class AgentBase(metaclass=AgentRegistrar.Meta):
    """Concrete base for all agent implementations.

    Subclasses auto-register via conscribe's AutoRegistrar metaclass.
    Set ``__registry_key__ = "key"`` to specify the registration key.

    ``__wiring__`` declares cross-registry dependencies:
    - ``agent_loop``: compatible Loop strategies (default: all)
    - ``llm``: compatible LLM providers (default: all)

    Subclasses can narrow wiring; conscribe MRO merge handles inheritance.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": "agent_loop",  # Mode 1: auto-discovery, allows all loops
        "llm": "llm",  # Mode 1: auto-discovery, allows all LLMs
        "memory": "memory",  # Mode 1: auto-discovery, allows all memories
    }

    @property
    def agent_id(self) -> str:
        """Return the conscribe registry key as agent_id."""
        return self.__registry_key__

    @property
    def domain(self) -> str:
        """Domain identifier. Override in subclasses for specific domains."""
        return "general"

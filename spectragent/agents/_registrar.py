"""Agent registry using conscribe."""

from typing import ClassVar

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
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def agent_id(self) -> str:
        """Return the conscribe registry key as agent_id."""
        return self.__registry_key__

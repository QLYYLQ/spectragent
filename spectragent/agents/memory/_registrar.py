"""Memory strategy registry using conscribe."""

from typing import ClassVar

from conscribe import create_registrar

from spectragent.agents.memory.base import BaseMemoryManager

MemoryRegistrar = create_registrar(
    "memory",
    BaseMemoryManager,
    discriminator_field="memory_id",
)


class MemoryBase(metaclass=MemoryRegistrar.Meta):
    """Concrete base for all memory strategy implementations.

    Subclasses auto-register via conscribe's AutoRegistrar metaclass.
    Set ``__registry_key__ = "key"`` to specify the registration key.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def memory_id(self) -> str:
        """Return the conscribe registry key as memory_id."""
        return self.__registry_key__

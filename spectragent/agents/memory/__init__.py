"""Agent memory subsystem — pluggable memory strategies via conscribe."""

from spectragent.agents.memory._registrar import MemoryBase, MemoryRegistrar
from spectragent.agents.memory.base import BaseMemoryManager
from spectragent.agents.memory.protocols import ALL_PROTOCOLS, MessageProtocol

__all__ = [
    "ALL_PROTOCOLS",
    "BaseMemoryManager",
    "MemoryBase",
    "MemoryRegistrar",
    "MessageProtocol",
]

"""Agent memory subsystem — pluggable memory strategies via conscribe."""

from spectragent.agents.memory._registrar import MemoryBase, MemoryRegistrar
from spectragent.agents.memory.base import BaseMemoryManager

__all__ = [
    "BaseMemoryManager",
    "MemoryBase",
    "MemoryRegistrar",
]

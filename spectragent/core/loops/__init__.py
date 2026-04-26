"""AgentLoop registry — slot-based weaving architecture.

Separates Agent (domain knowledge) from Loop (execution strategy) into
two independent registries. Each Loop declares ``__required_slots__`` /
``__optional_slots__`` to specify which agent methods it needs, and
``weave()`` patches ``agent.run`` with the Loop's execution logic.

See docs/architecture_design.md §4.1 and docs/execution_dag.md M03/M09.
"""

from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar
from spectragent.core.loops.base import AgentLoop
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.react import ReactLoop
from spectragent.core.loops.validation import (
    IncompatibleError,
    validate_compatibility,
)

__all__ = [
    "AgentLoop",
    "AgentLoopRegistrar",
    "AgentLoopBase",
    "ReactLoop",
    "ParseError",
    "validate_compatibility",
    "IncompatibleError",
]

"""AgentLoop registry + compatibility Protocols — M03.

Separates Agent (domain knowledge) from Loop (execution strategy) into
two independent registries. Each Loop declares which Protocol it requires
from Agents via ``__requires_protocol__``.

See docs/architecture_design.md §4.1 and docs/execution_dag.md M03.
"""

from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar
from spectragent.core.loops.base import AgentLoop
from spectragent.core.loops.compatibility import (
    CodeActCompatible,
    FuncCallCompatible,
    PlanExecCompatible,
    ReactCompatible,
)
from spectragent.core.loops.validation import (
    IncompatibleError,
    validate_compatibility,
)

__all__ = [
    "AgentLoop",
    "AgentLoopRegistrar",
    "AgentLoopBase",
    "ReactCompatible",
    "CodeActCompatible",
    "FuncCallCompatible",
    "PlanExecCompatible",
    "validate_compatibility",
    "IncompatibleError",
]

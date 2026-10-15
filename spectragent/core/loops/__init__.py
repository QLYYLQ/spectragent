"""AgentLoop registry — slot-based weaving architecture.

Separates Agent (domain knowledge) from Loop (execution strategy) into
two independent registries. Each Loop declares ``__required_slots__`` /
``__optional_slots__`` to specify which agent methods it needs (and
``__loop_inputs__`` for the per-task inputs it accepts), and ``weave()``
patches ``agent.run`` with the Loop's execution logic. ``ReactLoop`` is the
ReAct strategy; ``BranchLoop`` resumes a recorded run at a checkpoint.

See docs/architecture_design.md §4.1 and docs/execution_dag.md M03/M09.
"""

from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar, LoopInput
from spectragent.core.loops.base import AgentLoop
from spectragent.core.loops.branch import BranchLoop
from spectragent.core.loops.errors import (
    ParseError,
    RunAbort,
    RunEnded,
    UnrepresentableActionError,
    carried_outcome,
    carry_outcome,
)
from spectragent.core.loops.react import ReactLoop
from spectragent.core.loops.run import LoopRun
from spectragent.core.loops.validation import (
    IncompatibleCheckpointError,
    IncompatibleError,
    IncompatibleLoopInputError,
    MissingEnvironmentError,
)

__all__ = [
    "AgentLoop",
    "AgentLoopRegistrar",
    "AgentLoopBase",
    "BranchLoop",
    "LoopInput",
    "LoopRun",
    "ReactLoop",
    "ParseError",
    "RunAbort",
    "RunEnded",
    "UnrepresentableActionError",
    "carry_outcome",
    "carried_outcome",
    "IncompatibleCheckpointError",
    "IncompatibleError",
    "IncompatibleLoopInputError",
    "MissingEnvironmentError",
]

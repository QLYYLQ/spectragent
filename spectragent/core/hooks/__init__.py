"""Hook registry + lifecycle events — M04 + M11.

Hooks register via conscribe and declare method interceptions via
``__intercepts__``. The AOP Weaver (core/aop/) handles runtime weaving.

Lifecycle events (run_start/end/error) are called directly by AgentRuntime,
not through method interception.

TraceWriter collects execution facts and persists the selected recording scope.

See docs/architecture_design.md §4.6 and docs/execution_dag.md M04/M11.
"""

from spectragent.core.hooks._registrar import HookBase, HookRegistrar
from spectragent.core.hooks.base import RuntimeHook
from spectragent.core.hooks.trace_writer import TraceWriter

__all__ = [
    "RuntimeHook",
    "HookRegistrar",
    "HookBase",
    "TraceWriter",
]

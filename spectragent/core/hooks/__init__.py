"""Hook registry + lifecycle events — M04.

Hooks register via conscribe and declare method interceptions via
``__intercepts__``. The AOP Weaver (core/aop/) handles runtime weaving.

Lifecycle events (run_start/end/error) are called directly by AgentRuntime,
not through method interception.

See docs/architecture_design.md §4.6 and docs/execution_dag.md M04.
"""

from spectragent.core.hooks._registrar import HookBase, HookRegistrar
from spectragent.core.hooks.base import RuntimeHook
from spectragent.core.hooks.events import LifecycleEvents

__all__ = [
    "RuntimeHook",
    "HookRegistrar",
    "HookBase",
    "LifecycleEvents",
]

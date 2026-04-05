"""Capability registry — M06.

Capability = ActionGroup + Resolver. Each capability declares and
executes actions within its scope, and provides observation fragments
on demand.

Registration is import-driven. Use ``discover("spectragent.core.capabilities")``
to scan and register all concrete Capability implementations.

See docs/architecture_design.md §4.6 and docs/execution_dag.md M06.
"""

from spectragent.core.capabilities._registrar import CapabilityBase, CapabilityRegistrar
from spectragent.core.capabilities.base import BaseCapability

__all__ = [
    "BaseCapability",
    "CapabilityRegistrar",
    "CapabilityBase",
]

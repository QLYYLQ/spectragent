"""Capability registry — M06.

Capability = ActionGroup + Resolver. Each capability declares and
executes actions within its scope, and provides observation fragments
on demand.

Every subpackage of this package is one capability, named by its
``capability_id``, and declares its strategy axes, task setup and
optional-extra modules in its own ``declarations.py``, which
``core/registries.py`` reads. Registration is import-driven: a run imports
only the capability packages its configuration names
(``core.registries.discover_run_implementations``), and every module that
needs an optional extra is skipped.

See docs/architecture_design.md §4.2.1, §4.9 and docs/execution_dag.md M06.
"""

from spectragent.core.capabilities._registrar import CapabilityBase, CapabilityRegistrar
from spectragent.core.capabilities.base import BaseCapability

__all__ = [
    "BaseCapability",
    "CapabilityRegistrar",
    "CapabilityBase",
]

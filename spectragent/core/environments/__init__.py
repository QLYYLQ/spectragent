"""Environment registry — Capability combiner and action router.

Environments aggregate action spaces from injected Capabilities,
route actions via prefix dispatch (for multi-capability scenarios),
and delegate observation resolution to Capability resolvers.

NOTE: This module does NOT call ``discover()`` on import.
Use ``discover("spectragent.core.environments")`` explicitly at
runtime entry points when concrete Environment subclasses need
to be registered.

See docs/architecture_design.md §4.3 and docs/execution_dag.md M07.
"""

from spectragent.core.environments._registrar import EnvironmentBase, EnvironmentRegistrar
from spectragent.core.environments.base import Environment
from spectragent.core.environments.errors import (
    ObservationFailedError,
    UnroutableFragmentError,
)

__all__ = [
    "Environment",
    "EnvironmentRegistrar",
    "EnvironmentBase",
    "ObservationFailedError",
    "UnroutableFragmentError",
]

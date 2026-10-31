"""Environment registry — Capability combiner and action router.

Environments aggregate action spaces from injected Capabilities,
route actions via prefix dispatch (for multi-capability scenarios),
and delegate observation resolution to Capability resolvers.

No concrete environment lives here: a single-capability environment is
in its capability's package and registers when a run imports it
(``docs/architecture_design.md`` §4.2.1, §4.3).

See docs/architecture_design.md §4.3 and docs/execution_dag.md M07.
"""

from spectragent.core.environments._registrar import EnvironmentBase, EnvironmentRegistrar
from spectragent.core.environments.base import Environment
from spectragent.core.environments.errors import (
    ObservationFailedError,
    UnroutableArchiverError,
    UnroutableFragmentError,
)

__all__ = [
    "Environment",
    "EnvironmentRegistrar",
    "EnvironmentBase",
    "ObservationFailedError",
    "UnroutableArchiverError",
    "UnroutableFragmentError",
]

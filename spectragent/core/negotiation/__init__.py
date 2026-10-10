"""Unified negotiation module.

Hosts the shared negotiation engine, result types, and wiring-extraction
helper used by the observation (three-party), action (two-party) and
archive (consumer ↔ environment) negotiation flows. See
docs/architecture_design.md §4.4 and §4.5.
"""

from spectragent.core.negotiation._base import (
    AmbiguousKeyError,
    DemandOutsideWiringError,
    bounded_demand,
    demand_label,
    demand_of,
    extract_wiring,
)
from spectragent.core.negotiation._registrar import (
    NegotiationResult,
    NegotiationResultBase,
    NegotiationResultRegistrar,
)
from spectragent.core.negotiation.action import (
    ActionNegotiationResult,
    AmbiguousActionError,
    IncompatibleActionError,
    negotiate_action,
)
from spectragent.core.negotiation.archive import (
    ArchiveNegotiationResult,
    IncompatibleArchiveError,
    negotiate_archive,
)
from spectragent.core.negotiation.engine import negotiate
from spectragent.core.negotiation.memory import (
    IncompatibleMemoryError,
    negotiate_memory,
)
from spectragent.core.negotiation.observation import (
    AmbiguousFragmentError,
    IncompatibleFragmentError,
    ObservationNegotiationResult,
    negotiate_observation,
)

__all__ = [
    "ActionNegotiationResult",
    "AmbiguousActionError",
    "AmbiguousFragmentError",
    "AmbiguousKeyError",
    "ArchiveNegotiationResult",
    "DemandOutsideWiringError",
    "IncompatibleActionError",
    "IncompatibleArchiveError",
    "IncompatibleFragmentError",
    "IncompatibleMemoryError",
    "NegotiationResult",
    "NegotiationResultBase",
    "NegotiationResultRegistrar",
    "ObservationNegotiationResult",
    "bounded_demand",
    "demand_label",
    "demand_of",
    "extract_wiring",
    "negotiate",
    "negotiate_action",
    "negotiate_archive",
    "negotiate_memory",
    "negotiate_observation",
]

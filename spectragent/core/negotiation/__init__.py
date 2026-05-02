"""Unified negotiation module.

Hosts the shared negotiation engine, result types, and wiring-extraction
helper used by both observation (three-party) and action (two-party)
negotiation flows. See docs/architecture_design.md §4.4 and §4.5.
"""

from spectragent.core.negotiation._base import extract_wiring
from spectragent.core.negotiation._registrar import (
    NegotiationResult,
    NegotiationResultBase,
    NegotiationResultRegistrar,
)
from spectragent.core.negotiation.action import (
    ActionNegotiationResult,
    IncompatibleActionError,
    negotiate_action,
)
from spectragent.core.negotiation.engine import negotiate
from spectragent.core.negotiation.memory import (
    IncompatibleMemoryError,
    negotiate_memory,
)
from spectragent.core.negotiation.observation import (
    IncompatibleFragmentError,
    ObservationNegotiationResult,
    negotiate_observation,
)

__all__ = [
    "ActionNegotiationResult",
    "IncompatibleActionError",
    "IncompatibleFragmentError",
    "IncompatibleMemoryError",
    "NegotiationResult",
    "NegotiationResultBase",
    "NegotiationResultRegistrar",
    "ObservationNegotiationResult",
    "extract_wiring",
    "negotiate",
    "negotiate_action",
    "negotiate_memory",
    "negotiate_observation",
]

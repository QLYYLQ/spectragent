"""Observation Fragment system — M02.

Breaks the monolithic Observation into independently registered Fragments
with a 3-party negotiation engine (Capability provides / Agent requires /
Loop requires). The negotiation engine itself lives in
:mod:`spectragent.core.negotiation`.

See docs/architecture_design.md §4.4 and docs/execution_dag.md M02 for design details.
"""

from spectragent.schemas.observation._registrar import (
    FragmentBase,
    ObservationRegistrar,
)
from spectragent.schemas.observation.base import ObservationFragment
from spectragent.schemas.observation.dynamic_model import build_observation_model

__all__ = [
    "FragmentBase",
    "ObservationFragment",
    "ObservationRegistrar",
    "build_observation_model",
]

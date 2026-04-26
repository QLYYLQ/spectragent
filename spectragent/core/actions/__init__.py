"""Action registry — M13-Rev.

Each Action is an independently registered conscribe unit, symmetric to
ObservationFragment. Agents declare required/optional actions via
``__wiring__``; Capabilities declare provided actions the same way.
The negotiation engine itself lives in :mod:`spectragent.core.negotiation`.

See docs/architecture_design.md §4.5 and docs/design_discussion.md §3.3.
"""

from spectragent.core.actions._registrar import ActionBase, ActionRegistrar
from spectragent.core.actions.base import BaseAction

__all__ = [
    "ActionBase",
    "ActionRegistrar",
    "BaseAction",
]

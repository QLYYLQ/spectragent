"""Action registry — M13-Rev.

Each Action is an independently registered conscribe unit, symmetric to
ObservationFragment. Agents declare required/optional actions via
``__wiring__``; Capabilities declare provided actions the same way;
AgentRuntime negotiates an active set that feeds ``build_guarded_act``.

See docs/architecture_design.md §4.5 and docs/design_discussion.md §3.3.
"""

from spectragent.core.actions._registrar import ActionBase, ActionRegistrar
from spectragent.core.actions.base import BaseAction
from spectragent.core.actions.negotiation import (
    ActionNegotiationResult,
    IncompatibleActionError,
    extract_action_wiring,
    negotiate_actions,
)

__all__ = [
    "ActionBase",
    "ActionNegotiationResult",
    "ActionRegistrar",
    "BaseAction",
    "IncompatibleActionError",
    "extract_action_wiring",
    "negotiate_actions",
]

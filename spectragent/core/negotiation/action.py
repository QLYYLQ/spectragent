"""Two-party action negotiation — Agent ↔ Environment.

Symmetric to :mod:`spectragent.core.negotiation.observation` but without
loop participation: actions are always env-sourced, so only Agent wiring
is extracted.

See docs/architecture_design.md §4.5 for the full design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spectragent.core.negotiation._registrar import NegotiationResultBase
from spectragent.core.negotiation.engine import negotiate


class IncompatibleActionError(Exception):
    """Raised when required actions cannot be satisfied by the environment.

    The most common cause is an *ablated* capability: a YAML ``actions:``
    list narrows which actions the capability instantiates, and the agent
    requires one that was left out. That is the intended behaviour — an
    ablation that removes a required action must fail at negotiation, not
    silently advertise a smaller toolset to the model — so the message
    names the narrowing explicitly.
    """

    def __init__(self, required: set[str], provided: set[str]) -> None:
        missing = required - provided
        super().__init__(
            f"Required actions not provided by environment. "
            f"Missing: {sorted(missing)}. "
            f"Environment provides: {sorted(provided)}. "
            f"Required: {sorted(required)}. "
            f"If a capability declares an ``actions:`` list in YAML, that "
            f"list is an ablation — only the actions it names are "
            f"instantiated. Add the missing action(s) to that list, or "
            f"drop the list entirely to provide everything the "
            f"capability's __wiring__ offers."
        )
        self.missing = missing
        self.provided = provided
        self.required = required


@dataclass(frozen=True)
class ActionNegotiationResult(NegotiationResultBase):
    """Output of the two-party action negotiation.

    Attributes:
        active_actions: Action names activated for this run.
    """

    __registry_key__ = "action"

    active_actions: frozenset[str]


def _action_rules(
    *,
    agent: tuple[set[str], set[str]],
    env_provides: set[str],
) -> ActionNegotiationResult:
    """Two-party action negotiation semantics."""
    agent_required, agent_optional = agent
    missing = agent_required - env_provides
    if missing:
        raise IncompatibleActionError(
            required=agent_required,
            provided=env_provides,
        )
    active = agent_required | (agent_optional & env_provides)
    return ActionNegotiationResult(active_actions=frozenset(active))


def negotiate_action(agent: Any, env: Any) -> ActionNegotiationResult:
    """Run action negotiation for an (agent, env) pair."""
    return negotiate(
        ActionNegotiationResult,
        "action",
        _action_rules,
        agent=type(agent),
        env_provides=set(env.provided_actions),
    )

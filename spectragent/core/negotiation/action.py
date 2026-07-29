"""Two-party action negotiation — Agent ↔ Environment.

Symmetric to :mod:`spectragent.core.negotiation.observation` but without
loop participation: actions are always env-sourced, so only Agent wiring
is extracted.

See docs/architecture_design.md §4.5 for the full design.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from spectragent.core.actions._registrar import ACTION_KEY_SEPARATOR
from spectragent.core.negotiation._base import (
    AmbiguousKeyError,
    bind_short_names,
    group_by_short_name,
    resolve_demand,
)
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

    ``missing`` carries the keys **as the agent declared them**, which for
    a capability-relative declaration is the short name. That is the
    string the user has to go and edit; reporting a fully-qualified key
    they never wrote would point at the wrong line.
    """

    def __init__(self, missing: set[str], provided: set[str]) -> None:
        super().__init__(
            f"Required actions not provided by environment. "
            f"Missing: {sorted(missing)}. "
            f"Environment provides: {sorted(provided)}. "
            f"If a capability declares an ``actions:`` list in YAML, that "
            f"list is an ablation — only the actions it names are "
            f"instantiated. Add the missing action(s) to that list, or "
            f"drop the list entirely to provide everything the "
            f"capability's __wiring__ offers."
        )
        self.missing = missing
        self.provided = provided


class AmbiguousActionError(AmbiguousKeyError):
    """A relative action key that two of this environment's capabilities offer."""

    kind = "action"


@dataclass(frozen=True)
class ActionNegotiationResult(NegotiationResultBase):
    """Output of the two-party action negotiation.

    Attributes:
        active_actions: Fully-qualified action keys activated for this
            run — what descriptors, routing and the trace all speak.
        action_bindings: Short name → fully-qualified key, for the active
            set. Built here, once, so that the one component allowed to
            accept a short name from a model (``build_guarded_act``) can
            look the answer up instead of working it out again.
    """

    __registry_key__ = "action"

    active_actions: frozenset[str]
    action_bindings: Mapping[str, str] = field(default_factory=dict)


def _action_rules(
    *,
    agent: tuple[set[str], set[str]],
    env_provides: set[str],
) -> ActionNegotiationResult:
    """Two-party action negotiation semantics.

    Runs in the agent's *declaration* space — which is capability-relative
    unless the agent pinned a provider — and resolves it against the
    environment's fully-qualified action keys. Everything the result
    hands downstream is fully qualified.
    """
    agent_required, agent_optional = agent
    demand = resolve_demand(
        required=agent_required,
        optional=agent_optional,
        universe=env_provides,
        separator=ACTION_KEY_SEPARATOR,
        ambiguity_error=AmbiguousActionError,
    )
    if demand.missing:
        raise IncompatibleActionError(
            missing=set(demand.missing),
            provided=env_provides,
        )
    active = demand.resolved
    return ActionNegotiationResult(
        active_actions=active,
        action_bindings=bind_short_names(
            group_by_short_name(active, ACTION_KEY_SEPARATOR)
        ),
    )


def negotiate_action(agent: Any, env: Any) -> ActionNegotiationResult:
    """Run action negotiation for an (agent, env) pair."""
    return negotiate(
        ActionNegotiationResult,
        "action",
        _action_rules,
        agent=type(agent),
        env_provides=set(env.provided_actions),
    )

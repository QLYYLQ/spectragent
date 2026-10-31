"""Three-party action negotiation — Capability and Loop provide, Agent requires.

Symmetric to :mod:`spectragent.core.negotiation.observation`. Two parties
provide: the environment's capabilities, and the Loop, which provides and
executes the actions that never direct the environment (``loop.done``, its
``__loop_actions__``). Only the agent requires.

See docs/architecture_design.md §4.5 for the full design.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from spectragent.core.actions._registrar import (
    ACTION_KEY_SEPARATOR,
    ActionRegistrar,
    is_loop_action,
)
from spectragent.core.negotiation._base import (
    AmbiguousKeyError,
    bind_short_names,
    demand_label,
    demand_of,
    group_by_short_name,
    requested_by,
    resolve_demand,
)
from spectragent.core.negotiation._registrar import NegotiationResultBase
from spectragent.core.negotiation.engine import negotiate

logger = logging.getLogger(__name__)


class IncompatibleActionError(Exception):
    """Raised when required actions cannot be satisfied by the run's providers.

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

    The requester is named for the same reason as in
    :class:`~spectragent.core.negotiation.observation.IncompatibleFragmentError`:
    an instance may narrow its own demand, so which actions are required
    is a property of the *configuration*, not of the agent class. So is the
    provider that failed: the environment, or the Loop (``loop 'react'``)
    when it does not provide a registered ``loop.*`` action the agent
    requires.
    """

    def __init__(
        self,
        missing: set[str],
        provided: set[str],
        requested_by: Mapping[str, Sequence[str]] | None = None,
        *,
        provider: str = "environment",
    ) -> None:
        requested_by = {
            key: tuple(sources) for key, sources in sorted((requested_by or {}).items())
        }
        attribution = "; ".join(
            f"{key} ← {' and '.join(sources)}" for key, sources in requested_by.items()
        )
        # The ablation hint is about a capability's ``actions:`` list, which
        # a Loop does not have.
        hint = (
            " If a capability declares an ``actions:`` list in YAML, that "
            "list is an ablation — only the actions it names are "
            "instantiated. Add the missing action(s) to that list, or "
            "drop the list entirely to provide everything the "
            "capability's __wiring__ offers."
            if provider == "environment"
            else ""
        )
        super().__init__(
            f"Required actions not provided by {provider}. "
            f"Missing: {sorted(missing)}. "
            + (f"Requested by: {attribution}. " if attribution else "")
            + f"{provider[:1].upper()}{provider[1:]} provides: {sorted(provided)}."
            + hint
        )
        self.missing = missing
        self.provided = provided
        self.requested_by = requested_by
        #: Who failed to provide: ``environment``, or the Loop
        #: (``loop 'react'``) when it does not provide a required loop action.
        self.provider = provider


class AmbiguousActionError(AmbiguousKeyError):
    """A relative action key that two of this run's providers offer."""

    kind = "action"


@dataclass(frozen=True)
class ActionNegotiationResult(NegotiationResultBase):
    """Output of the action negotiation.

    Attributes:
        active_actions: Fully-qualified action keys activated for this
            run — what descriptors, routing and the trace all speak. The
            ``loop.*`` ones are the Loop's to execute.
        action_bindings: Short name → fully-qualified key, for the active
            set. Built here, once, so that the one component allowed to
            accept a short name from a model (``build_guarded_act``) can
            look the answer up instead of working it out again.
    """

    __registry_key__ = "action"

    active_actions: frozenset[str]
    action_bindings: Mapping[str, str] = field(default_factory=dict)


def _registered_loop_actions() -> frozenset[str]:
    """Every registered ``loop.*`` action: the ones a Loop, never a capability, executes.

    No environment provides them. Which of them *this* run can execute is the
    selected Loop's declaration (:func:`loop_actions`).
    """
    return frozenset(key for key in ActionRegistrar.get_all() if is_loop_action(key))


def loop_actions(loop: Any) -> frozenset[str]:
    """The actions *loop* provides and executes: its class's ``__loop_actions__``.

    ``AgentLoopBase`` declares ``loop.done`` by default
    (``core/loops/actions.py``). An object that declares nothing — a
    structural Loop outside the base — provides nothing.
    """
    cls = loop if isinstance(loop, type) else type(loop)
    return frozenset(getattr(cls, "__loop_actions__", frozenset()))


def _action_rules(
    *,
    agent: tuple[set[str], set[str]],
    env_provides: set[str],
    agent_source: str = "",
    loop_source: str = "",
    loop_provides: frozenset[str] | None = None,
) -> ActionNegotiationResult:
    """Action negotiation semantics.

    Runs in the agent's *declaration* space — which is capability-relative
    unless the agent pinned a provider — and resolves it against this run's
    action universe: the environment's fully-qualified keys plus the
    ``loop.*`` actions the Loop provides (*loop_provides*, the selected
    Loop's ``__loop_actions__``; ``None`` — a caller of the bare rules that
    names no Loop — means every registered one). Everything the result
    hands downstream is fully qualified.

    A required key that resolves to a registered loop action the Loop does
    not provide is refused naming the action and the Loop, as a loop-source
    fragment the Loop does not build is (``_observation_rules``).

    ``agent_source`` / ``loop_source`` are labels for the diagnosis and
    nothing else; they never reach :func:`resolve_demand`.
    """
    agent_required, agent_optional = agent
    registered = _registered_loop_actions()
    loop_keys = registered if loop_provides is None else registered & loop_provides
    demand = resolve_demand(
        required=agent_required,
        optional=agent_optional,
        universe=set(env_provides) | loop_keys,
        separator=ACTION_KEY_SEPARATOR,
        ambiguity_error=AmbiguousActionError,
    )
    if demand.missing:
        parties = [(agent_source, agent_required)]
        # Missing because the Loop does not provide it — not the environment's.
        unprovided = resolve_demand(
            required=demand.missing,
            optional=(),
            universe=registered - loop_keys,
            separator=ACTION_KEY_SEPARATOR,
            ambiguity_error=AmbiguousActionError,
        ).bindings
        if unprovided:
            raise IncompatibleActionError(
                missing=set(unprovided),
                provided=set(loop_keys),
                requested_by=requested_by(unprovided, parties),
                provider=f"loop {loop_source!r}" if loop_source else "the loop",
            )
        raise IncompatibleActionError(
            missing=set(demand.missing),
            provided=env_provides,
            requested_by=requested_by(demand.missing, parties),
        )
    active = demand.resolved
    grouped = group_by_short_name(active, ACTION_KEY_SEPARATOR)
    # Both providers stay active and both stay routable — only the bare
    # form has no answer, so this is not an error. It is still worth
    # saying out loud: an agent whose parser emits short names (UI-TARS)
    # has no way to address either, and would be rejected once per step
    # with nothing in the run explaining why.
    for short, group in sorted(grouped.items()):
        if len(group) > 1:
            logger.warning(
                "Action negotiation: %r is claimed by %s, so the bare name "
                "binds to neither. Actions emitted as %r will be rejected; "
                "an agent that needs one must emit a fully-qualified key.",
                short,
                list(group),
                short,
            )
    return ActionNegotiationResult(
        active_actions=active,
        action_bindings=bind_short_names(grouped),
    )


def negotiate_action(agent: Any, loop: Any, env: Any) -> ActionNegotiationResult:
    """Run action negotiation for an (agent, loop, env) triple.

    Symmetric to observation: the agent's half comes from
    :func:`~spectragent.core.negotiation._base.demand_of`, so an instance
    narrowing inside its class's declared space is the same mechanism on
    both axes rather than a special case on one. The Loop is a provider
    only: the actions it executes itself (:func:`loop_actions`) are on offer
    beside the environment's. *env* is ``None`` in an env-less run and
    provides nothing, as on the observation axis.
    """
    return negotiate(
        ActionNegotiationResult,
        "action",
        _action_rules,
        agent=demand_of(agent, "action"),
        env_provides=set(env.provided_actions) if env is not None else set(),
        agent_source=demand_label(agent),
        loop_source=demand_label(loop),
        loop_provides=loop_actions(loop),
    )

"""Three-party observation negotiation — Capability / Agent / Loop.

Pure-data outputs with zero type coupling to concrete Agent/Loop classes.
The Avro-style rules live in ``_observation_rules``; ``negotiate_observation``
is a thin wrapper over the generic :func:`negotiate` engine.

See docs/architecture_design.md §4.4 for the full specification.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

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
from spectragent.schemas.observation._registrar import (
    FRAGMENT_KEY_SEPARATOR,
    ObservationRegistrar,
)


class IncompatibleFragmentError(Exception):
    """Raised when required fragments cannot be satisfied by the environment.

    Reports the *complete* missing set, matching
    :class:`~spectragent.core.negotiation.action.IncompatibleActionError`.
    Raising on the first miss made the user fix-and-rerun once per missing
    fragment, and — because the scan iterated an unordered ``set`` — the
    one they were told about varied between processes.

    Fragments are named as they were declared, so a capability-relative
    declaration is reported by its short name — the string in the wiring
    line the user has to edit.

    Each missing fragment is also attributed to the party that required
    it, by registry key — ``dom ← browser_use.dom`` — so a mistyped
    capability profile reads as the mistake it is instead of as a puzzle
    about whether the agent or the Loop wanted a DOM.
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
        super().__init__(
            f"Required observation fragments not provided by {provider}. "
            f"Missing: {sorted(missing)}. "
            + (f"Requested by: {attribution}. " if attribution else "")
            + f"{provider[:1].upper()}{provider[1:]} provides: {sorted(provided)}."
        )
        self.missing = missing
        self.provided = provided
        self.requested_by = requested_by
        #: Who failed to provide (M32): ``environment`` at run assembly, or
        #: the Loop (``loop 'react'``) when it does not build a required
        #: loop-source fragment; the training export names the source run
        #: whose recording lacks them.
        self.provider = provider


class AmbiguousFragmentError(AmbiguousKeyError):
    """A relative fragment key two of this environment's capabilities offer."""

    kind = "observation fragment"


class DuplicateFragmentNameError(Exception):
    """Raised when two active fragments collapse to the same local name.

    Fragment keys are fully qualified (``source.local_id``) but the
    generated observation model exposes only the local segment, so
    ``swe.terminal`` and ``docker.terminal`` would both want ``obs.terminal``.
    Detected here, at negotiation time, rather than on the first
    ``env.observe()`` call — a config error should surface before the run
    starts, not at step 0.
    """

    def __init__(self, collisions: dict[str, list[str]]) -> None:
        # Sort the payload too, not just the message — callers assert on
        # ``.collisions`` and frozenset iteration order is not stable.
        collisions = {name: sorted(keys) for name, keys in sorted(collisions.items())}
        detail = "; ".join(f"{name!r} ← {keys}" for name, keys in collisions.items())
        super().__init__(
            f"Active observation fragments collapse to the same local name: {detail}. "
            f"Cross-source local-id collisions are not supported."
        )
        self.collisions = collisions


@dataclass(frozen=True)
class ObservationNegotiationResult(NegotiationResultBase):
    """Output of the three-party observation negotiation.

    Attributes:
        active_fragments: All fragments activated for this run, fully
            qualified.
        env_sourced: Subset of active_fragments provided by Capability resolvers.
        loop_sourced: Subset of active_fragments filled by the Loop itself.
        fragment_bindings: Local name → fully-qualified key, for the
            active set. Computed once here so nothing downstream has to
            rediscover which capability a locally-named fragment came
            from.
        required_fragments: Subset of active_fragments the agent or loop
            declared **required**, fully qualified. Negotiation has always
            known this — it is the difference between "I cannot work
            without a DOM" and "use a screenshot if there is one" — and it
            used to end at the missing-key check and be thrown away. It is
            kept because it is the only thing that can answer the question
            a multi-capability observation asks at runtime: this
            capability just raised, is the run over, or is this a slot
            that records the failure and carries on
            (:class:`~spectragent.schemas.observation.failure.FragmentFailure`)?
            Without it every failure is fatal, which discards the
            fragments its peers already captured.

            Includes loop-sourced required keys. Consumers that care only
            about capability failures intersect with ``env_sourced``.
    """

    __registry_key__ = "observation"

    active_fragments: frozenset[str]
    env_sourced: frozenset[str]
    loop_sourced: frozenset[str]
    fragment_bindings: Mapping[str, str] = field(default_factory=dict)
    #: Defaults to empty so a hand-built result (tests, fixtures) stays
    #: constructible. Note what that default *means* downstream: nothing
    #: is required, so every capability failure is recorded rather than
    #: raised. Real runs always go through :func:`negotiate_observation`,
    #: which fills it exactly.
    required_fragments: frozenset[str] = frozenset()


def _loop_sourced_keys() -> frozenset[str]:
    """Every registered fragment key a Loop fills in (``__source__ = "loop"``).

    These have no capability behind them, so they are never in
    ``env_provides``. Which of them *this* run can resolve is the selected
    Loop's declaration (:func:`loop_fragments`).
    """
    return frozenset(
        key
        for key, cls in ObservationRegistrar.get_all().items()
        if getattr(cls, "__source__", "env") == "loop"
    )


def loop_fragments(loop: Any) -> frozenset[str]:
    """The loop-source fragment keys *loop* builds: its class's ``__loop_fragments__``.

    ``AgentLoopBase`` declares the shared builders' four by default
    (``core/loops/_loop_fragments.py``). An object that declares nothing — a
    structural Loop outside the base — builds nothing.
    """
    cls = loop if isinstance(loop, type) else type(loop)
    return frozenset(getattr(cls, "__loop_fragments__", frozenset()))


def _observation_rules(
    *,
    agent: tuple[set[str], set[str]],
    loop: tuple[set[str], set[str]],
    env_provides: set[str],
    agent_source: str = "",
    loop_source: str = "",
    loop_provides: frozenset[str] | None = None,
) -> ObservationNegotiationResult:
    """Avro-style three-party negotiation semantics.

    Agent and Loop declare in capability-relative space unless they pin a
    provider; resolution against this run's fragment universe turns those
    declarations into fully-qualified keys, and everything the result
    hands downstream is fully qualified. The universe is the environment's
    keys plus the loop-source keys the Loop builds (*loop_provides*, the
    selected Loop's ``__loop_fragments__``; ``None`` — a caller of the bare
    rules that names no Loop — means every registered one).

    A required key that resolves to a registered loop-source fragment the
    Loop does not build is refused naming the fragment and the Loop — at
    ``prepare()``, rather than as a missing builder at the first step.

    ``agent_source`` / ``loop_source`` are labels, not inputs: they never
    reach :func:`resolve_demand` and cannot change what is negotiated.
    They exist so a failure can say *who* asked — which stopped being
    derivable from the class once an instance could narrow its own demand.
    Omitted, the diagnosis simply carries no attribution.
    """
    agent_required, agent_optional = agent
    loop_required, loop_optional = loop

    registered = _loop_sourced_keys()
    loop_keys = registered if loop_provides is None else registered & loop_provides
    demand = resolve_demand(
        required=agent_required | loop_required,
        optional=agent_optional | loop_optional,
        universe=set(env_provides) | loop_keys,
        separator=FRAGMENT_KEY_SEPARATOR,
        ambiguity_error=AmbiguousFragmentError,
    )

    if demand.missing:
        parties = [(agent_source, agent_required), (loop_source, loop_required)]
        # Missing because the Loop does not build it — not the environment's.
        unbuilt = resolve_demand(
            required=demand.missing,
            optional=(),
            universe=registered - loop_keys,
            separator=FRAGMENT_KEY_SEPARATOR,
            ambiguity_error=AmbiguousFragmentError,
        ).bindings
        if unbuilt:
            raise IncompatibleFragmentError(
                missing=set(unbuilt),
                provided=set(loop_keys),
                requested_by=requested_by(unbuilt, parties),
                provider=f"loop {loop_source!r}" if loop_source else "the loop",
            )
        raise IncompatibleFragmentError(
            missing=set(demand.missing),
            provided=env_provides,
            requested_by=requested_by(demand.missing, parties),
        )

    active = demand.resolved
    loop_sourced = active & loop_keys
    env_sourced = active - loop_keys

    # The generated model exposes fragments under their local name only,
    # so two sources claiming one local id cannot coexist. Fail here
    # rather than inside build_observation_model on the first observe().
    # Both providers can only be active because the wiring pinned each of
    # them by name — a *relative* declaration would have been refused one
    # step earlier by ``AmbiguousFragmentError``.
    by_local = group_by_short_name(active, FRAGMENT_KEY_SEPARATOR)
    collisions = {name: keys for name, keys in by_local.items() if len(keys) > 1}
    if collisions:
        raise DuplicateFragmentNameError(collisions)

    # Required keys, carried forward in the same fully-qualified space as
    # everything else the result hands downstream. Every declared-required
    # key has a binding: one without a provider is in ``demand.missing``,
    # and that raised above — so this indexes rather than filtering. A
    # ``if declared in demand.bindings`` guard here would fail open on the
    # one invariant this set carries: a key that slipped through it would
    # be silently reclassified as optional, and its capture failure would
    # land in the observation as a FragmentFailure instead of stopping the
    # run. KeyError is the right answer to an impossible state.
    declared_required = agent_required | loop_required
    required = frozenset(demand.bindings[declared] for declared in declared_required)

    return ObservationNegotiationResult(
        active_fragments=active,
        env_sourced=frozenset(env_sourced),
        loop_sourced=frozenset(loop_sourced),
        fragment_bindings=bind_short_names(by_local),
        required_fragments=required,
    )


def negotiate_observation(
    agent: Any,
    loop: Any,
    env: Any,
) -> ObservationNegotiationResult:
    """Run observation negotiation for an (agent, loop, env) triple.

    The agent's half comes from :func:`~spectragent.core.negotiation._base.
    demand_of`, so a configured instance may narrow inside its class's
    declared space (an agent in vision mode excludes ``dom`` and promotes
    the coordinate verbs). The *rules* still see one ``(required,
    optional)`` pair either way, which is what keeps instance narrowing
    from becoming a second negotiation semantics — the labels that ride
    alongside are used for one thing, naming the requester in a failure.

    Only the agent's half is narrowed. The Loop's declaration goes in as
    its class wrote it: an agent cannot excuse itself from a fragment the
    Loop needs to do its job. The Loop is also a provider: the loop-source
    fragments it builds (:func:`loop_fragments`) are the only ones on offer
    besides the environment's.

    *env* is ``None`` in an env-less run (a Loop declaring
    ``__requires_env__ = False``): it provides nothing, so an agent that
    requires an environment fragment is refused like any missing one.
    """
    return negotiate(
        ObservationNegotiationResult,
        "observation",
        _observation_rules,
        agent=demand_of(agent, "observation"),
        loop=type(loop),
        env_provides=set(env.provided_fragments) if env is not None else set(),
        agent_source=demand_label(agent),
        loop_source=demand_label(loop),
        loop_provides=loop_fragments(loop),
    )

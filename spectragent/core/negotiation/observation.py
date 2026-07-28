"""Three-party observation negotiation — Capability / Agent / Loop.

Pure-data outputs with zero type coupling to concrete Agent/Loop classes.
The Avro-style rules live in ``_observation_rules``; ``negotiate_observation``
is a thin wrapper over the generic :func:`negotiate` engine.

See docs/architecture_design.md §4.4 for the full specification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spectragent.core.negotiation._registrar import NegotiationResultBase
from spectragent.core.negotiation.engine import negotiate
from spectragent.schemas.observation._registrar import ObservationRegistrar


class IncompatibleFragmentError(Exception):
    """Raised when required fragments cannot be satisfied by the environment.

    Reports the *complete* missing set, matching
    :class:`~spectragent.core.negotiation.action.IncompatibleActionError`.
    Raising on the first miss made the user fix-and-rerun once per missing
    fragment, and — because the scan iterated an unordered ``set`` — the
    one they were told about varied between processes.
    """

    def __init__(self, required: set[str], provided: set[str]) -> None:
        missing = required - provided
        super().__init__(
            f"Required observation fragments not provided by environment. "
            f"Missing: {sorted(missing)}. "
            f"Environment provides: {sorted(provided)}. "
            f"Required: {sorted(required)}."
        )
        self.missing = missing
        self.provided = provided
        self.required = required


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
        active_fragments: All fragments activated for this run.
        env_sourced: Subset of active_fragments provided by Capability resolvers.
        loop_sourced: Subset of active_fragments filled by the Loop itself.
    """

    __registry_key__ = "observation"

    active_fragments: frozenset[str]
    env_sourced: frozenset[str]
    loop_sourced: frozenset[str]


def _observation_rules(
    *,
    agent: tuple[set[str], set[str]],
    loop: tuple[set[str], set[str]],
    env_provides: set[str],
) -> ObservationNegotiationResult:
    """Avro-style three-party negotiation semantics."""
    agent_required, agent_optional = agent
    loop_required, loop_optional = loop

    all_required = agent_required | loop_required
    all_optional = agent_optional | loop_optional
    all_requested = all_required | all_optional

    env_sourced: set[str] = set()
    loop_sourced: set[str] = set()
    missing: set[str] = set()

    # Sorted so the diagnosis is identical between processes.
    for frag_key in sorted(all_requested):
        frag_cls = ObservationRegistrar.get(frag_key)
        source = getattr(frag_cls, "__source__", "env")

        if source == "loop":
            loop_sourced.add(frag_key)
        elif frag_key in env_provides:
            env_sourced.add(frag_key)
        elif frag_key in all_required:
            missing.add(frag_key)

    if missing:
        raise IncompatibleFragmentError(
            required=missing,
            provided=env_provides,
        )

    active = frozenset(env_sourced | loop_sourced)

    # The generated model exposes fragments under their local name only,
    # so two sources claiming one local id cannot coexist. Fail here
    # rather than inside build_observation_model on the first observe().
    by_local: dict[str, list[str]] = {}
    for frag_key in active:
        by_local.setdefault(frag_key.rsplit(".", 1)[-1], []).append(frag_key)
    collisions = {name: keys for name, keys in by_local.items() if len(keys) > 1}
    if collisions:
        raise DuplicateFragmentNameError(collisions)

    return ObservationNegotiationResult(
        active_fragments=active,
        env_sourced=frozenset(env_sourced),
        loop_sourced=frozenset(loop_sourced),
    )


def negotiate_observation(
    agent: Any,
    loop: Any,
    env: Any,
) -> ObservationNegotiationResult:
    """Run observation negotiation for an (agent, loop, env) triple."""
    return negotiate(
        ObservationNegotiationResult,
        "observation",
        _observation_rules,
        agent=type(agent),
        loop=type(loop),
        env_provides=set(env.provided_fragments),
    )

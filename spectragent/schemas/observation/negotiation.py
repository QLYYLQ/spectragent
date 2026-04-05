"""Three-party observation negotiation engine.

Pure functions with zero type coupling — inputs are plain sets,
no Agent/Loop/Capability types imported.

See docs/architecture_design.md §4.4 "协商规则 (Avro-style resolution)" for the
full specification.
"""

from __future__ import annotations

from dataclasses import dataclass

from conscribe.wiring import WiringSpec, parse_wiring

from spectragent.schemas.observation._registrar import ObservationRegistrar


class IncompatibleFragmentError(Exception):
    """Raised when required fragments cannot be satisfied by the environment."""

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


@dataclass(frozen=True)
class NegotiationResult:
    """Output of the three-party observation negotiation.

    Attributes:
        active_fragments: All fragments activated for this run.
        env_sourced: Subset of active_fragments provided by Capability resolvers.
        loop_sourced: Subset of active_fragments filled by the Loop itself.
    """

    active_fragments: frozenset[str]
    env_sourced: frozenset[str]
    loop_sourced: frozenset[str]


def negotiate(
    env_provides: set[str],
    agent_required: set[str],
    agent_optional: set[str],
    loop_required: set[str],
    loop_optional: set[str],
) -> NegotiationResult:
    """Avro-style three-party negotiation.

    Rules:
        - env-sourced ∧ required ∧ env provides → activate
        - env-sourced ∧ optional ∧ env provides → activate
        - env provides ∧ nobody requires → skip (performance optimization)
        - required ∧ env does not provide → CONFIG ERROR
        - loop-sourced → always activate

    Args:
        env_provides: Fragment keys the Capability(s) can provide.
        agent_required: Fragment keys the Agent requires (from ``__wiring__``).
        agent_optional: Fragment keys the Agent optionally uses.
        loop_required: Fragment keys the Loop requires.
        loop_optional: Fragment keys the Loop optionally uses.

    Returns:
        NegotiationResult with activated fragment sets.

    Raises:
        IncompatibleFragmentError: When a required env-sourced fragment
            is not provided by the environment.
    """
    all_required = agent_required | loop_required
    all_optional = agent_optional | loop_optional
    all_requested = all_required | all_optional

    env_sourced: set[str] = set()
    loop_sourced: set[str] = set()

    for frag_key in all_requested:
        frag_cls = ObservationRegistrar.get(frag_key)
        source = getattr(frag_cls, "__source__", "env")

        if source == "loop":
            # Loop-sourced fragments are always activated
            loop_sourced.add(frag_key)
        elif frag_key in env_provides:
            # Env provides it and someone requested it → activate
            env_sourced.add(frag_key)
        elif frag_key in all_required:
            # Required but env doesn't provide → incompatible
            raise IncompatibleFragmentError(
                required={frag_key},
                provided=env_provides,
            )
        # else: optional and env doesn't provide → skip (graceful degradation)

    active = frozenset(env_sourced | loop_sourced)
    return NegotiationResult(
        active_fragments=active,
        env_sourced=frozenset(env_sourced),
        loop_sourced=frozenset(loop_sourced),
    )


def extract_observation_wiring(cls: type) -> tuple[set[str], set[str]]:
    """Extract observation fragment requirements from a class's ``__wiring__``.

    Uses ``conscribe.wiring.parse_wiring()`` (not ``resolve_wiring``) to avoid
    requiring registries to be populated at parse time.

    Args:
        cls: Any class with ``__wiring__`` (Agent, Loop, Capability).

    Returns:
        Tuple of ``(required, optional)`` fragment key sets.
    """
    specs = parse_wiring(cls)
    for spec in specs:
        if spec.registry_name == "observation":
            required = set(spec.allowed_keys) if spec.allowed_keys else set()
            optional = set(spec.optional_keys) if spec.optional_keys else set()
            return required, optional
    return set(), set()

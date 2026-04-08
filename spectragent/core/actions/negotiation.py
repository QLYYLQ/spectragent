"""Two-party action negotiation engine (agent ↔ environment).

Symmetric to :mod:`spectragent.schemas.observation.negotiation`. Action
negotiation has only two parties because all actions are env-sourced —
the Loop is just the driver and does not contribute action requirements.

See docs/architecture_design.md §4.5 "对称协商" for the full design.
"""

from __future__ import annotations

from dataclasses import dataclass

from conscribe.wiring import parse_wiring


class IncompatibleActionError(Exception):
    """Raised when required actions cannot be satisfied by the environment."""

    def __init__(self, required: set[str], provided: set[str]) -> None:
        missing = required - provided
        super().__init__(
            f"Required actions not provided by environment. "
            f"Missing: {sorted(missing)}. "
            f"Environment provides: {sorted(provided)}. "
            f"Required: {sorted(required)}."
        )
        self.missing = missing
        self.provided = provided
        self.required = required


@dataclass(frozen=True)
class ActionNegotiationResult:
    """Output of the two-party action negotiation.

    Attributes:
        active_actions: Action names activated for this run.
    """

    active_actions: frozenset[str]


def negotiate_actions(
    env_provides: set[str],
    agent_required: set[str],
    agent_optional: set[str],
) -> ActionNegotiationResult:
    """Two-party action negotiation.

    Rules:
        - required ∧ env provides → activate
        - optional ∧ env provides → activate
        - optional ∧ env does not provide → skip (graceful degradation)
        - required ∧ env does not provide → CONFIG ERROR

    Args:
        env_provides: Action names the Environment's Capabilities can provide.
        agent_required: Action names the Agent requires.
        agent_optional: Action names the Agent optionally uses.

    Returns:
        ActionNegotiationResult with the activated action set.

    Raises:
        IncompatibleActionError: When a required action is not provided
            by the environment.
    """
    missing = agent_required - env_provides
    if missing:
        raise IncompatibleActionError(
            required=agent_required, provided=env_provides,
        )
    active = agent_required | (agent_optional & env_provides)
    return ActionNegotiationResult(active_actions=frozenset(active))


def extract_action_wiring(cls: type) -> tuple[set[str], set[str]]:
    """Extract action requirements from a class's ``__wiring__``.

    Uses ``conscribe.wiring.parse_wiring()`` (not ``resolve_wiring``) to
    avoid requiring registries to be populated at parse time.

    Args:
        cls: Any class with ``__wiring__`` (Agent, Capability).

    Returns:
        Tuple of ``(required, optional)`` action name sets.
    """
    specs = parse_wiring(cls)
    for spec in specs:
        if spec.registry_name == "action":
            required = set(spec.allowed_keys) if spec.allowed_keys else set()
            optional = set(spec.optional_keys) if spec.optional_keys else set()
            return required, optional
    return set(), set()

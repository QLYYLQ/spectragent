"""Tests for the two-party action negotiation engine.

Structurally parallel to :mod:`tests.test_core.test_negotiation.test_observation`.
Action negotiation has only two parties (env ↔ agent); loops are drivers
and do not contribute action requirements.
"""

from __future__ import annotations

import pytest

from spectragent.core.negotiation import (
    ActionNegotiationResult,
    IncompatibleActionError,
    extract_wiring,
)
from spectragent.core.negotiation.action import _action_rules


def _run(
    *,
    env_provides: set[str],
    agent_required: set[str],
    agent_optional: set[str],
) -> ActionNegotiationResult:
    return _action_rules(
        agent=(agent_required, agent_optional),
        env_provides=env_provides,
    )


class TestNegotiateBasic:
    """Basic negotiation scenarios."""

    def test_all_required_provided(self) -> None:
        result = _run(
            env_provides={"click", "type", "done"},
            agent_required={"click", "type", "done"},
            agent_optional=set(),
        )
        assert isinstance(result, ActionNegotiationResult)
        assert result.active_actions == frozenset({"click", "type", "done"})

    def test_optional_provided_activates(self) -> None:
        result = _run(
            env_provides={"click", "type", "scroll"},
            agent_required={"click"},
            agent_optional={"scroll"},
        )
        assert "click" in result.active_actions
        assert "scroll" in result.active_actions

    def test_optional_not_provided_graceful(self) -> None:
        """Optional action not in env_provides → skip, no error."""
        result = _run(
            env_provides={"click"},
            agent_required={"click"},
            agent_optional={"scroll"},
        )
        assert "click" in result.active_actions
        assert "scroll" not in result.active_actions

    def test_unrequested_env_provides_skipped(self) -> None:
        """env provides more than agent asks for → extras stay inactive."""
        result = _run(
            env_provides={"click", "type", "scroll", "done"},
            agent_required={"click"},
            agent_optional=set(),
        )
        assert result.active_actions == frozenset({"click"})


class TestNegotiateErrors:
    """Required action not provided → raise IncompatibleActionError."""

    def test_required_missing_raises(self) -> None:
        with pytest.raises(IncompatibleActionError) as exc_info:
            _run(
                env_provides={"click"},
                agent_required={"bash"},
                agent_optional=set(),
            )
        assert "bash" in str(exc_info.value)
        assert exc_info.value.missing == {"bash"}

    def test_multiple_missing_listed(self) -> None:
        with pytest.raises(IncompatibleActionError) as exc_info:
            _run(
                env_provides={"click"},
                agent_required={"bash", "edit_file"},
                agent_optional=set(),
            )
        assert "bash" in str(exc_info.value)
        assert "edit_file" in str(exc_info.value)

    def test_optional_alone_never_raises(self) -> None:
        """All optional, none provided → active is empty but no error."""
        result = _run(
            env_provides={"click"},
            agent_required=set(),
            agent_optional={"scroll", "send_keys"},
        )
        assert result.active_actions == frozenset()


class TestExtractActionWiring:
    """``extract_wiring(..., "action")`` reads ``__wiring__`` declarations."""

    def test_no_wiring(self) -> None:
        class NoWiring:
            pass

        req, opt = extract_wiring(NoWiring, "action")
        assert req == set()
        assert opt == set()

    def test_required_only(self) -> None:
        class WithRequired:
            __wiring__ = {
                "action": ("action", ["click", "done"]),
            }

        req, opt = extract_wiring(WithRequired, "action")
        assert req == {"click", "done"}
        assert opt == set()

    def test_required_and_optional(self) -> None:
        class WithBoth:
            __wiring__ = {
                "action": ("action", ["click"], ["scroll", "send_keys"]),
            }

        req, opt = extract_wiring(WithBoth, "action")
        assert req == {"click"}
        assert opt == {"scroll", "send_keys"}

    def test_observation_wiring_ignored(self) -> None:
        class ObsOnly:
            __wiring__ = {
                "observation": ("observation", ["dom"]),
            }

        req, opt = extract_wiring(ObsOnly, "action")
        assert req == set()
        assert opt == set()

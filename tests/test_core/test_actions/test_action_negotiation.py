"""Tests for the two-party action negotiation engine.

Structurally parallel to
``tests/test_schemas/test_observation/test_negotiation.py``. Action
negotiation has only two parties (env ↔ agent); loops are drivers and
do not contribute action requirements.
"""

from __future__ import annotations

import pytest

from spectragent.core.actions.negotiation import (
    ActionNegotiationResult,
    IncompatibleActionError,
    extract_action_wiring,
    negotiate_actions,
)


class TestNegotiateBasic:
    """Basic negotiation scenarios."""

    def test_all_required_provided(self) -> None:
        result = negotiate_actions(
            env_provides={"click", "type", "done"},
            agent_required={"click", "type", "done"},
            agent_optional=set(),
        )
        assert isinstance(result, ActionNegotiationResult)
        assert result.active_actions == frozenset({"click", "type", "done"})

    def test_optional_provided_activates(self) -> None:
        result = negotiate_actions(
            env_provides={"click", "type", "scroll"},
            agent_required={"click"},
            agent_optional={"scroll"},
        )
        assert "click" in result.active_actions
        assert "scroll" in result.active_actions

    def test_optional_not_provided_graceful(self) -> None:
        """Optional action not in env_provides → skip, no error."""
        result = negotiate_actions(
            env_provides={"click"},
            agent_required={"click"},
            agent_optional={"scroll"},
        )
        assert "click" in result.active_actions
        assert "scroll" not in result.active_actions

    def test_unrequested_env_provides_skipped(self) -> None:
        """env provides more than agent asks for → extras stay inactive."""
        result = negotiate_actions(
            env_provides={"click", "type", "scroll", "done"},
            agent_required={"click"},
            agent_optional=set(),
        )
        assert result.active_actions == frozenset({"click"})


class TestNegotiateErrors:
    """Required action not provided → raise IncompatibleActionError."""

    def test_required_missing_raises(self) -> None:
        with pytest.raises(IncompatibleActionError) as exc_info:
            negotiate_actions(
                env_provides={"click"},
                agent_required={"bash"},
                agent_optional=set(),
            )
        assert "bash" in str(exc_info.value)
        assert exc_info.value.missing == {"bash"}

    def test_multiple_missing_listed(self) -> None:
        with pytest.raises(IncompatibleActionError) as exc_info:
            negotiate_actions(
                env_provides={"click"},
                agent_required={"bash", "edit_file"},
                agent_optional=set(),
            )
        assert "bash" in str(exc_info.value)
        assert "edit_file" in str(exc_info.value)

    def test_optional_alone_never_raises(self) -> None:
        """All optional, none provided → active is empty but no error."""
        result = negotiate_actions(
            env_provides={"click"},
            agent_required=set(),
            agent_optional={"scroll", "send_keys"},
        )
        assert result.active_actions == frozenset()


class TestExtractActionWiring:
    """extract_action_wiring reads __wiring__ declarations."""

    def test_no_wiring(self) -> None:
        class NoWiring:
            pass
        req, opt = extract_action_wiring(NoWiring)
        assert req == set()
        assert opt == set()

    def test_required_only(self) -> None:
        class WithRequired:
            __wiring__ = {
                "action": ("action", ["click", "done"]),
            }
        req, opt = extract_action_wiring(WithRequired)
        assert req == {"click", "done"}
        assert opt == set()

    def test_required_and_optional(self) -> None:
        class WithBoth:
            __wiring__ = {
                "action": ("action", ["click"], ["scroll", "send_keys"]),
            }
        req, opt = extract_action_wiring(WithBoth)
        assert req == {"click"}
        assert opt == {"scroll", "send_keys"}

    def test_observation_wiring_ignored(self) -> None:
        class ObsOnly:
            __wiring__ = {
                "observation": ("observation", ["dom"]),
            }
        req, opt = extract_action_wiring(ObsOnly)
        assert req == set()
        assert opt == set()

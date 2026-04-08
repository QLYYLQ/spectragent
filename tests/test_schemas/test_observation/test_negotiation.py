"""Tests for the three-party observation negotiation engine."""

import pytest

# Ensure fragments are registered before negotiation tests
from spectragent.schemas.observation import fragments as _  # noqa: F401
from spectragent.schemas.observation.negotiation import (
    IncompatibleFragmentError,
    NegotiationResult,
    extract_observation_wiring,
    negotiate,
)


class TestNegotiateBasic:
    """Basic negotiation scenarios."""

    def test_all_required_provided(self) -> None:
        result = negotiate(
            env_provides={"dom", "screenshot"},
            agent_required={"dom", "screenshot"},
            agent_optional=set(),
            loop_required=set(),
            loop_optional=set(),
        )
        assert result.env_sourced == frozenset({"dom", "screenshot"})
        assert result.loop_sourced == frozenset()
        assert result.active_fragments == frozenset({"dom", "screenshot"})

    def test_optional_provided(self) -> None:
        result = negotiate(
            env_provides={"dom", "screenshot", "browser_meta"},
            agent_required={"dom"},
            agent_optional={"screenshot"},
            loop_required=set(),
            loop_optional=set(),
        )
        assert "dom" in result.env_sourced
        assert "screenshot" in result.env_sourced

    def test_optional_not_provided_graceful(self) -> None:
        """Optional fragment not in env_provides → skip, no error."""
        result = negotiate(
            env_provides={"dom"},
            agent_required={"dom"},
            agent_optional={"screenshot"},
            loop_required=set(),
            loop_optional=set(),
        )
        assert "dom" in result.active_fragments
        assert "screenshot" not in result.active_fragments

    def test_loop_sourced_always_active(self) -> None:
        """action_result is loop-sourced → always activated."""
        result = negotiate(
            env_provides={"dom"},
            agent_required={"dom"},
            agent_optional=set(),
            loop_required={"action_result"},
            loop_optional=set(),
        )
        assert "action_result" in result.loop_sourced
        assert "action_result" in result.active_fragments

    def test_unrequested_env_provides_skipped(self) -> None:
        """env provides screenshot but nobody requires it → not activated."""
        result = negotiate(
            env_provides={"dom", "screenshot", "browser_meta"},
            agent_required={"dom"},
            agent_optional=set(),
            loop_required=set(),
            loop_optional=set(),
        )
        assert result.active_fragments == frozenset({"dom"})


class TestNegotiateErrors:
    """Negotiation failure cases."""

    def test_required_missing_raises(self) -> None:
        with pytest.raises(IncompatibleFragmentError) as exc_info:
            negotiate(
                env_provides={"dom"},
                agent_required={"terminal"},
                agent_optional=set(),
                loop_required=set(),
                loop_optional=set(),
            )
        assert "terminal" in str(exc_info.value)

    def test_loop_required_missing_raises(self) -> None:
        """Loop requires terminal but env doesn't provide it."""
        with pytest.raises(IncompatibleFragmentError):
            negotiate(
                env_provides={"dom"},
                agent_required=set(),
                agent_optional=set(),
                loop_required={"terminal"},
                loop_optional=set(),
            )


class TestNegotiateMultiParty:
    """Three-party negotiation with agent + loop requirements."""

    def test_agent_and_loop_both_require(self) -> None:
        result = negotiate(
            env_provides={"dom", "screenshot", "terminal"},
            agent_required={"dom", "screenshot"},
            agent_optional=set(),
            loop_required={"action_result"},
            loop_optional=set(),
        )
        assert result.env_sourced == frozenset({"dom", "screenshot"})
        assert result.loop_sourced == frozenset({"action_result"})
        assert len(result.active_fragments) == 3

    def test_mixed_required_optional(self) -> None:
        result = negotiate(
            env_provides={"dom", "browser_meta"},
            agent_required={"dom"},
            agent_optional={"screenshot"},  # not provided
            loop_required={"action_result"},
            loop_optional={"browser_meta"},
        )
        assert "dom" in result.env_sourced
        assert "browser_meta" in result.env_sourced
        assert "screenshot" not in result.active_fragments
        assert "action_result" in result.loop_sourced


class TestExtractObservationWiring:
    """extract_observation_wiring reads __wiring__ declarations."""

    def test_no_wiring(self) -> None:
        class NoWiring:
            pass
        req, opt = extract_observation_wiring(NoWiring)
        assert req == set()
        assert opt == set()

    def test_required_only(self) -> None:
        class WithRequired:
            __wiring__ = {
                "observation": ("observation", ["dom", "screenshot"]),
            }
        req, opt = extract_observation_wiring(WithRequired)
        assert req == {"dom", "screenshot"}
        assert opt == set()

    def test_required_and_optional(self) -> None:
        class WithBoth:
            __wiring__ = {
                "observation": ("observation", ["terminal"], ["filesystem"]),
            }
        req, opt = extract_observation_wiring(WithBoth)
        assert req == {"terminal"}
        assert opt == {"filesystem"}

    def test_non_observation_wiring_ignored(self) -> None:
        class OtherWiring:
            __wiring__ = {
                "agent_loop": ("agent_loop", ["react"]),
            }
        req, opt = extract_observation_wiring(OtherWiring)
        assert req == set()
        assert opt == set()

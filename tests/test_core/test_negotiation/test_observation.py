"""Tests for the three-party observation negotiation engine."""

import pytest

from spectragent.core.negotiation import (
    IncompatibleFragmentError,
    ObservationNegotiationResult,
    extract_wiring,
)
from spectragent.core.negotiation.observation import _observation_rules

# Ensure fragments are registered before negotiation tests
from spectragent.schemas.observation import fragments as _  # noqa: F401


def _run(
    *,
    env_provides: set[str],
    agent_required: set[str],
    agent_optional: set[str],
    loop_required: set[str],
    loop_optional: set[str],
) -> ObservationNegotiationResult:
    """Thin helper that packs the old keyword form into the new rules API."""
    return _observation_rules(
        agent=(agent_required, agent_optional),
        loop=(loop_required, loop_optional),
        env_provides=env_provides,
    )


class TestNegotiateBasic:
    """Basic negotiation scenarios."""

    def test_all_required_provided(self) -> None:
        result = _run(
            env_provides={"browser.dom", "browser.screenshot"},
            agent_required={"browser.dom", "browser.screenshot"},
            agent_optional=set(),
            loop_required=set(),
            loop_optional=set(),
        )
        assert result.env_sourced == frozenset({"browser.dom", "browser.screenshot"})
        assert result.loop_sourced == frozenset()
        assert result.active_fragments == frozenset(
            {"browser.dom", "browser.screenshot"},
        )

    def test_optional_provided(self) -> None:
        result = _run(
            env_provides={"browser.dom", "browser.screenshot", "browser.browser_meta"},
            agent_required={"browser.dom"},
            agent_optional={"browser.screenshot"},
            loop_required=set(),
            loop_optional=set(),
        )
        assert "browser.dom" in result.env_sourced
        assert "browser.screenshot" in result.env_sourced

    def test_optional_not_provided_graceful(self) -> None:
        """Optional fragment not in env_provides → skip, no error."""
        result = _run(
            env_provides={"browser.dom"},
            agent_required={"browser.dom"},
            agent_optional={"browser.screenshot"},
            loop_required=set(),
            loop_optional=set(),
        )
        assert "browser.dom" in result.active_fragments
        assert "browser.screenshot" not in result.active_fragments

    def test_loop_sourced_always_active(self) -> None:
        """loop.action_result is loop-sourced → always activated."""
        result = _run(
            env_provides={"browser.dom"},
            agent_required={"browser.dom"},
            agent_optional=set(),
            loop_required={"loop.action_result"},
            loop_optional=set(),
        )
        assert "loop.action_result" in result.loop_sourced
        assert "loop.action_result" in result.active_fragments

    def test_unrequested_env_provides_skipped(self) -> None:
        """env provides screenshot but nobody requires it → not activated."""
        result = _run(
            env_provides={"browser.dom", "browser.screenshot", "browser.browser_meta"},
            agent_required={"browser.dom"},
            agent_optional=set(),
            loop_required=set(),
            loop_optional=set(),
        )
        assert result.active_fragments == frozenset({"browser.dom"})


class TestNegotiateErrors:
    """Negotiation failure cases."""

    def test_required_missing_raises(self) -> None:
        with pytest.raises(IncompatibleFragmentError) as exc_info:
            _run(
                env_provides={"browser.dom"},
                agent_required={"docker.terminal"},
                agent_optional=set(),
                loop_required=set(),
                loop_optional=set(),
            )
        assert "docker.terminal" in str(exc_info.value)

    def test_loop_required_missing_raises(self) -> None:
        """Loop requires terminal but env doesn't provide it."""
        with pytest.raises(IncompatibleFragmentError):
            _run(
                env_provides={"browser.dom"},
                agent_required=set(),
                agent_optional=set(),
                loop_required={"docker.terminal"},
                loop_optional=set(),
            )


class TestNegotiateMultiParty:
    """Three-party negotiation with agent + loop requirements."""

    def test_agent_and_loop_both_require(self) -> None:
        result = _run(
            env_provides={"browser.dom", "browser.screenshot", "docker.terminal"},
            agent_required={"browser.dom", "browser.screenshot"},
            agent_optional=set(),
            loop_required={"loop.action_result"},
            loop_optional=set(),
        )
        assert result.env_sourced == frozenset(
            {"browser.dom", "browser.screenshot"},
        )
        assert result.loop_sourced == frozenset({"loop.action_result"})
        assert len(result.active_fragments) == 3

    def test_mixed_required_optional(self) -> None:
        result = _run(
            env_provides={"browser.dom", "browser.browser_meta"},
            agent_required={"browser.dom"},
            agent_optional={"browser.screenshot"},  # not provided
            loop_required={"loop.action_result"},
            loop_optional={"browser.browser_meta"},
        )
        assert "browser.dom" in result.env_sourced
        assert "browser.browser_meta" in result.env_sourced
        assert "browser.screenshot" not in result.active_fragments
        assert "loop.action_result" in result.loop_sourced


class TestExtractWiring:
    """extract_wiring reads ``__wiring__`` declarations for any registry."""

    def test_no_wiring(self) -> None:
        class NoWiring:
            pass
        req, opt = extract_wiring(NoWiring, "observation")
        assert req == set()
        assert opt == set()

    def test_required_only(self) -> None:
        class WithRequired:
            __wiring__ = {
                "observation": ("observation", ["dom", "screenshot"]),
            }
        req, opt = extract_wiring(WithRequired, "observation")
        assert req == {"dom", "screenshot"}
        assert opt == set()

    def test_required_and_optional(self) -> None:
        class WithBoth:
            __wiring__ = {
                "observation": ("observation", ["terminal"], ["filesystem"]),
            }
        req, opt = extract_wiring(WithBoth, "observation")
        assert req == {"terminal"}
        assert opt == {"filesystem"}

    def test_non_matching_wiring_ignored(self) -> None:
        class OtherWiring:
            __wiring__ = {
                "agent_loop": ("agent_loop", ["react"]),
            }
        req, opt = extract_wiring(OtherWiring, "observation")
        assert req == set()
        assert opt == set()

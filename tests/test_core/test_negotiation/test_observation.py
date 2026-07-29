"""Tests for the three-party observation negotiation engine."""

import pytest

# Register the fragments these tests negotiate over. Two imports, because
# a fragment lives with the code that produces it: the shared module below
# holds the ``loop.*`` ones, this package holds ``browser.dom`` & co.
import spectragent.core.capabilities.browser.observations  # noqa: F401
from spectragent.core.negotiation import (
    IncompatibleFragmentError,
    ObservationNegotiationResult,
    extract_wiring,
)
from spectragent.core.negotiation.observation import (
    DuplicateFragmentNameError,
    _observation_rules,
)
from spectragent.schemas.observation import fragments as _  # noqa: F401
from spectragent.schemas.observation._registrar import ObservationRegistrar


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


class TestCompleteMissingSet:
    """§6.7.1 — report every missing fragment, not an arbitrary one.

    Raising on the first miss made the user fix-and-rerun once per
    missing fragment, and because the scan iterated an unordered ``set``
    the fragment they were told about varied between processes.
    ``negotiate_action`` always computed the full set; this closes the
    asymmetry.
    """

    def test_all_missing_required_fragments_are_reported(self) -> None:
        with pytest.raises(IncompatibleFragmentError) as exc_info:
            _run(
                env_provides={"browser.dom"},
                agent_required={
                    "browser.dom",
                    "browser.screenshot",
                    "browser.browser_meta",
                },
                agent_optional=set(),
                loop_required=set(),
                loop_optional=set(),
            )
        assert exc_info.value.missing == {"browser.screenshot", "browser.browser_meta"}
        message = str(exc_info.value)
        assert "browser.screenshot" in message
        assert "browser.browser_meta" in message

    def test_diagnosis_is_stable_across_runs(self) -> None:
        """Same inputs must name the same fragments every time."""
        seen: set[frozenset[str]] = set()
        for _attempt in range(5):
            with pytest.raises(IncompatibleFragmentError) as exc_info:
                _run(
                    env_provides=set(),
                    agent_required={"browser.dom", "browser.screenshot"},
                    agent_optional=set(),
                    loop_required=set(),
                    loop_optional=set(),
                )
            seen.add(frozenset(exc_info.value.missing))
        assert len(seen) == 1


class TestLocalNameCollision:
    """§5.2.3 — a name clash is a config error, not a step-0 crash.

    Fragment keys are fully qualified (``source.local_id``) but the
    generated model exposes only the local segment, so ``swe.terminal``
    and ``docker.terminal`` both want ``obs.terminal``. That was detected
    inside ``build_observation_model`` — i.e. on the first ``env.observe()``
    — even though negotiation had everything it needed to catch it before
    the run started.
    """

    @staticmethod
    def _register_colliding_pair() -> None:
        from spectragent.schemas.observation._registrar import FragmentBase

        for source in ("swe", "docker"):
            key = f"{source}.terminal"
            try:
                ObservationRegistrar.get(key)
            except Exception:
                type(
                    f"{source.title()}TerminalObs",
                    (FragmentBase,),
                    {
                        "__registry_key__": key,
                        "__module__": __name__,
                        "__annotations__": {"stdout": str},
                        "stdout": "",
                    },
                )

    def test_collision_is_rejected_at_negotiation(self) -> None:
        self._register_colliding_pair()
        with pytest.raises(DuplicateFragmentNameError) as exc_info:
            _run(
                env_provides={"swe.terminal", "docker.terminal"},
                agent_required={"swe.terminal", "docker.terminal"},
                agent_optional=set(),
                loop_required=set(),
                loop_optional=set(),
            )
        assert exc_info.value.collisions == {
            "terminal": ["docker.terminal", "swe.terminal"],
        }

    def test_one_source_alone_is_fine(self) -> None:
        self._register_colliding_pair()
        result = _run(
            env_provides={"swe.terminal"},
            agent_required={"swe.terminal"},
            agent_optional=set(),
            loop_required=set(),
            loop_optional=set(),
        )
        assert result.active_fragments == frozenset({"swe.terminal"})


class TestObservationModelCaching:
    """The model is rebuilt once per fragment set, not once per step."""

    def test_same_fragment_set_returns_the_same_class(self) -> None:
        from spectragent.schemas.observation.dynamic_model import build_observation_model

        keys = frozenset({"browser.dom", "browser.screenshot"})
        assert build_observation_model(keys) is build_observation_model(keys)

    def test_different_fragment_sets_get_different_classes(self) -> None:
        from spectragent.schemas.observation.dynamic_model import build_observation_model

        a = build_observation_model(frozenset({"browser.dom"}))
        b = build_observation_model(frozenset({"browser.dom", "browser.screenshot"}))
        assert a is not b

    def test_replacing_a_fragment_class_invalidates_the_cached_model(self) -> None:
        """Cache on the resolved classes, not on the key set alone.

        Keying only on fragment *keys* handed back a model still
        annotated with the previous class after a re-registration, so the
        next ``observe()`` died with ``Input should be ... an instance of
        NetA`` naming a class the registry no longer held — and nothing
        in the repo ever called ``cache_clear()``.
        """
        from spectragent.schemas.observation._registrar import (
            FragmentBase,
            ObservationRegistrar,
        )
        from spectragent.schemas.observation.dynamic_model import build_observation_model

        key = "cachebust.network"
        keys = frozenset({key})

        class NetA(FragmentBase):
            __registry_key__ = key
            request_count: int = 0

        try:
            first = build_observation_model(keys)
            assert first is build_observation_model(keys)

            ObservationRegistrar.unregister(key)

            class NetB(FragmentBase):
                __registry_key__ = key
                request_count: int = 0
                bytes_in: int = 0

            second = build_observation_model(keys)
            assert second is not first
            # ... and the rebuilt model actually accepts the live class.
            obs = second(network=NetB(request_count=3, bytes_in=9))
            assert getattr(obs, "network").bytes_in == 9  # noqa: B009
        finally:
            ObservationRegistrar.unregister(key)

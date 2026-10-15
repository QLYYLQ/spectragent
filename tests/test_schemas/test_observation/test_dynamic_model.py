"""Tests for dynamic Pydantic model generation from negotiated fragments."""

# Ensure fragments are registered
from spectragent.core.negotiation import ObservationNegotiationResult
from spectragent.schemas.observation import fragments as _  # noqa: F401
from spectragent.schemas.observation.dynamic_model import (
    build_observation_model,
    build_observation_model_from_result,
)
from spectragent.schemas.observation.fragments import (
    ActionResultObs,
    TerminalObs,
)


class TestBuildObservationModel:
    """Dynamic model generation from active fragment sets."""

    def test_single_fragment(self) -> None:
        Model = build_observation_model(frozenset({"docker.terminal"}))
        obs = Model(terminal=TerminalObs(stdout="hello", cwd="/tmp"))
        assert obs.terminal.stdout == "hello"  # type: ignore[attr-defined]
        assert obs.terminal.cwd == "/tmp"  # type: ignore[attr-defined]

    def test_multiple_fragments(self) -> None:
        Model = build_observation_model(
            frozenset({"docker.terminal", "loop.action_result"}),
        )
        obs = Model(
            terminal=TerminalObs(stdout="ok", cwd="/"),
            action_result=ActionResultObs(last_action="bash", success=True),
        )
        assert obs.terminal.stdout == "ok"  # type: ignore[attr-defined]
        assert obs.action_result.success is True  # type: ignore[attr-defined]

    def test_no_synthetic_timestamp_field(self) -> None:
        """The model carries fragments only — nothing the loop never fills.

        A synthetic ``timestamp`` field was always 0.0 because nothing
        assigned it, and the trace writer dutifully wrote a junk
        ``NNN_timestamp.json`` every step. Step timing belongs to
        ``StepRecord.timestamp``, which is actually set.
        """
        Model = build_observation_model(frozenset({"docker.terminal"}))
        assert "timestamp" not in Model.model_fields
        obs = Model(terminal=TerminalObs(stdout="x", cwd="/"))
        assert not hasattr(obs, "timestamp")

    def test_model_name(self) -> None:
        Model = build_observation_model(frozenset({"docker.terminal"}))
        assert Model.__name__ == "NegotiatedObservation"

    def test_empty_fragments(self) -> None:
        Model = build_observation_model(frozenset())
        obs = Model()
        assert obs.model_dump() == {}

    def test_json_serialization(self) -> None:
        Model = build_observation_model(frozenset({"docker.terminal"}))
        obs = Model(terminal=TerminalObs(stdout="hi", cwd="/home"))
        data = obs.model_dump()
        assert data["terminal"]["stdout"] == "hi"


class TestBuildFromResult:
    """Convenience wrapper: build from ObservationNegotiationResult."""

    def test_from_result(self) -> None:
        result = ObservationNegotiationResult(
            active_fragments=frozenset({"docker.terminal", "loop.action_result"}),
            env_sourced=frozenset({"docker.terminal"}),
            loop_sourced=frozenset({"loop.action_result"}),
        )
        Model = build_observation_model_from_result(result)
        assert "terminal" in Model.model_fields
        assert "action_result" in Model.model_fields


class TestFailableSlots:
    """An optional fragment's slot may carry the reason it is absent.

    Required slots may not, on purpose: the environment raises for those,
    so a failure landing in one would mean a bug had quietly changed what
    "required" means.
    """

    def test_an_optional_slot_accepts_a_failure(self) -> None:
        from spectragent.schemas.observation.failure import FragmentFailure

        Model = build_observation_model(
            frozenset({"docker.terminal"}), failable=frozenset({"docker.terminal"})
        )
        obs = Model(
            terminal=FragmentFailure(
                fragment_id="docker.terminal",
                capability="docker",
                error="TimeoutError",
                detail="exec timed out",
            )
        )
        assert obs.terminal.error == "TimeoutError"  # type: ignore[attr-defined]
        # And still accepts the real thing.
        assert Model(terminal=TerminalObs(stdout="ok", cwd="/")).terminal.stdout == "ok"  # type: ignore[attr-defined]

    def test_a_required_slot_refuses_a_failure(self) -> None:
        import pytest
        from pydantic import ValidationError

        from spectragent.schemas.observation.failure import FragmentFailure

        Model = build_observation_model(frozenset({"docker.terminal"}))
        with pytest.raises(ValidationError):
            Model(terminal=FragmentFailure(fragment_id="docker.terminal", error="RuntimeError"))

    def test_failability_stays_local_to_each_model(self) -> None:
        """One caller allowing failure cannot widen another caller's slots."""
        keys = frozenset({"docker.terminal"})
        strict = build_observation_model(keys)
        lenient = build_observation_model(keys, failable=keys)
        assert strict is not lenient
        assert strict is not build_observation_model(keys)

    def test_from_result_derives_failability_from_the_negotiation(self) -> None:
        from spectragent.schemas.observation.failure import FragmentFailure

        result = ObservationNegotiationResult(
            active_fragments=frozenset({"docker.terminal", "loop.action_result"}),
            env_sourced=frozenset({"docker.terminal"}),
            loop_sourced=frozenset({"loop.action_result"}),
            required_fragments=frozenset(),
        )
        Model = build_observation_model_from_result(result)
        obs = Model(terminal=FragmentFailure(fragment_id="docker.terminal", error="OSError"))
        assert isinstance(obs.terminal, FragmentFailure)  # type: ignore[attr-defined]

        required = ObservationNegotiationResult(
            active_fragments=frozenset({"docker.terminal"}),
            env_sourced=frozenset({"docker.terminal"}),
            loop_sourced=frozenset(),
            required_fragments=frozenset({"docker.terminal"}),
        )
        assert build_observation_model_from_result(required) is not Model


class TestCaptureTime:
    """Every fragment says when it was captured."""

    def test_a_fragment_stamps_itself_on_construction(self) -> None:
        import time

        before = time.time()
        obs = TerminalObs(stdout="x", cwd="/")
        after = time.time()
        assert before <= obs.captured_at <= after

    def test_capture_time_survives_serialization(self) -> None:
        """It has to reach the trace, which is the only reader that matters."""
        obs = TerminalObs(stdout="x", cwd="/")
        restored = TerminalObs.model_validate_json(obs.model_dump_json())
        assert restored.captured_at == obs.captured_at

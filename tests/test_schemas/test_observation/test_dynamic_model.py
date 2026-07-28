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

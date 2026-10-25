"""The platform-neutral capture state: its value rules and its one-line summary.

``docs/architecture_design.md`` §4.4, "跨平台采集状态 ``capture_state``". The
base names no platform; a capability's concrete fragment supplies the settle
and foreground projections. Here a stub capability's fragment stands in for
one, registered per test (the registry rolls back after each). The browser's
fragment and its resolver are tested in
``tests/test_core/test_capabilities/test_browser_capture_state.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from spectragent.schemas.observation._registrar import ObservationRegistrar
from spectragent.schemas.observation.capture_state import (
    MAX_RESOURCE_IDENTIFIERS,
    CaptureStateFragment,
    FailedVisibleResources,
    ForegroundChange,
    PointerState,
    SettleState,
)


@pytest.fixture
def stub() -> type[CaptureStateFragment]:
    """A capability's concrete capture state that keeps both projections as its own fields."""

    class StubCaptureState(CaptureStateFragment):
        __registry_key__ = "stub.capture_state"

        settled: SettleState | None = None
        change: ForegroundChange | None = None

        @property
        def settle_state(self) -> SettleState | None:
            return self.settled

        @property
        def foreground_change(self) -> ForegroundChange | None:
            return self.change

    return StubCaptureState


UNKNOWN_PROBE = FailedVisibleResources(status="unknown", detail="no answer")


# ── the pointer ───────────────────────────────────────────────────────


class TestPointer:
    def test_known_carries_both_coordinates(self) -> None:
        pointer = PointerState(status="known", x=12, y=40)
        assert (pointer.x, pointer.y) == (12, 40)

    @pytest.mark.parametrize("status", ["unknown", "unavailable"])
    def test_unknown_and_unavailable_carry_none(self, status: Any) -> None:
        pointer = PointerState(status=status)
        assert (pointer.x, pointer.y) == (None, None)

    @pytest.mark.parametrize(
        "fields",
        [
            {"status": "known"},
            {"status": "known", "x": 3},
            {"status": "known", "y": 3},
            {"status": "unknown", "x": 3, "y": 4},
            {"status": "unavailable", "x": 0, "y": 0},
            {"status": "unknown", "x": 3},
        ],
        ids=["known-none", "known-x-only", "known-y-only", "unknown-with", "unavailable-with", "x"],
    )
    def test_coordinates_exactly_when_known(self, fields: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="exactly when its status is known"):
            PointerState(**fields)

    def test_it_is_a_closed_value(self) -> None:
        with pytest.raises(ValidationError):
            PointerState(status="hovering")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            PointerState(status="known", x=1, y=2, shape="arrow")  # type: ignore[call-arg]


# ── failed visible resources ──────────────────────────────────────────


class TestFailedVisibleResources:
    def test_zero_means_measured_and_none_failed(self) -> None:
        none_failed = FailedVisibleResources(status="measured", count=0)
        assert (none_failed.count, none_failed.identifiers) == (0, ())

    @pytest.mark.parametrize("status", ["unknown", "unavailable"])
    def test_an_unmeasured_fact_has_no_count(self, status: Any) -> None:
        assert FailedVisibleResources(status=status).count is None
        with pytest.raises(ValidationError, match="exactly when it was measured"):
            FailedVisibleResources(status=status, count=0)

    def test_measured_needs_a_count(self) -> None:
        with pytest.raises(ValidationError, match="exactly when it was measured"):
            FailedVisibleResources(status="measured")

    def test_an_unmeasured_fact_names_no_resource(self) -> None:
        with pytest.raises(ValidationError, match="names no resources"):
            FailedVisibleResources(status="unknown", identifiers=("a.png",))

    def test_no_more_names_than_failures(self) -> None:
        with pytest.raises(ValidationError, match="more resource identifiers"):
            FailedVisibleResources(status="measured", count=1, identifiers=("a.png", "b.png"))

    def test_names_are_bounded_while_the_count_stays_whole(self) -> None:
        names = tuple(f"{n}.png" for n in range(MAX_RESOURCE_IDENTIFIERS))
        many = FailedVisibleResources(status="measured", count=50, identifiers=names)
        assert many.count == 50 and len(many.identifiers) == MAX_RESOURCE_IDENTIFIERS
        with pytest.raises(ValidationError, match="more resource identifiers"):
            FailedVisibleResources(
                status="measured", count=50, identifiers=(*names, "one-too-many.png")
            )

    def test_a_negative_count_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            FailedVisibleResources(status="measured", count=-1)

    def test_detail_says_why_it_is_unknown(self) -> None:
        assert UNKNOWN_PROBE.detail == "no answer"


class TestSettleAndForeground:
    def test_settle_defaults_to_nothing_pending(self) -> None:
        assert SettleState(outcome="settled", waited_ms=640, loaded=True).pending == ()

    def test_a_negative_wait_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            SettleState(outcome="settled", waited_ms=-1, loaded=True)

    def test_closed_values(self) -> None:
        with pytest.raises(ValidationError):
            SettleState(outcome="quiet", waited_ms=0, loaded=None)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ForegroundChange(reason="switched", from_surface="a", to_surface="b")  # type: ignore[arg-type]


class TestLoaded:
    """Whether the captured surface had finished its initial load (§4.4, "初次加载是否完成")."""

    def test_every_capability_says_it(self) -> None:
        with pytest.raises(ValidationError, match="loaded"):
            SettleState(outcome="capped", waited_ms=5000)  # type: ignore[call-arg]

    @pytest.mark.parametrize("outcome", ["settled", "capped", "unknown"])
    @pytest.mark.parametrize("loaded", [True, False, None])
    def test_a_fact_about_the_surface_not_the_wait(self, outcome: Any, loaded: Any) -> None:
        """No outcome implies a value: a wait may stop waiting on a load that never completes."""
        assert SettleState(outcome=outcome, waited_ms=1, loaded=loaded).loaded is loaded


# ── the abstract base ─────────────────────────────────────────────────


class TestTheBase:
    def test_the_base_is_not_a_fragment_anyone_negotiates(self) -> None:
        assert CaptureStateFragment not in ObservationRegistrar.get_all().values()

    def test_a_concrete_fragment_registers_under_its_capabilitys_prefix(
        self, stub: type[CaptureStateFragment]
    ) -> None:
        assert ObservationRegistrar.get("stub.capture_state") is stub
        fragment = stub(
            pointer=PointerState(status="unavailable"), failed_visible_resources=UNKNOWN_PROBE
        )
        assert fragment.fragment_id == "stub.capture_state"

    def test_pointer_and_resources_are_required(self, stub: type[CaptureStateFragment]) -> None:
        with pytest.raises(ValidationError):
            stub()

    def test_a_subclass_that_projects_nothing_says_so(self) -> None:
        class Incomplete(CaptureStateFragment):
            __registry_key__ = "incomplete.capture_state"

        fragment = Incomplete(
            pointer=PointerState(status="unknown"), failed_visible_resources=UNKNOWN_PROBE
        )
        with pytest.raises(NotImplementedError, match="Incomplete must provide settle_state"):
            _ = fragment.settle_state
        with pytest.raises(NotImplementedError, match="foreground_change"):
            _ = fragment.foreground_change


# ── the summary ───────────────────────────────────────────────────────


class TestSummary:
    def test_every_fact(self, stub: type[CaptureStateFragment]) -> None:
        fragment = stub(
            settled=SettleState(outcome="settled", waited_ms=640, loaded=True),
            pointer=PointerState(status="known", x=12, y=40),
            failed_visible_resources=FailedVisibleResources(
                status="measured", count=1, identifiers=("missing.png",)
            ),
        )
        assert fragment.summary() == "settled 640ms; pointer (12, 40); 1 failed visible resource"

    def test_a_capped_wait_and_a_followed_surface(self, stub: type[CaptureStateFragment]) -> None:
        fragment = stub(
            settled=SettleState(outcome="capped", waited_ms=5000, loaded=True, pending=("dom",)),
            change=ForegroundChange(reason="opened", from_surface="T1", to_surface="T2"),
            pointer=PointerState(status="unknown"),
            failed_visible_resources=FailedVisibleResources(status="measured", count=2),
        )
        assert fragment.summary() == (
            "capped 5000ms; foreground opened; pointer unknown; 2 failed visible resources"
        )

    def test_no_wait_is_left_out_and_zero_is_said(self, stub: type[CaptureStateFragment]) -> None:
        fragment = stub(
            pointer=PointerState(status="unavailable"),
            failed_visible_resources=FailedVisibleResources(status="measured", count=0),
        )
        assert fragment.summary() == "pointer unavailable; 0 failed visible resources"

    @pytest.mark.parametrize("status", ["unknown", "unavailable"])
    def test_an_unmeasured_count_is_never_written_as_zero(
        self, stub: type[CaptureStateFragment], status: Any
    ) -> None:
        fragment = stub(
            change=ForegroundChange(reason="closed", from_surface="T2", to_surface="T1"),
            pointer=PointerState(status="unknown"),
            failed_visible_resources=FailedVisibleResources(status=status),
        )
        summary = fragment.summary()
        assert summary == f"foreground closed; pointer unknown; failed visible resources {status}"
        assert "0" not in summary

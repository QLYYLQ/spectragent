"""A recorded restoration evidence reads back as its capability's type, through the capability registry.

Each capability that implements ``RestorationSupport`` defines its own
evidence model and names it on its class (``RestorationEvidence``). A
restoration check writes it whole; the trace reader looks its
``capability_id`` up in the existing capability registry and validates it
with that class's model, the way an action's ``Effect`` is read back
(``docs/architecture_design.md`` §4.5, §4.9). Nothing reads back as a dict or
as the base: a key no capability answers to, a capability without an
evidence model and a payload the model refuses are each a ``TraceSchemaError``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ValidationError

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.trace.effects import (
    registered_restoration_evidence,
    restore_restoration_evidence,
)
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation.fragments import HistoryObs
from spectragent.schemas.restoration import RestorationCheck, RestorationEvidence
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import fragment, new_wal


class ProbeEvidence(RestorationEvidence):
    """A test capability's evidence: one distance, one check on it against a recorded limit."""

    REASONS: ClassVar[tuple[str, ...]] = ("too_far",)

    distance: int
    max_distance: int = 3

    def failed_checks(self) -> list[str]:
        return ["too_far"] if self.distance > self.max_distance else []


def _register_probe_capabilities() -> None:
    """A capability naming its evidence, and one naming none (registered per test)."""

    class ProbeCapability(CapabilityBase):
        __registry_key__ = "restore_probe"
        RestorationEvidence = ProbeEvidence

        def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
            return []

        async def execute_action(self, action: Action) -> ActionResult:
            raise AssertionError("never executed")

    class SilentCapability(CapabilityBase):
        __registry_key__ = "restore_probe_silent"

        def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
            return []

        async def execute_action(self, action: Action) -> ActionResult:
            raise AssertionError("never executed")


def _check(distance: int) -> RestorationCheck:
    evidence = ProbeEvidence(capability_id="restore_probe", distance=distance)
    reasons = evidence.failed_checks()
    return RestorationCheck(
        step=0,
        verdict="warn" if reasons else "ok",
        reason=reasons[0] if reasons else None,
        reasons=reasons,
        evidence=[evidence],
    )


def _write(tmp_path: Path, check: RestorationCheck) -> Path:
    record = StepRecord(step=0, origin="restored", restoration=check)
    wal = new_wal(tmp_path)
    wal.commit_step(0, record, fragments=[fragment(0, HistoryObs(items=[record]))])
    return wal.run_dir


def test_evidence_round_trips_through_the_trace_as_its_type(tmp_path: Path) -> None:
    _register_probe_capabilities()
    check = _check(distance=7)
    trace = RunTrace(_write(tmp_path, check))
    step = trace.step(0)
    assert step is not None and step.restoration is not None and step.restoration == check
    (evidence,) = step.restoration.evidence
    assert isinstance(evidence, ProbeEvidence)
    assert evidence.failed_checks() == ["too_far"] == step.restoration.reasons
    history = trace.observation_fragment(0, "loop.history")
    assert isinstance(history, HistoryObs)
    restoration = history.items[0].restoration
    assert restoration is not None and isinstance(restoration.evidence[0], ProbeEvidence)


def test_the_evidence_is_written_whole(tmp_path: Path) -> None:
    _register_probe_capabilities()
    run_dir = _write(tmp_path, _check(distance=1))
    written = json.loads((run_dir / "steps" / "000.json").read_text())
    assert written["restoration"]["evidence"] == [
        {"capability_id": "restore_probe", "distance": 1, "max_distance": 3}
    ]


def _corrupt(run_dir: Path, mutate: Any) -> None:
    path = run_dir / "steps" / "000.json"
    document = json.loads(path.read_text())
    mutate(document["restoration"]["evidence"][0])
    path.write_text(json.dumps(document))


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda e: e.update(capability_id="restore_nowhere"), "names no registered capability"),
        (
            lambda e: e.update(capability_id="restore_probe_silent"),
            "declares no RestorationEvidence",
        ),
        (lambda e: e.update(distance="far"), "ProbeEvidence"),
        (lambda e: e.update(surprise=1), "surprise"),
        (lambda e: e.pop("capability_id"), "no capability_id"),
    ],
)
def test_evidence_that_does_not_read_back_is_a_trace_schema_error(
    tmp_path: Path, mutate: Any, match: str
) -> None:
    _register_probe_capabilities()
    run_dir = _write(tmp_path, _check(distance=1))
    _corrupt(run_dir, mutate)
    with pytest.raises(TraceSchemaError, match=match) as caught:
        RunTrace(run_dir).step(0)
    assert "000.json" in str(caught.value)


def test_a_mapping_is_never_accepted_as_evidence() -> None:
    with pytest.raises(ValidationError, match="RestorationEvidence instances"):
        RestorationCheck.model_validate(
            {
                "step": 0,
                "verdict": "ok",
                "evidence": [{"capability_id": "restore_probe", "distance": 1}],
            }
        )


def test_the_resolver_finds_a_capabilitys_evidence_by_its_key() -> None:
    _register_probe_capabilities()
    assert registered_restoration_evidence("restore_probe") is ProbeEvidence
    with pytest.raises(ValueError, match="declares no RestorationEvidence"):
        registered_restoration_evidence("restore_probe_silent")


def test_restoring_walks_the_schema_not_the_key_names() -> None:
    """A mapping under a field that cannot hold evidence is left for the model to judge."""

    class Elsewhere(BaseModel):
        evidence: list[dict[str, Any]]
        check: RestorationCheck | None = None

    data = {"evidence": [{"capability_id": "restore_probe", "distance": 1}], "check": None}
    assert restore_restoration_evidence(Elsewhere, data, registered_restoration_evidence) == data

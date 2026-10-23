"""Restoration contracts: StepRecord origins, checks, checkpoints, provenance, TaskContext.

``docs/architecture_design.md`` §5.3 ("步的来源与恢复校验", ``TraceConfig.branch``)
and §4.10 (``TaskContext``).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from spectragent.schemas import (
    REASON_ORDER,
    Action,
    BranchProvenance,
    Checkpoint,
    RestorationCheck,
    RestorationEvidence,
    RestoredAction,
    Retarget,
    StepRecord,
    TaskContext,
)


def _check(**overrides: object) -> RestorationCheck:
    fields: dict[str, object] = {
        "step": 2,
        "verdict": "warn",
        "reason": "url_mismatch",
        "reasons": ["url_mismatch", "screenshot_distance"],
        "evidence": [
            RestorationEvidence(
                capability_id="browser",
                url_match=False,
                recorded_url="https://a.test/",
                restored_url="https://a.test/other",
                signature_match=True,
                screenshot_distance=9,
                document_misses=0,
            )
        ],
        "actions": [
            RestoredAction(
                recorded=Action(name="browser.click", params={"index": 7}),
                executed=Action(name="browser.click", params={"index": 41}),
                method="geometry_signature",
                detail="1 candidate",
                recorded_success=True,
                live_success=True,
            )
        ],
    }
    fields.update(overrides)
    return RestorationCheck.model_validate(fields)


class TestStepRecordOrigin:
    def test_old_step_reads_policy(self) -> None:
        """A step written before the fields existed reads back as a policy step."""
        old = {
            "step": 3,
            "observation_summary": "dom",
            "llm_output": "click",
            "actions": [{"name": "browser.click", "params": {"index": 1}}],
            "action_results": [{"success": True}],
            "timestamp": 1.0,
        }
        record = StepRecord.model_validate_json(json.dumps(old))
        assert record.origin == "policy"
        assert record.restoration is None

    def test_restored_step_round_trips(self) -> None:
        record = StepRecord(step=2, origin="restored", restoration=_check())
        back = StepRecord.model_validate_json(record.model_dump_json())
        assert back == record
        assert back.restoration is not None
        assert back.restoration.actions[0].executed == Action(
            name="browser.click", params={"index": 41}
        )

    def test_unknown_origin_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(step=0, origin="replayed")  # type: ignore[arg-type]


class TestRestorationCheck:
    def test_reason_order_is_the_literal_order(self) -> None:
        assert REASON_ORDER == (
            "document_miss",
            "url_mismatch",
            "retarget_not_found",
            "retarget_ambiguous",
            "action_failed",
            "signature_mismatch",
            "media_mismatch",
            "screenshot_distance",
        )

    def test_ok_has_no_reasons(self) -> None:
        check = RestorationCheck(step=0, verdict="ok")
        assert check.reason is None
        assert check.reasons == []

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"verdict": "ok"}, id="ok-with-reasons"),
            pytest.param({"reason": "screenshot_distance"}, id="reason-not-first"),
            pytest.param(
                {"reasons": ["screenshot_distance", "url_mismatch"]}, id="reasons-out-of-order"
            ),
            pytest.param({"reasons": ["url_mismatch", "url_mismatch"]}, id="reasons-repeated"),
        ],
    )
    def test_inconsistent_checks_are_refused(self, overrides: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            _check(**overrides)

    def test_warn_without_reasons_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RestorationCheck(step=0, verdict="warn")

    def test_evidence_measures_default_to_unmeasured(self) -> None:
        evidence = RestorationEvidence(capability_id="browser")
        assert evidence.url_match is None
        assert evidence.signature_match is None
        assert evidence.screenshot_distance is None
        assert evidence.document_misses is None
        assert (evidence.signature_missing, evidence.signature_extra) == (0, 0)
        assert (evidence.url_ignored_query, evidence.url_rule_ids) == ([], [])

    def test_the_url_parameters_left_out_round_trip_and_older_evidence_has_none(self) -> None:
        evidence = RestorationEvidence(
            capability_id="browser",
            url_match=True,
            recorded_url="https://a.test/all?vt=1",
            restored_url="https://a.test/all?vt=2",
            url_ignored_query=["vt"],
            url_rule_ids=["a-vt"],
        )
        assert RestorationEvidence.model_validate_json(evidence.model_dump_json()) == evidence
        older = RestorationEvidence.model_validate({"capability_id": "browser", "url_match": False})
        assert (older.url_ignored_query, older.url_rule_ids) == ([], [])


class TestRetarget:
    def test_an_action_without_failure(self) -> None:
        retarget = Retarget(action=Action(name="browser.click"), method="identity")
        assert retarget.failure is None

    def test_a_failure_without_action(self) -> None:
        retarget = Retarget(action=None, method="geometry_signature", failure="ambiguous")
        assert retarget.action is None

    @pytest.mark.parametrize(
        ("action", "failure"),
        [
            pytest.param(None, None, id="neither"),
            pytest.param(Action(name="browser.click"), "not_found", id="both"),
        ],
    )
    def test_exactly_one_of_action_and_failure(
        self, action: Action | None, failure: str | None
    ) -> None:
        with pytest.raises(ValidationError):
            Retarget(action=action, method="geometry_signature", failure=failure)  # type: ignore[arg-type]


class TestCheckpoint:
    def test_round_trip(self, tmp_path) -> None:  # noqa: ANN001
        checkpoint = Checkpoint(trace_dir=str(tmp_path), step=3)
        assert Checkpoint.model_validate_json(checkpoint.model_dump_json()) == checkpoint

    def test_relative_trace_dir_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="absolute"):
            Checkpoint(trace_dir="traces/run1", step=0)

    def test_negative_step_is_refused(self, tmp_path) -> None:  # noqa: ANN001
        with pytest.raises(ValidationError):
            Checkpoint(trace_dir=str(tmp_path), step=-1)

    def test_frozen(self, tmp_path) -> None:  # noqa: ANN001
        checkpoint = Checkpoint(trace_dir=str(tmp_path), step=0)
        with pytest.raises(ValidationError):
            checkpoint.step = 1  # type: ignore[misc]


class TestBranchProvenance:
    def test_round_trip(self) -> None:
        provenance = BranchProvenance(
            source_run_id="r1",
            source_trace="2026/r1",
            source_benchmark="online_mind2web",
            source_subset="All",
            source_task_id="t9",
            checkpoint_step=4,
            branch_index=1,
            branch_group_id="r1/s4",
        )
        back = BranchProvenance.model_validate_json(provenance.model_dump_json())
        assert back == provenance

    def test_has_no_cost_fields(self) -> None:
        """Tokens, never money: nothing here prices a branch."""
        names = set(BranchProvenance.model_fields)
        assert not {n for n in names if "cost" in n or "price" in n}


class TestTaskContext:
    def test_defaults_are_empty(self) -> None:
        context = TaskContext()
        assert context.env == {}
        assert context.loop == {}
        assert context.branch is None

    def test_defaults_are_not_shared(self) -> None:
        first, second = TaskContext(), TaskContext()
        first.env["start_url"] = "https://a.test/"
        assert second.env == {}

    def test_carries_a_checkpoint_in_the_loop_part(self, tmp_path) -> None:  # noqa: ANN001
        checkpoint = Checkpoint(trace_dir=str(tmp_path), step=1)
        context = TaskContext(env={"start_url": "https://a.test/"}, loop={"checkpoint": checkpoint})
        assert context.loop["checkpoint"] is checkpoint

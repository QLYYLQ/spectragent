"""Restoration contracts: StepRecord origins, checks, checkpoints, provenance, TaskContext.

``docs/architecture_design.md`` §5.3 ("步的来源与恢复校验", ``TraceConfig.branch``)
and §4.10 (``TaskContext``). The framework holds the generic shapes only: each
capability's evidence is its own subclass with its own checks (a test type
here; the browser's in ``test_browser_restoration_evidence.py``), and the
framework's own reasons are the action ones.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from pydantic import ValidationError

from spectragent.schemas import (
    ACTION_REASONS,
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
from spectragent.schemas.benchmark_spec import BenchmarkSpec
from spectragent.schemas.restoration import IDENTITY, ordered_reasons


class _PageEvidence(RestorationEvidence):
    """A capability's evidence with two checks, failing the ones ``failing`` names."""

    REASONS: ClassVar[tuple[str, ...]] = ("url_mismatch", "screenshot_distance")

    failing: tuple[str, ...] = ()

    def failed_checks(self) -> list[str]:
        return [reason for reason in self.REASONS if reason in self.failing]


class _OtherEvidence(_PageEvidence):
    """A second capability's evidence, sharing one check name with the first."""

    REASONS: ClassVar[tuple[str, ...]] = ("app_mismatch", "screenshot_distance")


def _check(**overrides: object) -> RestorationCheck:
    fields: dict[str, object] = {
        "step": 2,
        "verdict": "warn",
        "reason": "url_mismatch",
        "reasons": ["url_mismatch", "screenshot_distance"],
        "evidence": [
            _PageEvidence(capability_id="page", failing=("url_mismatch", "screenshot_distance"))
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
        """Through its JSON and the typed evidence restore the trace reader runs."""
        from spectragent.core.trace.effects import restore_restoration_evidence

        record = StepRecord(step=2, origin="restored", restoration=_check())
        written = json.loads(record.model_dump_json())
        assert written["restoration"]["evidence"][0]["failing"] == [
            "url_mismatch",
            "screenshot_distance",
        ], "the evidence is written whole, as its own type"
        data = restore_restoration_evidence(StepRecord, written, lambda _key: _PageEvidence)
        back = StepRecord.model_validate(data)
        assert back == record
        assert back.restoration is not None
        assert back.restoration.actions[0].executed == Action(
            name="browser.click", params={"index": 41}
        )

    def test_unknown_origin_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(step=0, origin="replayed")  # type: ignore[arg-type]


class TestRestorationCheck:
    def test_the_frameworks_own_reasons_are_the_action_ones(self) -> None:
        assert ACTION_REASONS == ("retarget_not_found", "retarget_ambiguous", "action_failed")

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
            pytest.param(
                {"reason": "app_mismatch", "reasons": ["app_mismatch"]},
                id="a-check-no-evidence-here-declares",
            ),
            pytest.param(
                {"reason": "action_failed", "reasons": ["action_failed", "url_mismatch"]},
                id="an-action-reason-before-the-observations",
            ),
        ],
    )
    def test_inconsistent_checks_are_refused(self, overrides: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            _check(**overrides)

    def test_warn_without_reasons_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RestorationCheck(step=0, verdict="warn")

    def test_the_observations_checks_come_first_in_evidence_order_then_the_actions(self) -> None:
        evidence = [_OtherEvidence(capability_id="other"), _PageEvidence(capability_id="page")]
        failed = {"action_failed", "url_mismatch", "screenshot_distance", "app_mismatch"}
        assert ordered_reasons(failed, evidence) == [
            "app_mismatch",
            "screenshot_distance",
            "url_mismatch",
            "action_failed",
        ]
        with pytest.raises(ValueError, match="no_such_check"):
            ordered_reasons({"no_such_check"}, evidence)

    def test_evidence_is_its_capabilitys_own_type(self) -> None:
        with pytest.raises(ValidationError, match="RestorationEvidence instances"):
            _check(evidence=[{"capability_id": "page", "failing": ["url_mismatch"]}])
        with pytest.raises(ValidationError, match="surprise"):
            _PageEvidence.model_validate({"capability_id": "page", "surprise": 1})

    def test_the_base_names_no_check(self) -> None:
        evidence = RestorationEvidence(capability_id="page")
        assert RestorationEvidence.REASONS == ()
        with pytest.raises(NotImplementedError):
            evidence.failed_checks()


class TestRetarget:
    def test_an_action_without_failure(self) -> None:
        retarget = Retarget(action=Action(name="browser.click"), method=IDENTITY)
        assert retarget.failure is None

    def test_the_method_is_the_capabilitys_own_word(self) -> None:
        """Only ``identity`` is the framework's; a capability names its own ways."""
        retarget = Retarget(action=Action(name="android.tap"), method="ui_tree_attributes")
        assert retarget.method == "ui_tree_attributes"
        with pytest.raises(ValidationError):
            Retarget(action=Action(name="android.tap"), method="")

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

    def test_only_a_branch_task_names_the_benchmark_it_belongs_to(self) -> None:
        """Every other task belongs to the benchmark whose task source gave it (§4.10.1)."""
        spec = BenchmarkSpec(benchmark_id="androidworld", selection="androidworld")
        branch = BranchProvenance(
            source_run_id="run-a",
            source_trace="run-a",
            checkpoint_step=1,
            branch_index=0,
            branch_group_id="run-a/s1",
        )
        assert TaskContext(branch=branch, benchmark=spec).benchmark == spec
        assert TaskContext(branch=branch).benchmark is None, "a branch of a run of no benchmark"
        with pytest.raises(ValidationError, match="a task with no branch belongs to the benchmark"):
            TaskContext(benchmark=spec)

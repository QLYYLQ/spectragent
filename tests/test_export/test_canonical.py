"""Canonical samples from WAL-built traces (arch §4.15 "可导出的步与 CanonicalStepSample")."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.core.loops.validation import IncompatibleError
from spectragent.core.trace.reader import RunTrace
from spectragent.export import canonicalise
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.restoration import BranchProvenance
from spectragent.schemas.sample import DroppedParams, ThoughtRecord
from tests._helpers.export_traces import ExportStep, screenshot, setup_record, write_run
from tests._helpers.trace_builder import eval_result


def _click(index: int, **extra: object) -> Action:
    return Action(name="browser.click", params={"index": index, **extra})


def _samples(run_dir: Path, **kwargs: object):
    return canonicalise(RunTrace(run_dir), rel_path=f"recorded/{run_dir.name}", **kwargs)  # type: ignore[arg-type]


class TestExportableSteps:
    def test_only_healthy_policy_steps_that_acted(self, tmp_path: Path) -> None:
        run = write_run(
            tmp_path,
            "run-a",
            [
                ExportStep(actions=[_click(1)]),  # 0: a sample
                ExportStep(actions=[_click(2)], error_phase="act"),  # 1: failed
                ExportStep(actions=[]),  # 2: parsed to nothing
                ExportStep(actions=[_click(3)], results=[]),  # 3: nothing executed
                ExportStep(actions=[_click(4)]),  # 4: a sample
                ExportStep(actions=[], origin="terminal"),  # 5: the terminal observation
            ],
        )
        samples = _samples(run)
        assert [s.step for s in samples] == [0, 4]
        assert [s.sample_id for s in samples] == ["run-a/s0", "run-a/s4"]
        assert samples[0].source_trace == "recorded/run-a"
        assert samples[1].history_steps == [0]

    def test_parse_failure_and_successful_noop_are_distinct_and_both_excluded(
        self, tmp_path: Path
    ) -> None:
        run = write_run(
            tmp_path,
            "parse-and-noop",
            [
                ExportStep(actions=[], llm_output="malformed reply", error_phase="parse"),
                ExportStep(actions=[], llm_output='{"actions": []}'),
                ExportStep(actions=[_click(1)]),
            ],
        )
        trace = RunTrace(run)
        failed, noop, _acted = trace.step_records
        assert failed.error_phase == "parse"
        assert noop.error_phase == ""
        assert failed.actions == noop.actions == []
        samples = canonicalise(trace, rel_path="recorded/parse-and-noop")
        assert [sample.step for sample in samples] == [2]
        assert samples[0].history_steps == []

    def test_an_orphan_step_file_is_not_a_step(self, tmp_path: Path) -> None:
        run = write_run(tmp_path, "run-b", [ExportStep(actions=[_click(1)])])
        # What a crash between the step file and the manifest leaves behind.
        orphan = json.loads((run / "steps" / "000.json").read_text())
        orphan["step"] = 1
        (run / "steps" / "001.json").write_text(json.dumps(orphan))
        assert [s.step for s in _samples(run)] == [0]

    def test_only_executed_actions_are_targets(self, tmp_path: Path) -> None:
        run = write_run(
            tmp_path,
            "run-c",
            [
                ExportStep(
                    actions=[_click(1), _click(2), _click(3)],
                    results=[ActionResult(success=True), ActionResult(success=False)],
                )
            ],
        )
        (sample,) = _samples(run)
        assert [a.params["index"] for a in sample.teacher_actions] == [1, 2]
        assert sample.labels.step_status == "action_failed"
        assert sample.actions is None and sample.untranslatable is None


class TestTeacherActions:
    def test_undeclared_params_are_dropped_and_named(self, tmp_path: Path) -> None:
        run = write_run(
            tmp_path,
            "run-d",
            [
                ExportStep(
                    actions=[
                        _click(7, reasoning="the cart link"),
                        Action(name="browser.scroll", params={"direction": "down"}),
                    ]
                )
            ],
            agent_id="skyvern",
        )
        (sample,) = _samples(run)
        assert sample.teacher_actions == [
            Action(name="browser.click", params={"index": 7}),
            Action(name="browser.scroll", params={"direction": "down"}),
        ]
        assert sample.dropped_params == [DroppedParams(action_index=0, params=["reasoning"])]


class TestThought:
    def test_teacher_thought_and_its_field(self, tmp_path: Path) -> None:
        reply = json.dumps({"thinking": "", "next_goal": "open the cart", "memory": "m"})
        run = write_run(tmp_path, "run-e", [ExportStep(actions=[_click(1)], llm_output=reply)])
        (sample,) = _samples(run, thought_source="teacher", teacher_cls=BrowserUseAgent)
        assert sample.thought == ThoughtRecord(text="open the cart", source_field="next_goal")
        (none,) = _samples(run, thought_source="none", teacher_cls=BrowserUseAgent)
        assert none.thought is None

    def test_skyvern_reasonings(self, tmp_path: Path) -> None:
        reply = json.dumps(
            {"actions": [{"action_type": "browser.click", "index": 1, "reasoning": "cart"}]}
        )
        run = write_run(
            tmp_path,
            "run-f",
            [ExportStep(actions=[_click(1, reasoning="cart")], llm_output=reply)],
            agent_id="skyvern",
        )
        (sample,) = _samples(run, thought_source="teacher", teacher_cls=SkyvernAgent)
        assert sample.thought == ThoughtRecord(text="cart", source_field="reasoning")

    def test_a_teacher_without_the_slot_is_named_as_the_exports_need(self, tmp_path: Path) -> None:
        run = write_run(tmp_path, "run-g", [ExportStep(actions=[_click(1)])])

        class Mute:
            pass

        with pytest.raises(IncompatibleError) as excinfo:
            _samples(run, thought_source="teacher", teacher_cls=Mute)
        assert excinfo.value.missing_slots == ["parse_react_thought"]
        assert "needed for training export (optional slots of loop 'react')" in str(excinfo.value)


class TestReferencesAndLabels:
    def test_environment_fragments_and_the_image(self, tmp_path: Path) -> None:
        shot = screenshot(dpr=2)
        run = write_run(
            tmp_path,
            "run-h",
            [ExportStep(actions=[_click(1)], fragments=[shot], failed=["browser.dom"])],
        )
        (sample,) = _samples(run)
        assert [(ref.key, ref.failed) for ref in sample.observations] == [
            ("browser.capture_state", False),
            ("browser.dom", True),
            ("browser.platform", False),
            ("browser.screenshot", False),
        ]  # loop-sourced fragments are not observations
        assert shot.image_bytes is not None
        digest = hashlib.sha256(shot.image_bytes).hexdigest()
        screenshot_ref = next(ref for ref in sample.observations if ref.key == "browser.screenshot")
        assert screenshot_ref.artifact_sha256 == digest
        assert sample.image is not None
        assert sample.image.model_dump() == {
            "fragment": "browser.screenshot",
            "sha256": digest,
            "media_type": "image/png",
            "image_width": 2560,
            "image_height": 1440,
            "viewport_width": 1280,
            "viewport_height": 720,
        }

    def test_graded_verdicts_in_slug_order_and_the_redaction_ledger(self, tmp_path: Path) -> None:
        run = write_run(
            tmp_path,
            "run-i",
            [ExportStep(actions=[_click(1)])],
            task_setup=setup_record(redacted=True),
            verdicts={
                "z-judge": eval_result(evaluator_id="online_mind2web", predicted_label=0),
                "a-judge": eval_result(evaluator_id="online_mind2web", predicted_label=1),
                "skipped": eval_result(evaluator_id="exact_match", verdict_source="not_evaluated"),
            },
        )
        (sample,) = _samples(run)
        assert [(v.verdict_slug, v.predicted_label) for v in sample.labels.verdicts] == [
            ("a-judge", 1),
            ("z-judge", 0),
        ]
        assert sample.labels.step_status == "ok"
        assert sample.labels.loss_weight == 1.0
        assert [r.model_dump() for r in sample.redactions] == [
            {
                "policy": "af-privacy-1",
                "location": "/start_url",
                "field": "token",
                "rule": "url_query",
            }
        ]
        assert sample.task == "Put the blue kettle in the cart"
        assert (sample.benchmark, sample.subset, sample.task_id) == (
            "online_mind2web",
            "test",
            "task-001",
        )
        assert sample.config_hash == RunTrace(run).manifest.config_hash


class TestBranchSamples:
    def test_policy_steps_only_with_branch_meta_and_the_terminal_ref(self, tmp_path: Path) -> None:
        source = write_run(
            tmp_path,
            "src",
            [ExportStep(actions=[_click(i)]) for i in range(3)],
            verdicts={"judge": eval_result(evaluator_id="online_mind2web", predicted_label=1)},
        )
        terminal_shot = screenshot(color=10)
        branch = write_run(
            tmp_path,
            "src-s2-b1",
            [
                ExportStep(actions=[_click(0)], origin="restored"),
                ExportStep(actions=[_click(1)], origin="restored"),
                ExportStep(actions=[_click(9)]),  # t = 2: the branch's own decision
                ExportStep(actions=[_click(8)]),  # a continuation step
                ExportStep(actions=[], origin="terminal", fragments=[terminal_shot]),
            ],
            branch=BranchProvenance(
                source_run_id="src",
                source_trace="recorded/src",
                checkpoint_step=2,
                branch_index=1,
                branch_group_id="src/s2",
            ),
        )
        samples = _samples(branch, verdicts_from=RunTrace(source))
        assert [s.step for s in samples] == [2, 3]
        assert [s.history_steps for s in samples] == [[0, 1], [0, 1, 2]]
        assert terminal_shot.image_bytes is not None
        for sample in samples:
            assert sample.branch is not None
            assert sample.branch.model_dump() == {
                "branch_group_id": "src/s2",
                "source_run_id": "src",
                "checkpoint_step": 2,
                "branch_index": 1,
                "terminal_step": 4,
                "terminal_image_sha256": hashlib.sha256(terminal_shot.image_bytes).hexdigest(),
                "duplicate_of": None,
            }
            assert [v.verdict_slug for v in sample.labels.verdicts] == ["judge"]
        assert _samples(branch)[0].labels.verdicts == []  # its own evals/: none

    def test_a_branch_without_a_terminal_step(self, tmp_path: Path) -> None:
        branch = write_run(
            tmp_path,
            "b",
            [ExportStep(actions=[_click(0)], origin="restored"), ExportStep(actions=[_click(1)])],
            branch=BranchProvenance(
                source_run_id="s",
                source_trace="s",
                checkpoint_step=1,
                branch_index=0,
                branch_group_id="s/s1",
            ),
        )
        (sample,) = _samples(branch)
        assert sample.branch is not None
        assert (sample.branch.terminal_step, sample.branch.terminal_image_sha256) == (None, None)


def test_a_copied_run_canonicalises_identically(tmp_path: Path) -> None:
    """Nothing read depends on where the run lives or when it is read."""
    run = write_run(tmp_path / "a", "run-j", [ExportStep(actions=[_click(1)])])
    copy = tmp_path / "b" / "run-j"
    shutil.copytree(run, copy)
    assert [s.canonical_json() for s in _samples(run)] == [
        s.canonical_json() for s in _samples(copy)
    ]

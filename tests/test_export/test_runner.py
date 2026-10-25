"""``ExportRunner``: sources in, a validated dataset directory out (arch §4.15 "流水线", "确定性")."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.composer import ConfigComposer
from spectragent.core.trace.digest import tree_digest
from spectragent.export.benchmarks import BenchmarkLoaders
from spectragent.export.errors import (
    ExportConfigError,
    ExportEmptyError,
    ExportValidationError,
    StudentCaptureMismatchError,
)
from spectragent.export.runner import ExportRunner
from spectragent.schemas.action import Action
from spectragent.schemas.dataset import DATASET_FORMAT, DatasetManifest, StepLedger
from tests._helpers.export_runs import exporter_entry, read_jsonl, run_export
from tests._helpers.export_traces import (
    NEVER_QUIET,
    NOT_LOADED,
    ExportStep,
    capture_state,
    click_at,
    finished,
    screenshot,
    ui_tars_step,
    write_ui_tars_run,
)
from tests._helpers.trace_builder import eval_result


def _write_sources(root: Path, order: list[str]) -> None:
    """Three runs, created in *order*: a filesystem order that must not matter."""
    plans = {
        "run-a": [
            ui_tars_step(click_at(640, 360), color=11),
            ui_tars_step(finished("ok"), color=12),
        ],
        "run-b": [ui_tars_step(click_at(7, 700), thought="a lossy x", color=21)],
        "run-c": [
            ui_tars_step(click_at(100, 100), color=31),
            ExportStep(actions=[]),  # parsed to nothing: ends the segment
            ui_tars_step(click_at(200, 200), color=33),
        ],
    }
    for run_id in order:
        write_ui_tars_run(root, run_id, plans[run_id], task_id=run_id)


def _captured(action: Action, settle: Any, *, color: int) -> ExportStep:
    """A UI-TARS step whose capture recorded *settle* (``ui_tars_step`` records a settled one)."""
    return ExportStep(
        actions=[action],
        llm_output="Thought: look\nAction: (recorded reply)",
        fragments=[screenshot(color=color), capture_state(settle)],
    )


class TestUnloadedSteps:
    """``filters.exclude_unloaded_steps`` (arch §4.15, §4.4 "初次加载是否完成")."""

    def _source(self, root: Path, middle: ExportStep) -> None:
        write_ui_tars_run(
            root,
            "run-a",
            [ui_tars_step(click_at(100, 100), color=11), middle, ui_tars_step(click_at(300, 300))],
            task_id="run-a",
        )

    def _export(self, composer: ConfigComposer, root: Path, out: Path, **filters: Any) -> Any:
        entry = exporter_entry(
            root,
            out,
            split={"validation_fraction": 0.0},
            filters={"max_document_misses": "unlimited", **filters},
        )
        return run_export(composer, entry)

    def test_an_unloaded_step_is_no_sample_but_stays_in_later_context(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        self._source(tmp_path / "src", _captured(click_at(200, 200), NOT_LOADED, color=12))
        out = tmp_path / "out"
        report = self._export(composer, tmp_path / "src", out)
        lines = read_jsonl(out / "train.samples.jsonl")
        assert [line["sample"]["sample_id"] for line in lines] == ["run-a/s0", "run-a/s2"]
        last = lines[-1]["render"]
        assert last["segment"] == 0 and last["context_steps"] == [0, 1], (
            "the left-out step is in the context as it happened; the segment goes on"
        )
        assert report.manifest.steps.filtered == {
            "run-a#s1": (
                "step 1 was captured before its initial load finished (browser.capture_state: "
                "the wait ended capped after 5002 ms) (exclude_unloaded_steps)"
            )
        }
        assert report.manifest.steps.skipped == {} and report.manifest.steps.untranslatable == {}
        assert report.manifest.exporter["filters"]["exclude_unloaded_steps"] is True

    def test_the_context_turn_says_the_page_had_not_loaded(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        """Rendered by the student as the step was: its capture state opens with the fact."""
        self._source(tmp_path / "src", _captured(click_at(200, 200), NOT_LOADED, color=12))
        out = tmp_path / "out"
        self._export(composer, tmp_path / "src", out)
        (_first, last) = read_jsonl(out / "train.jsonl")
        texts = [
            part["text"]
            for message in last["messages"]
            if message["role"] == "user"
            for part in (
                message["content"]
                if isinstance(message["content"], list)
                else [{"type": "text", "text": message["content"]}]
            )
            if part.get("type") == "text"
        ]
        assert sum("had not finished loading" in text for text in texts) == 1

    def test_off_every_step_is_a_sample(self, composer: ConfigComposer, tmp_path: Path) -> None:
        self._source(tmp_path / "src", _captured(click_at(200, 200), NOT_LOADED, color=12))
        out = tmp_path / "out"
        report = self._export(composer, tmp_path / "src", out, exclude_unloaded_steps=False)
        ids = [line["id"] for line in read_jsonl(out / "train.jsonl")]
        assert ids == ["run-a/s0", "run-a/s1", "run-a/s2"]
        assert report.manifest.steps.filtered == {}

    @pytest.mark.parametrize(
        "middle",
        [
            _captured(click_at(200, 200), NEVER_QUIET, color=12),
            _captured(click_at(200, 200), None, color=12),
            ExportStep(
                actions=[click_at(200, 200)],
                llm_output="Thought: look\nAction: (recorded reply)",
                fragments=[screenshot(color=12)],
                failed=["browser.capture_state"],
            ),
        ],
        ids=["a DOM that never went quiet", "no wait", "capture state failed"],
    )
    def test_no_evidence_of_an_unloaded_page_is_exported(
        self, composer: ConfigComposer, tmp_path: Path, middle: ExportStep
    ) -> None:
        self._source(tmp_path / "src", middle)
        out = tmp_path / "out"
        report = self._export(composer, tmp_path / "src", out)
        ids = [line["id"] for line in read_jsonl(out / "train.jsonl")]
        assert ids == ["run-a/s0", "run-a/s1", "run-a/s2"]
        assert report.manifest.steps.filtered == {}

    def test_an_export_of_only_unloaded_steps_is_empty_and_says_why(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        write_ui_tars_run(
            tmp_path / "src",
            "run-a",
            [_captured(click_at(200, 200), NOT_LOADED, color=12)],
            task_id="run-a",
        )
        with pytest.raises(ExportEmptyError, match="steps filtered"):
            self._export(composer, tmp_path / "src", tmp_path / "out")


class TestDeterminism:
    def test_two_exports_have_identical_tree_hashes(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        _write_sources(tmp_path / "src1", ["run-c", "run-a", "run-b"])
        _write_sources(tmp_path / "src2", ["run-b", "run-c", "run-a"])
        first = run_export(composer, exporter_entry(tmp_path / "src1", tmp_path / "out1"))
        second = run_export(composer, exporter_entry(tmp_path / "src2", tmp_path / "out2"))
        assert tree_digest(tmp_path / "out1") == tree_digest(tmp_path / "out2")
        assert first.manifest == second.manifest
        assert sum(first.samples.values()) == 5

    def test_samples_are_ordered_by_source_path_then_step(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        _write_sources(tmp_path / "src", ["run-c", "run-b", "run-a"])
        out = tmp_path / "out"
        run_export(
            composer, exporter_entry(tmp_path / "src", out, split={"validation_fraction": 0.0})
        )
        ids = [line["id"] for line in read_jsonl(out / "train.jsonl")]
        assert ids == ["run-a/s0", "run-a/s1", "run-b/s0", "run-c/s0", "run-c/s2"]
        lines = read_jsonl(out / "train.samples.jsonl")
        assert [line["sample"]["sample_id"] for line in lines] == ids
        assert [line["render"]["segment"] for line in lines] == [0, 0, 0, 0, 1]
        assert not (out / "validation.jsonl").exists()

    def test_the_manifest_has_no_clock_no_absolute_path_no_machine(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        _write_sources(tmp_path / "src", ["run-a", "run-b", "run-c"])
        out = tmp_path / "out"
        report = run_export(composer, exporter_entry(tmp_path / "src", out))
        text = (out / "manifest.json").read_text()
        assert str(tmp_path) not in text and os.uname().nodename not in text
        assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text), "no timestamp"
        # Read back through the model every reader of a dataset uses.
        manifest = DatasetManifest.model_validate(json.loads(text))
        assert manifest == report.manifest
        assert manifest.format == DATASET_FORMAT
        assert manifest.student.agent_id == "ui_tars"
        assert manifest.student.coordinate_space == "normalized_1000"
        assert manifest.quantization_px == {"0": 3, "1": 1}
        assert manifest.steps == StepLedger(
            skipped={"not_exportable": 1}, untranslatable={}, filtered={}
        )
        assert manifest.step_info == {"recorded": 5}
        assert manifest.thoughts.fields == {"Thought": 5}
        assert [(s.path, s.samples) for s in manifest.sources.exported] == [
            ("run-a", 2),
            ("run-b", 1),
            ("run-c", 2),
        ]
        for path in sorted(out.glob("*.jsonl")):
            assert str(tmp_path) not in path.read_text()

    def test_an_unreadable_source_is_named_without_its_path(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        """A source that does not read back is skipped by its run-relative file, never its path.

        One run's manifest does not parse (found when the sources are opened),
        another's verdict does not (found by ``require_eval``): the manifest
        names both without the sources root, so two roots export to the same
        bytes.
        """
        passed = {"judge": eval_result(evaluator_id="online_mind2web")}
        reports = []
        for name in ("src1", "elsewhere/src2"):
            root = tmp_path / name
            write_ui_tars_run(
                root, "run-a", [ui_tars_step(click_at(640, 360))], task_id="a", verdicts=passed
            )
            broken = root / "run-broken"
            broken.mkdir()
            (broken / "manifest.json").write_text("{not json")
            damaged = write_ui_tars_run(
                root, "run-evals", [ui_tars_step(click_at(9, 9))], task_id="e", verdicts=passed
            )
            (damaged / "evals" / "judge.json").write_text("{not json")
            entry = exporter_entry(
                root,
                tmp_path / f"out-{len(reports)}",
                filters={
                    "require_eval": {"evaluator_id": "online_mind2web"},
                    "max_document_misses": "unlimited",
                },
            )
            reports.append(run_export(composer, entry))
        first, second = reports
        assert first.manifest.sources.skipped == {
            "run-broken": "not a readable trace: manifest.json does not read back "
            "(TraceSchemaError)",
            "run-evals": "not a readable trace: evals/judge.json does not read back "
            "(TraceSchemaError)",
        }
        text = (tmp_path / "out-0" / "manifest.json").read_text()
        assert str(tmp_path) not in text and "/private/" not in text
        assert first.manifest == second.manifest
        assert tree_digest(tmp_path / "out-0") == tree_digest(tmp_path / "out-1")


class TestFailClosed:
    def test_an_existing_output_dir_is_refused(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        _write_sources(tmp_path / "src", ["run-a"])
        (tmp_path / "out").mkdir()
        with pytest.raises(ExportConfigError, match="exists"):
            run_export(composer, exporter_entry(tmp_path / "src", tmp_path / "out"))

    def test_a_failure_leaves_no_partial_dataset(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        """A run whose last step fails a check: the images of earlier steps were stored, and go."""
        src = tmp_path / "src"
        _write_sources(src, ["run-a"])
        write_ui_tars_run(
            src,
            "run-z",
            [ExportStep(actions=[click_at(1, 1)], fragments=[screenshot(dpr=2)])],
        )
        with pytest.raises(StudentCaptureMismatchError):
            run_export(composer, exporter_entry(src, tmp_path / "out"))
        assert sorted(p.name for p in tmp_path.iterdir()) == ["src"]

    def test_a_validation_failure_after_images_were_stored_removes_them(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        src = tmp_path / "src"
        _write_sources(src, ["run-a"])
        entry = exporter_entry(src, tmp_path / "out", length={"max_tokens": 10})
        with pytest.raises(ExportValidationError, match="length"):
            run_export(composer, entry)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["src"]

    def test_a_stale_partial_is_replaced(self, composer: ConfigComposer, tmp_path: Path) -> None:
        _write_sources(tmp_path / "src", ["run-a"])
        stale = tmp_path / "out.partial"
        stale.mkdir()
        (stale / "leftover.txt").write_text("from a crash")
        run_export(composer, exporter_entry(tmp_path / "src", tmp_path / "out"))
        assert not stale.exists() and not (tmp_path / "out" / "leftover.txt").exists()

    def test_nothing_to_export_is_explained(self, composer: ConfigComposer, tmp_path: Path) -> None:
        src = tmp_path / "src"
        write_ui_tars_run(
            src,
            "nav",
            [ui_tars_step(Action(name="browser.navigate", params={"url": "https://x.test/"}))],
        )
        write_ui_tars_run(src, "secret", [ui_tars_step(click_at(1, 1))], secrets_recorded=True)
        with pytest.raises(ExportEmptyError) as excinfo:
            run_export(composer, exporter_entry(src, tmp_path / "out"))
        assert excinfo.value.counts == {
            "sources skipped": 1,
            "steps untranslatable (no_counterpart)": 1,
        }
        assert not (tmp_path / "out").exists()


def test_the_runner_reaches_strategies_only_through_the_exporter(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    """A hand-built exporter with a hand-built translator runs the same pipeline."""
    from spectragent.export import IndexToCoordinateTranslator, OpenAIMessagesExporter

    _write_sources(tmp_path / "src", ["run-a"])
    exporter = OpenAIMessagesExporter(
        student_run_config=exporter_entry(tmp_path, tmp_path)["student_run_config"],
        output_dir=str(tmp_path / "out"),
        sources_root=str(tmp_path / "src"),
        filters=None,
    )
    exporter.translator = IndexToCoordinateTranslator(occlusion="none")
    report = ExportRunner(exporter, composer=composer, benchmarks=BenchmarkLoaders()).run()
    assert report.manifest.translator == {
        "occlusion": "none",
        "occlusion_unknown": "raise",
        "translator_id": "index_to_coordinate",
        "untranslatable": "drop_step",
    }
    assert sum(report.samples.values()) == 2

"""The metrics chain: path fields only, stage outcomes, the stage selection, the spec (arch §4.16 "链").

The CLIs are replaced at the chain's one seam (``run_chain(bench=..., export=...)``)
by recorders that read the effective YAML the chain wrote and commit a run
where it says — so what is pinned here is what the chain hands each stage.
The real CLIs run in the rehearsal (``test_rehearsal_live.py``).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core.trace.models import RunStatus
from spectragent.flywheel.chain import (
    ChainConfigError,
    ChainSpec,
    ChainStageError,
    PlantedSecret,
    resolve_planted,
    run_chain,
    selected_stages,
)
from tests._helpers.flywheel_traces import dataset_manifest
from tests._helpers.trace_builder import new_wal

RUN_YAML: dict[str, Any] = {
    "agent": [{"agent_id": "ui_tars", "id": "main"}],
    "environment": [{"env_type": "browser", "id": "browser_env"}],
    "hook": [
        {"hook_id": "trace_writer", "trace_dir": "traces", "archive": []},
        {"hook_id": "metrics"},
    ],
}
BRANCH_YAML: dict[str, Any] = {
    **RUN_YAML,
    "task_loader": [
        {
            "benchmark_name": "trace_branches",
            "data_dir": "traces/recorded",
            "step_selector": {"selector_id": "last"},
            "branches_per_step": 2,
        }
    ],
}
EXPORT_YAML: dict[str, Any] = {
    "exporter": [
        {
            "exporter_id": "openai_messages",
            "sources_root": "traces",
            "sources": ["recorded/*"],
            "filters": {"require_eval": None},
            "student_run_config": "configs/flywheel/ui_tars_student.yaml",
            "output_dir": "datasets/sft_v1",
        }
    ]
}


def _write(path: Path, raw: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(raw))
    return path


def _spec(tmp_path: Path, **overrides: Any) -> ChainSpec:
    fields: dict[str, Any] = {
        "name": "test-chain",
        "root": tmp_path / "root",
        "evidence": "rehearsal",
        "tasks": {"benchmark": "online_mind2web", "subset": "All", "task_ids": ["a", "b"]},
        "record": {"config": _write(tmp_path / "record.yaml", RUN_YAML), "timeout": 60},
        "control": {"config": _write(tmp_path / "control.yaml", RUN_YAML)},
        "same_path": {"config": _write(tmp_path / "same_path.yaml", BRANCH_YAML)},
        "branch": {"config": _write(tmp_path / "branch.yaml", BRANCH_YAML)},
        "export": {"config": _write(tmp_path / "export.yaml", EXPORT_YAML), "repeat": 2},
        "report": {"planted": [{"label": "site password", "value": "pw-1"}]},
    }
    fields.update(overrides)
    return ChainSpec.model_validate(fields)


class Recorder:
    """A stand-in for one CLI: records each argv and acts on the YAML it names."""

    def __init__(self, act: Callable[[dict[str, Any], list[str]], int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.act = act

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        raw = yaml.safe_load(Path(argv[argv.index("--config") + 1]).read_text())
        return self.act(raw, argv) if self.act is not None else 0


def commit_one_run(raw: dict[str, Any], argv: list[str]) -> int:
    """What a sweep that ran one task does: one committed run under the YAML's trace_dir."""
    (writer,) = [hook for hook in raw["hook"] if hook["hook_id"] == "trace_writer"]
    wal = new_wal(Path(writer["trace_dir"]), f"run-{len(argv)}")
    wal.finalize(RunStatus.COMPLETED)
    return 0


def publish_dataset(raw: dict[str, Any], _argv: list[str]) -> int:
    output = Path(raw["exporter"][0]["output_dir"])
    output.mkdir(parents=True)
    (output / "manifest.json").write_text(dataset_manifest(samples=0).model_dump_json() + "\n")
    return 0


class TestThePaths:
    def test_only_the_path_fields_change(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        bench, export = Recorder(commit_one_run), Recorder(publish_dataset)
        result = run_chain(spec, bench=bench, export=export)
        root = result.layout.root
        assert root == (tmp_path / "root").resolve()
        for stage, source in (("record", RUN_YAML), ("control", RUN_YAML)):
            effective = yaml.safe_load((root / "configs" / f"{stage}.yaml").read_text())
            expected = copy.deepcopy(source)
            expected["hook"][0]["trace_dir"] = str(root / "traces" / stage)
            assert effective == expected, stage
        for stage in ("same_path", "branch"):
            effective = yaml.safe_load((root / "configs" / f"{stage}.yaml").read_text())
            expected = copy.deepcopy(BRANCH_YAML)
            expected["hook"][0]["trace_dir"] = str(root / "traces" / stage)
            expected["task_loader"][0]["data_dir"] = str(root / "traces" / "record")
            assert effective == expected, stage
        for dataset in ("first", "second"):
            effective = yaml.safe_load((root / "configs" / f"export_{dataset}.yaml").read_text())
            expected = copy.deepcopy(EXPORT_YAML)
            expected["exporter"][0].update(
                sources_root=str(root / "traces"),
                sources=["record/*", "branch/*"],
                output_dir=str(root / "datasets" / dataset),
            )
            assert effective == expected, dataset
        assert [call[call.index("--config") + 1] for call in export.calls] == [
            str(root / "configs" / "export_first.yaml"),
            str(root / "configs" / "export_second.yaml"),
        ]
        assert (root / "report" / "report.json").is_file() and result.report is not None

    def test_the_argv_of_each_run_stage(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, control={"config": tmp_path / "control.yaml", "evaluate": True})
        bench = Recorder(commit_one_run)
        run_chain(spec, bench=bench, export=Recorder(publish_dataset), log_level="WARNING")
        record, control, same_path, branch = bench.calls
        root = (tmp_path / "root").resolve()
        assert record == [
            "--config",
            str(root / "configs" / "record.yaml"),
            "--timeout",
            "60",
            "--log-level",
            "WARNING",
            "--benchmark",
            "online_mind2web",
            "--subset",
            "All",
            "--task-id",
            "a",
            "--task-id",
            "b",
            "--no-eval",
        ]
        assert control[-2:] == ["--evals-dir", str(root / "evals" / "control")]
        for call in (same_path, branch):
            assert "--no-eval" in call and "--benchmark" not in call and "--task-id" not in call

    @pytest.mark.parametrize(
        ("stage", "raw", "block"),
        [
            (
                "record",
                {**RUN_YAML, "hook": [{"hook_id": "metrics"}]},
                "'hook' block with hook_id: trace_writer",
            ),
            (
                "record",
                {**RUN_YAML, "hook": [RUN_YAML["hook"][0], RUN_YAML["hook"][0]]},
                "2 'hook' block",
            ),
            ("same_path", RUN_YAML, "'task_loader' block with benchmark_name: trace_branches"),
            ("export", {"exporter": []}, "0 'exporter' blocks"),
        ],
    )
    def test_a_missing_or_doubled_block_stops_everything_first(
        self, tmp_path: Path, stage: str, raw: dict[str, Any], block: str
    ) -> None:
        spec = _spec(tmp_path)
        _write(tmp_path / f"{stage}.yaml", raw)
        bench, export = Recorder(commit_one_run), Recorder(publish_dataset)
        with pytest.raises(ChainConfigError, match=f"stage '{stage}'.*{block}"):
            run_chain(spec, bench=bench, export=export)
        assert bench.calls == [] and export.calls == [], "no stage ran"
        assert not (tmp_path / "root" / "chain.json").exists()


class TestStageOutcomes:
    def test_a_task_that_failed_is_data(self, tmp_path: Path) -> None:
        def failed_task(raw: dict[str, Any], argv: list[str]) -> int:
            commit_one_run(raw, argv)
            return 1  # spectragent-bench: a task did not complete

        result = run_chain(
            _spec(tmp_path, control=None),
            bench=Recorder(failed_task),
            export=Recorder(publish_dataset),
        )
        assert [outcome.exit_codes for outcome in result.stages.values()][:3] == [[1], [1], [1]]
        assert result.report is not None

    @pytest.mark.parametrize(
        ("act", "message"),
        [
            (lambda raw, argv: 1, "exited 1 and wrote no run"),
            (lambda raw, argv: 2, "exited 2"),
            (
                lambda raw, argv: (_ for _ in ()).throw(SystemExit("No task_loader")),
                "No task_loader",
            ),
            (lambda raw, argv: (_ for _ in ()).throw(RuntimeError("boom")), "RuntimeError: boom"),
        ],
        ids=["no_run", "exit_code", "system_exit", "exception"],
    )
    def test_a_stage_failure_stops_the_chain_and_says_which(
        self, tmp_path: Path, act: Any, message: str
    ) -> None:
        def same_path_fails(raw: dict[str, Any], argv: list[str]) -> int:
            writer = next(hook for hook in raw["hook"] if hook["hook_id"] == "trace_writer")
            if writer["trace_dir"].endswith("same_path"):
                return act(raw, argv)
            return commit_one_run(raw, argv)

        spec = _spec(tmp_path)
        bench, export = Recorder(same_path_fails), Recorder(publish_dataset)
        with pytest.raises(ChainStageError, match=f"stage 'same_path'.*{message}"):
            run_chain(spec, bench=bench, export=export)
        assert len(bench.calls) == 3 and export.calls == [], "branch and export never ran"
        record = json.loads((tmp_path / "root" / "chain.json").read_text())
        assert set(record["stages"]) == {"record", "control", "same_path"}
        assert message in record["stages"]["same_path"]["error"]
        assert record["stages"]["record"]["runs"] == ["traces/record/run-15"]

    def test_a_report_that_cannot_be_written_is_the_report_stage_s_failure(
        self, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path)
        blocker = tmp_path / "root" / "report"
        blocker.parent.mkdir(parents=True)
        blocker.write_text("a file where the report directory goes")
        with pytest.raises(ChainStageError, match="stage 'report': FileExistsError"):
            run_chain(spec, stages=["report"], bench=Recorder(), export=Recorder())
        record = json.loads((tmp_path / "root" / "chain.json").read_text())
        assert record["stages"]["report"]["error"].startswith("stage 'report': FileExistsError")

    def test_an_export_that_fails(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        with pytest.raises(ChainStageError, match="spectragent-export exited 1"):
            run_chain(spec, stages=["export"], bench=Recorder(), export=Recorder(lambda r, a: 1))

    @pytest.mark.parametrize("dataset", ["first", "second"])
    def test_an_existing_dataset_stops_the_chain_before_any_paid_stage(
        self, tmp_path: Path, dataset: str
    ) -> None:
        spec = _spec(tmp_path)
        (tmp_path / "root" / "datasets" / dataset).mkdir(parents=True)
        bench, export = Recorder(commit_one_run), Recorder(publish_dataset)
        with pytest.raises(ChainConfigError, match="exists — an export never overwrites"):
            run_chain(spec, bench=bench, export=export)
        assert bench.calls == [] and export.calls == [], "no stage ran"
        assert not (tmp_path / "root" / "chain.json").exists()
        # Without the export stage, an existing dataset is nobody's business.
        run_chain(spec, stages=["record"], bench=bench, export=export)
        assert len(bench.calls) == 1


class TestTheSelection:
    def test_only_the_listed_stages_in_chain_order(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        assert selected_stages(spec, ["report", "record"]) == ["record", "report"]
        bench = Recorder(commit_one_run)
        result = run_chain(spec, stages=["report", "record"], bench=bench, export=Recorder())
        assert list(result.stages) == ["record", "report"] and len(bench.calls) == 1

    def test_unknown_and_unconfigured_stages_are_refused(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, control=None)
        with pytest.raises(ChainConfigError, match="unknown stage"):
            selected_stages(spec, ["record", "replay"])
        with pytest.raises(ChainConfigError, match=r"\['control'\] are not configured"):
            selected_stages(spec, ["control"])
        assert spec.configured() == ["record", "same_path", "branch", "export", "report"]

    def test_chain_json_keeps_earlier_stages(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        run_chain(spec, stages=["record"], bench=Recorder(commit_one_run), export=Recorder())
        run_chain(spec, stages=["report"], bench=Recorder(), export=Recorder())
        record = json.loads((tmp_path / "root" / "chain.json").read_text())
        assert set(record["stages"]) == {"record", "report"}
        assert (record["name"], record["evidence"]) == ("test-chain", "rehearsal")


class TestTheSpec:
    def test_unknown_keys_are_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "chain.yaml"
        raw = json.loads(_spec(tmp_path).model_dump_json())
        raw["record"]["trace_dir"] = "elsewhere"
        path.write_text(yaml.safe_dump(raw))
        with pytest.raises(ChainConfigError, match="trace_dir"):
            ChainSpec.load(path)

    def test_the_file_round_trips(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        path = tmp_path / "chain.yaml"
        path.write_text(yaml.safe_dump(json.loads(spec.model_dump_json())))
        assert ChainSpec.load(path) == spec

    @pytest.mark.parametrize(
        "planted",
        [{"label": "x"}, {"label": "x", "value": "a", "env": "B"}, {"label": "x", "value": ""}],
    )
    def test_a_planted_secret_has_exactly_one_source(self, planted: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            PlantedSecret.model_validate(planted)

    def test_a_branch_stage_never_evaluates(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="same_path.evaluate"):
            _spec(tmp_path, same_path={"config": tmp_path / "x.yaml", "evaluate": True})

    def test_planted_values_from_the_spec_or_the_environment(self) -> None:
        planted = [
            PlantedSecret(label="literal", value="pw-1"),
            PlantedSecret(label="key", env="TEACHER_KEY"),
            PlantedSecret(label="unset", env="NOT_SET_HERE"),
        ]
        found = resolve_planted(planted, {"TEACHER_KEY": "sk-from-env"})
        assert [(p.label, p.value, p.source) for p in found] == [
            ("literal", "pw-1", "value"),
            ("key", "sk-from-env", "env:TEACHER_KEY"),
            ("unset", None, "env:NOT_SET_HERE"),
        ]

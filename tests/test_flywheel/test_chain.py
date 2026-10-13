"""The metrics chain: path fields only, stage outcomes, the stage selection, the spec (arch §4.16 "链").

The CLIs are replaced at the chain's one seam (``run_chain(bench=..., export=...)``)
by recorders that read the effective YAML the chain wrote and commit a run
where it says — so what is pinned here is what the chain hands each stage.
The real CLIs run in the rehearsal (``test_rehearsal_live.py``).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.trace.encoding import PendingLLMCall, encode_llm_call
from spectragent.core.trace.models import TraceStatus
from spectragent.flywheel.chain import (
    ChainConfigError,
    ChainSpec,
    ChainStageError,
    PlantedSecret,
    build_spec_report,
    resolve_planted,
    run_chain,
    selected_stages,
)
from spectragent.flywheel.report import report_json
from tests._helpers.flywheel_traces import dataset_manifest
from tests._helpers.trace_builder import new_wal, prompt, step_record, usage

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
    wal.finalize(TraceStatus.COMPLETE)
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
                lambda raw, argv: commit_one_run(raw, argv) or 3,
                "exited 3: a guard vetoed the stage",
            ),
            (
                lambda raw, argv: (_ for _ in ()).throw(SystemExit("No task_loader")),
                "No task_loader",
            ),
            (lambda raw, argv: (_ for _ in ()).throw(RuntimeError("boom")), "RuntimeError: boom"),
        ],
        ids=["no_run", "exit_code", "veto", "system_exit", "exception"],
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


# ── the token limit ───────────────────────────────────────────────────


def committed_run(raw: dict[str, Any], name: str, calls: Sequence[int | None]) -> None:
    """One run under the YAML's trace_dir: step 0 made one model call per entry of *calls*.

    An entry is the tokens the call's provider reported; ``None`` is a call
    that reported no usage.
    """
    (writer,) = [hook for hook in raw["hook"] if hook["hook_id"] == "trace_writer"]
    wal = new_wal(Path(writer["trace_dir"]), name)
    encoded = []
    for index, tokens in enumerate(calls):
        pending = PendingLLMCall(prompt("go"))
        reported = usage(tokens, 0) if tokens is not None else None
        pending.complete(ChatInvokeCompletion(completion="done()", usage=reported))
        encoded.append(encode_llm_call(0, index, pending))
    wal.commit_step(0, step_record(0), llm_calls=encoded)
    wal.finalize(TraceStatus.COMPLETE)


class Sweep:
    """A stand-in ``spectragent-bench``: per stage, the calls of each run it commits and its exit code.

    A stage it has no plan for commits one run of one 1-token call and exits 0.
    """

    def __init__(self, plan: dict[str, tuple[list[list[int | None]], int]]) -> None:
        self.plan = plan
        self.limits: dict[str, str | None] = {}

    def __call__(self, raw: dict[str, Any], argv: list[str]) -> int:
        (writer,) = [hook for hook in raw["hook"] if hook["hook_id"] == "trace_writer"]
        stage = Path(writer["trace_dir"]).name
        self.limits[stage] = (
            argv[argv.index("--token-limit") + 1] if "--token-limit" in argv else None
        )
        runs, code = self.plan.get(stage, ([[1]], 0))
        for index, calls in enumerate(runs):
            committed_run(raw, f"{stage}-{len(self.limits)}-{index}", calls)
        return code


class TestTheTokenLimit:
    """The chain's total token limit (the user's ruling of 2026-10-14, arch §4.16 "token 上限")."""

    def test_each_run_stage_gets_what_is_left(self, tmp_path: Path) -> None:
        sweep = Sweep(
            {
                "record": ([[20, 10]], 0),
                "control": ([[15], [5]], 1),
                "same_path": ([[10]], 0),
                "branch": ([[5]], 0),
            }
        )
        result = run_chain(
            _spec(tmp_path, token_limit=100),
            bench=Recorder(sweep),
            export=Recorder(publish_dataset),
        )
        assert sweep.limits == {"record": "100", "control": "70", "same_path": "50", "branch": "40"}
        assert result.token_limit_stop is None and result.report is not None
        record = json.loads((tmp_path / "root" / "chain.json").read_text())
        assert (record["token_limit"], record["tokens_used"]) == (100, 65)
        assert record["stopped_by_token_limit"] is None
        assert record["stages"]["control"]["token_use"] == {
            "runs": 2,
            "tokens": 20,
            "calls": 2,
            "calls_without_usage": [],
            "skipped": {},
        }
        use = result.report.token_use
        assert (use.limit, use.tokens, use.limit_reached) == (100, 65, False)
        assert {role: item.tokens for role, item in use.roles.items()} == {
            "record": 30,
            "control": 20,
            "same_path": 10,
            "branch": 5,
        }

    def test_the_stage_that_reaches_it_is_the_last_to_start(self, tmp_path: Path) -> None:
        sweep = Sweep({"record": ([[30]], 0), "control": ([[25]], 4)})
        bench, export = Recorder(sweep), Recorder(publish_dataset)
        result = run_chain(_spec(tmp_path, token_limit=50), bench=bench, export=export)

        assert list(sweep.limits) == ["record", "control"], "no run stage after it started"
        assert export.calls == [], "nor the export"
        assert list(result.stages) == ["record", "control", "report"]
        stop = result.token_limit_stop
        assert stop is not None and stop.record() == {
            "stage": "control",
            "tokens_used": 55,
            "token_limit": 50,
            "stages_not_started": ["same_path", "branch", "export"],
            "calls_without_usage": [],
        }
        record = json.loads((tmp_path / "root" / "chain.json").read_text())
        assert record["stopped_by_token_limit"] == stop.record()
        assert record["stages"]["control"]["exit_codes"] == [4]
        assert set(record["stages"]) == {"record", "control", "report"}

        # The report is computed from what exists and says why the chain stopped.
        root = tmp_path / "root"
        document = json.loads((root / "report" / "report.json").read_text())
        assert document["token_use"]["limit"] == 50
        assert document["token_use"]["tokens"] == 55
        assert document["token_use"]["limit_reached"] is True
        assert document["inputs"]["roles"]["same_path"]["present"] is False
        markdown = (root / "report" / "report.md").read_text()
        assert "**The chain stopped at its token limit.** Its runs recorded 55 tokens" in markdown
        assert markdown.index("stopped at its token limit") < markdown.index("## Inputs")
        assert (
            report_json(build_spec_report(_spec(tmp_path, token_limit=50)))
            == (root / "report" / "report.json").read_text()
        ), "report --spec recomputes the same bytes"

    def test_the_chain_stops_on_its_own_count_too(self, tmp_path: Path) -> None:
        """A stage whose runs brought the total to the limit stops the chain, whatever it exited."""
        sweep = Sweep({"record": ([[60]], 0)})
        result = run_chain(
            _spec(tmp_path, token_limit=50), bench=Recorder(sweep), export=Recorder()
        )
        assert list(sweep.limits) == ["record"]
        assert result.token_limit_stop is not None and result.token_limit_stop.stage == "record"

    def test_runs_of_earlier_invocations_count(self, tmp_path: Path) -> None:
        """Stage by stage, one limit: each run of the chain first counts the runs already there."""
        sweep = Sweep({"record": ([[60]], 0), "control": ([[30]], 0)})
        for stage in ("record", "control", "same_path"):
            run_chain(
                _spec(tmp_path, token_limit=100),
                stages=[stage],
                bench=Recorder(sweep),
                export=Recorder(),
            )
        # same_path got the 10 left and used 1 of them (the sweep's default run).
        assert sweep.limits == {"record": "100", "control": "40", "same_path": "10"}

        # With the limit lowered under the 91 used, no run or export stage starts again.
        bench = Recorder(sweep)
        result = run_chain(
            _spec(tmp_path, token_limit=80),
            stages=["branch", "export", "report"],
            bench=bench,
            export=Recorder(publish_dataset),
        )
        assert bench.calls == [], "the runs under the root had already reached the limit"
        stop = result.token_limit_stop
        assert stop is not None
        assert (stop.stage, stop.tokens_used, stop.stages_not_started) == (
            None,
            91,
            ["branch", "export"],
        )
        assert result.report is not None and result.report.token_use.limit_reached is True
        record = json.loads((tmp_path / "root" / "chain.json").read_text())
        assert record["stopped_by_token_limit"]["stage"] is None
        # Only the report selected: nothing paid was asked for, so nothing stopped.
        again = run_chain(
            _spec(tmp_path, token_limit=80), stages=["report"], bench=bench, export=Recorder()
        )
        assert again.token_limit_stop is None

    def test_calls_without_usage_are_counted_and_named(self, tmp_path: Path) -> None:
        sweep = Sweep({"record": ([[10, None]], 4)})
        result = run_chain(
            _spec(tmp_path, token_limit=10), bench=Recorder(sweep), export=Recorder()
        )
        (run,) = result.stages["record"].runs
        named = f"{run}: step 0 call 1 (completed)"
        use = result.stages["record"].token_use
        assert use is not None and (use.tokens, use.calls, use.calls_without_usage) == (
            10,
            2,
            [named],
        )
        assert result.token_limit_stop is not None
        assert result.token_limit_stop.calls_without_usage == [named]
        assert result.report is not None
        assert result.report.token_use.roles["record"].calls_without_usage == [named]

    def test_unlimited_is_what_the_chain_always_did(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spectragent.flywheel import chain as chain_module

        def never(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("a chain without a token limit counted tokens")

        monkeypatch.setattr(chain_module, "open_chain", never)
        monkeypatch.setattr(chain_module, "token_use", never)
        bench = Recorder(commit_one_run)
        result = run_chain(_spec(tmp_path), bench=bench, export=Recorder(publish_dataset))
        assert all("--token-limit" not in argv for argv in bench.calls)
        assert result.token_limit_stop is None
        record = json.loads((tmp_path / "root" / "chain.json").read_text())
        assert record["token_limit"] == "unlimited"
        assert "tokens_used" not in record and "stopped_by_token_limit" not in record
        assert all("token_use" not in stage for stage in record["stages"].values())
        # A sweep that says it reached a limit nobody gave it is a failed stage.
        with pytest.raises(ChainStageError, match="stage 'record': spectragent-bench exited 4"):
            run_chain(
                _spec(tmp_path),
                stages=["record"],
                bench=Recorder(lambda raw, argv: commit_one_run(raw, argv) or 4),
                export=Recorder(),
            )

    @pytest.mark.parametrize("value", [0, -1, None, "lots", "none"])
    def test_the_limit_is_a_number_of_at_least_one_or_unlimited(
        self, tmp_path: Path, value: Any
    ) -> None:
        with pytest.raises(ValueError, match="token_limit"):
            _spec(tmp_path, token_limit=value)
        assert _spec(tmp_path).token_limit == "unlimited"
        assert _spec(tmp_path, token_limit="unlimited").token_limit == "unlimited"
        assert _spec(tmp_path, token_limit=2_000_000).token_limit == 2_000_000

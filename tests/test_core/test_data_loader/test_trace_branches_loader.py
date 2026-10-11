"""The ``trace_branches`` task loader (``docs/architecture_design.md`` §4.10, PRD F6/F7/F12/F17).

Sources are committed by the real ``WALWriter`` (``tests/_helpers/branch_sources.py``),
so every rule here reads a run exactly as it reads a recorded one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.data_loader._common import BENCHMARK_DATA_ENV, BenchmarkDataError
from spectragent.core.data_loader.task import TaskLoaderRegistrar, TraceBranchesTaskLoader
from spectragent.core.data_loader.task.require_eval import RequireEval
from spectragent.core.data_loader.task.step_selector import EveryKSteps, ExplicitSteps, LastStep
from spectragent.core.trace.models import RunStatus
from spectragent.schemas.restoration import BranchProvenance, Checkpoint
from spectragent.schemas.task import BrowserSetup, TaskContext
from tests._helpers.branch_sources import (
    START_URL,
    TASK_TEXT,
    Step,
    setup_record,
    write_source,
)
from tests._helpers.trace_builder import eval_result


def loader(root: Path, **knobs: Any) -> TraceBranchesTaskLoader:
    selector = knobs.pop("step_selector", None)
    built = TraceBranchesTaskLoader(data_dir=str(root), **knobs)
    if selector is not None:
        built.step_selector = selector  # what the Composer's injection does
    return built


# ── expansion: ids, definitions, the three context parts ─────────────


class TestExpansion:
    def test_every_candidate_step_times_branches_per_step(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=3)
        tasks = loader(tmp_path, branches_per_step=2)
        assert tasks.task_ids() == [
            f"run-a/s{step}/b{index}" for step in range(3) for index in range(2)
        ]
        assert len(tasks) == 6

    def test_the_task_definition_is_the_sources_task(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=2)
        task = loader(tmp_path)["run-a/s1/b0"]
        assert task.benchmark == "trace_branches"
        assert task.env_type == "browser"
        assert task.task_text == TASK_TEXT
        assert (task.difficulty, task.tags) == ("hard", ["cart"])
        assert task.setup == BrowserSetup(start_url=START_URL)

    def test_the_three_parts_of_the_context(self, tmp_path: Path) -> None:
        source = write_source(tmp_path, "run-a", steps=3)
        context = loader(tmp_path, branches_per_step=2).get_task_context("run-a/s2/b1")
        assert context.env == {"start_url": START_URL, "replay_archive": str(source)}
        assert context.loop == {"checkpoint": Checkpoint(trace_dir=str(source), step=2)}
        assert context.branch == BranchProvenance(
            source_run_id="run-a",
            source_trace="run-a",
            source_benchmark="online_mind2web",
            source_subset="Hard",
            source_task_id="task-001",
            checkpoint_step=2,
            branch_index=1,
            branch_group_id="run-a/s2",
        )

    def test_globs_are_relative_to_the_root_and_sorted(self, tmp_path: Path) -> None:
        write_source(tmp_path / "b", "run-1", steps=1)
        write_source(tmp_path / "a", "run-2", steps=1)
        (tmp_path / "a" / "notes").mkdir()  # a match without a manifest is not a run
        (tmp_path / "a" / "summary.json").write_text("{}")
        tasks = loader(tmp_path, sources=["b/*", "a/*", "a/run-*"])
        assert tasks.task_ids() == ["run-2/s0/b0", "run-1/s0/b0"], "sorted by path: a/ before b/"
        branch = tasks.get_task_context("run-2/s0/b0").branch
        assert branch is not None and branch.source_trace == "a/run-2"

    def test_the_default_root_is_traces_in_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = write_source(tmp_path / "traces", "run-a", steps=1)
        monkeypatch.chdir(tmp_path)
        tasks = TraceBranchesTaskLoader()
        assert tasks.task_ids() == ["run-a/s0/b0"]
        checkpoint = tasks.get_task_context("run-a/s0/b0").loop["checkpoint"]
        assert checkpoint.trace_dir == str(source.resolve()), "absolute, whatever the cwd"

    def test_a_relative_root_still_gives_absolute_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_source(tmp_path / "recorded", "run-a", steps=1)
        monkeypatch.chdir(tmp_path)
        context = TraceBranchesTaskLoader(data_dir="recorded").get_task_context("run-a/s0/b0")
        assert Path(context.env["replay_archive"]).is_absolute()
        assert context.loop["checkpoint"].trace_dir == context.env["replay_archive"]

    def test_unknown_task_ids(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=1)
        tasks = loader(tmp_path)
        assert tasks.get("nope") is None
        assert tasks.get_task_context("nope") == TaskContext()
        with pytest.raises(KeyError):
            tasks["nope"]

    def test_a_returned_context_is_the_callers_copy(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=1)
        tasks = loader(tmp_path)
        tasks.get_task_context("run-a/s0/b0").env["start_url"] = "https://evil.test/"
        assert tasks.get_task_context("run-a/s0/b0").env["start_url"] == START_URL

    def test_no_match_is_empty_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tasks = loader(tmp_path / "missing", sources=["recorded/*"])
        assert tasks.task_ids() == [] and len(tasks) == 0
        assert "recorded/*" in caplog.text and "missing" in caplog.text


class TestLaziness:
    def test_constructing_reads_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_source(tmp_path, "run-a", steps=1)

        def no_io(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the loader touched the file system while being built")

        with monkeypatch.context() as patch:
            patch.setattr(Path, "glob", no_io)
            patch.setattr(Path, "resolve", no_io)
            patch.setattr(Path, "is_dir", no_io)
            built = TraceBranchesTaskLoader(data_dir=str(tmp_path))
        assert built.task_ids() == ["run-a/s0/b0"], "the scan happens on first use"

    def test_composing_the_yaml_block_reads_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spectragent.core.benchmark_runner import build_composer

        write_source(tmp_path, "run-a", steps=2)
        scans: list[Any] = []
        original = TraceBranchesTaskLoader._scan_once

        def counting(self: TraceBranchesTaskLoader) -> None:
            scans.append(self)
            original(self)

        monkeypatch.setattr(TraceBranchesTaskLoader, "_scan_once", counting)
        run = build_composer().compose(
            {
                "task_loader": [
                    {
                        "benchmark_name": "trace_branches",
                        "data_dir": str(tmp_path),
                        "step_selector": {"selector_id": "explicit", "steps": [1]},
                    }
                ]
            }
        )
        built = run.optional_single("task_loader")
        assert isinstance(built, TraceBranchesTaskLoader) and scans == []
        assert built.task_ids() == ["run-a/s1/b0"], "the configured selector arrived"

    def test_scanned_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        write_source(tmp_path, "run-a", steps=1)
        tasks = loader(tmp_path)
        tasks.task_ids()
        monkeypatch.setattr(Path, "glob", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
        assert tasks.task_ids() == ["run-a/s0/b0"] and dict(tasks.skipped) == {}


# ── candidates and the selector ──────────────────────────────────────


class TestCandidates:
    def test_committed_policy_steps_with_a_recorded_observation(self, tmp_path: Path) -> None:
        write_source(
            tmp_path,
            "run-a",
            steps=[Step(), Step(error_phase="observe"), Step(error_phase="act"), Step("terminal")],
        )
        assert loader(tmp_path).task_ids() == ["run-a/s0/b0", "run-a/s2/b0"]

    def test_an_uncommitted_step_is_invisible(self, tmp_path: Path) -> None:
        source = write_source(tmp_path, "run-a", steps=2)
        orphan = json.loads((source / "steps" / "001.json").read_text())
        orphan["step"] = 2
        (source / "steps" / "002.json").write_text(json.dumps(orphan))  # never in the manifest
        tasks = loader(tmp_path, step_selector=ExplicitSteps(steps=[2]))
        assert tasks.task_ids() == []
        assert "not committed (its last committed step is 1)" in tasks.skipped["run-a#s2"]

    def test_a_gap_ends_the_candidates(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=3, uncommitted=[1])
        tasks = loader(tmp_path, step_selector=ExplicitSteps(steps=[0, 2]))
        assert tasks.task_ids() == ["run-a/s0/b0"]
        assert "prefix is not committed (step 1 is missing)" in tasks.skipped["run-a#s2"]

    @pytest.mark.parametrize(
        ("steps", "named", "reason"),
        [
            (3, 7, "step 7 is not committed (its last committed step is 2)"),
            ([Step(), Step("terminal")], 1, "step 1 is a terminal step"),
            ([Step(), Step(error_phase="observe")], 1, "observation was not recorded"),
            (2, -1, "step -1 is not committed"),
        ],
        ids=["past_the_end", "terminal", "observe_failed", "negative"],
    )
    def test_why_a_named_step_is_not_a_candidate(
        self, tmp_path: Path, steps: Any, named: int, reason: str
    ) -> None:
        write_source(tmp_path, "run-a", steps=steps)
        tasks = loader(tmp_path, step_selector=ExplicitSteps(steps=[0, named]))
        assert tasks.task_ids() == ["run-a/s0/b0"]
        assert reason in tasks.skipped[f"run-a#s{named}"]

    def test_the_selector_chooses_among_the_candidates(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=7)
        tasks = loader(tmp_path, step_selector=EveryKSteps(k=3, offset=1))
        assert tasks.task_ids() == ["run-a/s1/b0", "run-a/s4/b0"]

    def test_last_is_each_sources_own_last_candidate(self, tmp_path: Path) -> None:
        """Sources of different lengths fork once each, at their own last branchable step."""
        write_source(tmp_path, "run-a", steps=5)
        write_source(tmp_path, "run-b", steps=[Step(), Step(), Step("terminal")])
        write_source(tmp_path, "run-c", steps=[Step(), Step(error_phase="observe")])
        tasks = loader(tmp_path, step_selector=LastStep(), branches_per_step=2)
        assert tasks.task_ids() == [
            "run-a/s4/b0",
            "run-a/s4/b1",
            "run-b/s1/b0",
            "run-b/s1/b1",
            "run-c/s0/b0",
            "run-c/s0/b1",
        ]
        assert dict(tasks.skipped) == {}

    def test_a_source_with_no_branchable_step(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=[Step(error_phase="observe")])
        write_source(tmp_path, "run-b", steps=1)
        tasks = loader(tmp_path)
        assert tasks.task_ids() == ["run-b/s0/b0"]
        assert "no branchable step" in tasks.skipped["run-a"]


# ── filters ──────────────────────────────────────────────────────────


class TestFilters:
    @pytest.mark.parametrize(
        ("require", "kept"),
        [("completed", {"done"}), ("error", {"failed"}), ("any", {"done", "failed", "running"})],
    )
    def test_require_status(self, tmp_path: Path, require: str, kept: set[str]) -> None:
        write_source(tmp_path, "done", steps=1)
        write_source(tmp_path, "failed", steps=1, status=RunStatus.ERROR)
        write_source(tmp_path, "running", steps=1, status=RunStatus.RUNNING)
        tasks = loader(tmp_path, require_status=require)
        assert {task_id.split("/")[0] for task_id in tasks.task_ids()} == kept
        for name in {"done", "failed", "running"} - kept:
            assert tasks.skipped[name].startswith("filtered: status ")

    def test_a_filtered_source_never_raises(self, tmp_path: Path) -> None:
        """A filter leaves a source out even when it could not be branched anyway."""
        write_source(tmp_path, "failed", steps=1, status=RunStatus.ERROR, archivers=())
        assert loader(tmp_path).task_ids() == []

    @pytest.mark.parametrize(
        ("verdicts", "kept"),
        [
            ({"exact_match": eval_result(predicted_label=1)}, True),
            ({"exact_match": eval_result(predicted_label=0)}, False),
            (
                {"exact_match": eval_result(predicted_label=1, verdict_source="evaluation_error")},
                False,
            ),
            (
                {"exact_match": eval_result(predicted_label=1, verdict_source="not_evaluated")},
                False,
            ),
            ({"judge": eval_result(evaluator_id="online_mind2web", predicted_label=1)}, False),
            ({}, False),
        ],
        ids=["graded_pass", "graded_fail", "evaluation_error", "not_evaluated", "other", "none"],
    )
    def test_require_eval(self, tmp_path: Path, verdicts: dict[str, Any], kept: bool) -> None:
        write_source(tmp_path, "run-a", steps=1, verdicts=verdicts)
        tasks = loader(tmp_path, require_eval=RequireEval(evaluator_id="exact_match"))
        assert (tasks.task_ids() == ["run-a/s0/b0"]) is kept
        if not kept:
            assert "(require_eval)" in tasks.skipped["run-a"]

    def test_require_eval_label_and_slug(self, tmp_path: Path) -> None:
        verdicts = {
            "exact_match-strict": eval_result(predicted_label=0),
            "exact_match-loose": eval_result(predicted_label=1),
        }
        write_source(tmp_path, "run-a", steps=1, verdicts=verdicts)
        for need, kept in [
            ({"evaluator_id": "exact_match"}, True),
            ({"evaluator_id": "exact_match", "label": 0}, True),
            ({"evaluator_id": "exact_match", "slug": "exact_match-strict"}, False),
            ({"evaluator_id": "exact_match", "slug": "exact_match-loose"}, True),
            ({"evaluator_id": "exact_match", "slug": "exact_match-strict", "label": 0}, True),
        ]:
            tasks = loader(tmp_path, require_eval=need)  # the Composer's dict form
            assert bool(tasks.task_ids()) is kept, need

    def test_result_json_is_never_read(self, tmp_path: Path) -> None:
        source = write_source(tmp_path, "run-a", steps=1, verdicts={"exact_match": eval_result()})
        (source / "result.json").write_text("{not json")
        tasks = loader(tmp_path, require_eval=RequireEval(evaluator_id="exact_match"))
        assert tasks.task_ids() == ["run-a/s0/b0"]

    def test_a_corrupt_verdict_is_a_source_that_cannot_be_branched(self, tmp_path: Path) -> None:
        source = write_source(tmp_path, "run-a", steps=1)
        (source / "evals" / "exact_match.json").write_text("{not json")
        with pytest.raises(BenchmarkDataError, match="evaluations do not read back"):
            loader(tmp_path, require_eval=RequireEval(evaluator_id="exact_match")).task_ids()


# ── preconditions ────────────────────────────────────────────────────


def _corrupt_manifest(root: Path) -> None:
    source = write_source(root, "run-x", steps=1)
    (source / "manifest.json").write_text("{torn")


def _duplicate(root: Path) -> None:
    write_source(root / "copy", "run-x", steps=1)


_PRECONDITIONS: dict[str, tuple[Any, str]] = {
    "format_1": (
        lambda root: write_source(root, "run-x", steps=1, format_version=1),
        "format 1 trace",
    ),
    "branch_run": (
        lambda root: write_source(
            root,
            "run-x",
            steps=1,
            branch=BranchProvenance(
                source_run_id="run-0",
                source_trace="run-0",
                checkpoint_step=0,
                branch_index=0,
                branch_group_id="run-0/s0",
            ),
        ),
        "itself a branch run of 'run-0'",
    ),
    "replay_run": (
        lambda root: write_source(
            root,
            "run-x",
            steps=1,
            task_setup=setup_record({"start_url": START_URL, "replay_archive": "/rec"}),
        ),
        "it is a replay run",
    ),
    "no_archive": (
        lambda root: write_source(root, "run-x", steps=1, archivers=()),
        "did not record 'browser.network'",
    ),
    "no_fingerprint": (
        lambda root: write_source(root, "run-x", steps=1, fingerprint=False),
        "no committed environment fingerprint",
    ),
    "no_setup_context": (
        lambda root: write_source(root, "run-x", steps=1, task_setup=None, benchmark=""),
        "no environment setup context: it recorded none",
    ),
    "redacted_setup_context": (
        lambda root: write_source(
            root, "run-x", steps=1, task_setup=setup_record(redacted=True), benchmark="nowhere"
        ),
        "the recorded one is redacted (1 value(s)), and re-resolving nowhere/Hard/task-001 failed",
    ),
    "unreadable": (_corrupt_manifest, "not a readable trace"),
}


class TestPreconditions:
    @pytest.mark.parametrize("case", sorted(_PRECONDITIONS))
    def test_raise_names_the_source_and_the_reason(self, tmp_path: Path, case: str) -> None:
        build, reason = _PRECONDITIONS[case]
        build(tmp_path)
        write_source(tmp_path, "run-ok", steps=1)
        with pytest.raises(BenchmarkDataError) as info:
            loader(tmp_path).task_ids()
        assert "run-x: " in str(info.value) and reason in str(info.value)
        assert "run-ok" not in str(info.value)

    @pytest.mark.parametrize("case", sorted(_PRECONDITIONS))
    def test_skip_records_the_reason_and_keeps_the_rest(self, tmp_path: Path, case: str) -> None:
        build, reason = _PRECONDITIONS[case]
        build(tmp_path)
        write_source(tmp_path, "run-ok", steps=1)
        tasks = loader(tmp_path, on_unbranchable="skip")
        assert tasks.task_ids() == ["run-ok/s0/b0"]
        assert reason in tasks.skipped["run-x"]

    def test_a_shared_run_id_refuses_both(self, tmp_path: Path) -> None:
        write_source(tmp_path / "a", "run-x", steps=1)
        _duplicate(tmp_path)
        tasks = loader(tmp_path, sources=["*/*"], on_unbranchable="skip")
        assert tasks.task_ids() == []
        assert "also the run id of copy/run-x" in tasks.skipped["a/run-x"]
        assert "also the run id of a/run-x" in tasks.skipped["copy/run-x"]

    def test_a_shared_run_id_filtered_out_is_no_collision(self, tmp_path: Path) -> None:
        write_source(tmp_path / "a", "run-x", steps=1)
        write_source(tmp_path / "copy", "run-x", steps=1, status=RunStatus.ERROR)
        assert loader(tmp_path, sources=["*/*"]).task_ids() == ["run-x/s0/b0"]

    def test_every_unbranchable_source_is_listed_at_once(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-1", steps=1, archivers=())
        write_source(tmp_path, "run-2", steps=1, fingerprint=False)
        with pytest.raises(BenchmarkDataError, match="2 source") as info:
            loader(tmp_path).task_ids()
        assert "run-1: " in str(info.value) and "run-2: " in str(info.value)

    def test_require_archive_names_what_is_required(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=1, archivers=())
        assert loader(tmp_path, require_archive=[]).task_ids() == ["run-a/s0/b0"]
        write_source(tmp_path, "run-b", steps=1, archivers=("browser.network", "browser.storage"))
        tasks = loader(
            tmp_path,
            require_archive=["browser.network", "browser.storage"],
            on_unbranchable="skip",
        )
        assert tasks.task_ids() == ["run-b/s0/b0"]
        assert "did not record 'browser.network'" in tasks.skipped["run-a"]

    def test_failed_scan_publishes_nothing(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-ok", steps=1)
        write_source(tmp_path, "run-x", steps=1, archivers=())
        tasks = loader(tmp_path)
        with pytest.raises(BenchmarkDataError):
            tasks.task_ids()
        with pytest.raises(BenchmarkDataError):
            tasks.get_task_context("run-ok/s0/b0")


# ── the environment setup context (Q6) ───────────────────────────────


def _mind2web_data(root: Path, start_url: str) -> Path:
    data = root / "browser" / "online_mind2web"
    data.mkdir(parents=True)
    (data / "data_info.json").write_text(json.dumps({"split": {"Hard": "hard.jsonl"}}))
    record = {"task_id": "task-001", "task_text": TASK_TEXT, "start_url": start_url}
    (data / "hard.jsonl").write_text(json.dumps(record) + "\n")
    return root


class TestSetupContext:
    def test_the_recorded_context_is_used(self, tmp_path: Path) -> None:
        write_source(
            tmp_path,
            "run-a",
            steps=1,
            task_setup=setup_record({"start_url": "https://recorded.test/", "lang": "en"}),
        )
        env = loader(tmp_path).get_task_context("run-a/s0/b0").env
        assert env["start_url"] == "https://recorded.test/" and env["lang"] == "en"

    @pytest.mark.parametrize("recorded", ["none", "redacted"])
    def test_re_resolved_from_the_source_benchmark(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: str
    ) -> None:
        monkeypatch.setenv(
            BENCHMARK_DATA_ENV, str(_mind2web_data(tmp_path / "data", "https://real.test/"))
        )
        task_setup = None if recorded == "none" else setup_record(redacted=True)
        write_source(tmp_path / "src", "run-a", steps=1, task_setup=task_setup)
        context = loader(tmp_path / "src").get_task_context("run-a/s0/b0")
        assert context.env["start_url"] == "https://real.test/"
        assert "<redacted>" not in json.dumps(context.env)

    def test_a_task_the_source_benchmark_lacks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            BENCHMARK_DATA_ENV, str(_mind2web_data(tmp_path / "data", "https://real.test/"))
        )
        write_source(tmp_path / "src", "run-a", steps=1, task_setup=None, task_id="task-404")
        with pytest.raises(BenchmarkDataError, match="has no task online_mind2web/Hard/task-404"):
            loader(tmp_path / "src").task_ids()

    def test_the_source_benchmark_is_built_once_per_subset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            BENCHMARK_DATA_ENV, str(_mind2web_data(tmp_path / "data", "https://real.test/"))
        )
        for name in ("run-a", "run-b", "run-c"):
            write_source(tmp_path / "src", name, steps=1, task_setup=None)
        real = TaskLoaderRegistrar.get("online_mind2web")
        built: list[Any] = []

        class Counting(real):  # type: ignore[valid-type,misc]
            __abstract__ = True

            def __post_init__(self) -> None:
                built.append(self)
                super().__post_init__()

        monkeypatch.setattr(TaskLoaderRegistrar, "get", lambda key: Counting)
        assert len(loader(tmp_path / "src").task_ids()) == 3
        assert len(built) == 1


# ── construction ─────────────────────────────────────────────────────


class TestKnobs:
    @pytest.mark.parametrize("pattern", ["/abs/*", "~/traces/*", "../up/*", "a/../../b", ""])
    def test_a_glob_must_stay_inside_the_root(self, pattern: str) -> None:
        with pytest.raises(ValueError, match="source glob"):
            TraceBranchesTaskLoader(sources=[pattern])

    def test_defaults(self) -> None:
        built = TraceBranchesTaskLoader()
        assert built.sources == ("*",) and built.require_archive == ("browser.network",)
        assert (built.branches_per_step, built.require_status, built.on_unbranchable) == (
            1,
            "completed",
            "raise",
        )
        assert built.require_eval is None and built.step_selector.selector_id == "all"
        assert built.benchmark_name == "trace_branches"

    @pytest.mark.parametrize(
        "knobs",
        [{"branches_per_step": 0}, {"require_status": "done"}, {"on_unbranchable": "ignore"}],
    )
    def test_out_of_range_knobs(self, knobs: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="trace_branches"):
            TraceBranchesTaskLoader(**knobs)

    def test_the_yaml_surface(self) -> None:
        from pydantic import ValidationError

        from spectragent.generated._composed import TraceBranchesTask_LoaderConfig

        config = TraceBranchesTask_LoaderConfig.model_validate(
            {
                "data_dir": "traces/recorded",
                "sources": ["2026-*"],
                "require_eval": {"evaluator_id": "online_mind2web", "slug": "online_mind2web-ab"},
                "step_selector": {"selector_id": "every_k", "k": 2},
            }
        )
        assert config.require_eval == RequireEval(
            evaluator_id="online_mind2web", label=1, slug="online_mind2web-ab"
        )
        for bad in (
            {"step_selector": {"selector_id": "every"}},
            {"step_selector": {"selector_id": "all"}, "branches_per_step": 0},
            {"step_selector": {"selector_id": "all"}, "require_eval": {"evaluator": "x"}},
            {"step_selector": {"selector_id": "all"}, "on_unbranchable": "ignore"},
            {},
        ):
            with pytest.raises(ValidationError):
                TraceBranchesTask_LoaderConfig.model_validate(bad)

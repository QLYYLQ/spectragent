"""The ``trace_branches`` task loader (``docs/architecture_design.md`` §4.10).

Sources are committed by the real ``WALWriter`` (``tests/_helpers/branch_sources.py``),
so every rule here reads a run exactly as it reads a recorded one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.settle import PageSettle
from spectragent.core.data_loader._common import BENCHMARK_DATA_ENV, BenchmarkDataError
from spectragent.core.data_loader.task import TaskLoaderRegistrar, TraceBranchesTaskLoader
from spectragent.core.data_loader.task.require_eval import RequireEval
from spectragent.core.data_loader.task.step_selector import EveryKSteps, ExplicitSteps, LastStep
from spectragent.core.trace.models import TraceStatus
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.restoration import BranchProvenance, BranchTaskVariant, Checkpoint
from spectragent.schemas.task import BrowserSetup, TaskContext
from tests._helpers.branch_sources import (
    CAPTURE_FAILED,
    NEVER_QUIET,
    NOT_LOADED,
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
        tasks = loader(
            tmp_path,
            sources=(
                "b/*",
                "a/*",
                "a/run-*",
            ),
        )
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
        tasks = loader(tmp_path / "missing", sources=("recorded/*",))
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
        ("wanted", "kept"),
        [
            ("any", {"done", "failed", "timed-out"}),
            ("done", {"done"}),
            ("error", {"failed"}),
            ("timeout", {"timed-out"}),
            ("vetoed", set()),
        ],
    )
    def test_agent_outcome(self, tmp_path: Path, wanted: str, kept: set[str]) -> None:
        """How the agent ended is a filter, any by default — a separate ledger from the trace's."""
        write_source(tmp_path, "done", steps=1)
        write_source(tmp_path, "failed", steps=1, agent_done=AgentDoneStatus.ERROR)
        write_source(tmp_path, "timed-out", steps=1, agent_done=AgentDoneStatus.TIMEOUT)
        tasks = loader(tmp_path, agent_outcome=wanted)
        assert {task_id.split("/")[0] for task_id in tasks.task_ids()} == kept
        for name in {"done", "failed", "timed-out"} - kept:
            assert tasks.skipped[name].startswith("filtered: its agent ended ")
            assert f"(agent_outcome: {wanted})" in tasks.skipped[name]

    def test_without_a_result_the_outcome_filter_cannot_keep_a_source(self, tmp_path: Path) -> None:
        write_source(tmp_path, "no-result", steps=1, agent_done=None)
        assert loader(tmp_path).task_ids() == ["no-result/s0/b0"]  # any reads no result.json
        tasks = loader(tmp_path, agent_outcome="done")
        assert tasks.task_ids() == []
        assert "it has no result.json" in tasks.skipped["no-result"]

    def test_a_filtered_source_never_raises(self, tmp_path: Path) -> None:
        """A filter leaves a source out even when it could not be branched anyway."""
        write_source(tmp_path, "failed", steps=1, agent_done=AgentDoneStatus.ERROR, archivers=())
        assert loader(tmp_path, agent_outcome="done").task_ids() == []


class TestTheTracesOwnLedger:
    """A checkpoint is a step the source's trace is closed and complete through — always."""

    def test_a_trace_never_closed_has_no_branchable_step(self, tmp_path: Path) -> None:
        write_source(tmp_path, "writing", steps=2, status=TraceStatus.WRITING, agent_done=None)
        write_source(tmp_path, "closed", steps=1)
        tasks = loader(tmp_path, agent_outcome="any")
        assert tasks.task_ids() == ["closed/s0/b0"]
        assert tasks.skipped["writing"].startswith("no branchable step: its trace was never closed")

    def test_an_incomplete_trace_gives_every_checkpoint_before_its_hole(
        self, tmp_path: Path
    ) -> None:
        """The run stopped on a hole at step 2: steps 0 and 1 are still worth branching."""
        write_source(
            tmp_path,
            "holed",
            steps=3,
            uncommitted=[2],
            status=TraceStatus.INCOMPLETE,
            incomplete_reason="recording failed at step 2: ArchiveCollectionError: boom",
            agent_done=AgentDoneStatus.ERROR,
        )
        tasks = loader(tmp_path)
        assert tasks.task_ids() == ["holed/s0/b0", "holed/s1/b0"]

    def test_a_selected_step_past_the_hole_names_it(self, tmp_path: Path) -> None:
        write_source(
            tmp_path,
            "holed",
            steps=4,
            uncommitted=[2],
            status=TraceStatus.INCOMPLETE,
            incomplete_reason="recording failed at step 2: ArchiveCollectionError: boom",
        )
        tasks = loader(tmp_path, step_selector=ExplicitSteps(steps=[1, 3]))
        assert tasks.task_ids() == ["holed/s1/b0"]
        assert "step 3's prefix is not committed (step 2 is missing)" in tasks.skipped["holed#s3"]

    def test_the_rule_is_complete_through_not_merely_committed(self, tmp_path: Path) -> None:
        """A committed step past ``complete_through`` is no checkpoint, whatever made it so."""
        source = write_source(
            tmp_path,
            "run-a",
            steps=3,
            status=TraceStatus.INCOMPLETE,
            incomplete_reason="recording failed at step 1: OSError: disk full",
        )
        path = source / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["complete_through"] = 0
        path.write_text(json.dumps(manifest))
        tasks = loader(tmp_path, step_selector=ExplicitSteps(steps=[0, 2]))
        assert tasks.task_ids() == ["run-a/s0/b0"]
        assert tasks.skipped["run-a#s2"] == (
            "step 2 is past the step its trace is complete through (0): recording failed at "
            "step 1: OSError: disk full"
        )

    def test_a_tail_failure_costs_no_checkpoint(self, tmp_path: Path) -> None:
        """Incomplete only after its last step (the close-out collection failed): all steps count."""
        write_source(
            tmp_path,
            "tail",
            steps=2,
            status=TraceStatus.INCOMPLETE,
            incomplete_reason="the close-out evidence collection failed: ArchiveCollectionError",
        )
        assert loader(tmp_path).task_ids() == ["tail/s0/b0", "tail/s1/b0"]

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
    "older_format": (
        lambda root: write_source(root, "run-x", steps=1, format_version=2),
        "trace format version 2",
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
        tasks = loader(tmp_path, sources=("*/*",), on_unbranchable="skip")
        assert tasks.task_ids() == []
        assert "also the run id of copy/run-x" in tasks.skipped["a/run-x"]
        assert "also the run id of a/run-x" in tasks.skipped["copy/run-x"]

    def test_a_shared_run_id_filtered_out_is_no_collision(self, tmp_path: Path) -> None:
        write_source(tmp_path / "a", "run-x", steps=1)
        write_source(tmp_path / "copy", "run-x", steps=1, agent_done=AgentDoneStatus.ERROR)
        tasks = loader(tmp_path, sources=("*/*",), agent_outcome="done")
        assert tasks.task_ids() == ["run-x/s0/b0"]

    def test_every_unbranchable_source_is_listed_at_once(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-1", steps=1, archivers=())
        write_source(tmp_path, "run-2", steps=1, fingerprint=False)
        with pytest.raises(BenchmarkDataError, match="2 source") as info:
            loader(tmp_path).task_ids()
        assert "run-1: " in str(info.value) and "run-2: " in str(info.value)

    def test_require_archive_names_what_is_required(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=1, archivers=())
        assert loader(tmp_path, require_archive=()).task_ids() == ["run-a/s0/b0"]
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


# ── the environment setup context ────────────────────────────────────


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
            TraceBranchesTaskLoader(sources=(pattern,))

    def test_defaults(self) -> None:
        built = TraceBranchesTaskLoader()
        assert built.sources == ("*",) and built.require_archive == ("browser.network",)
        assert (built.branches_per_step, built.agent_outcome, built.on_unbranchable) == (
            1,
            "any",
            "raise",
        )
        assert built.require_eval is None and built.step_selector.selector_id == "all"
        assert built.exclude_unloaded_checkpoints is True
        assert built.benchmark_name == "trace_branches"

    @pytest.mark.parametrize(
        "knobs",
        [{"branches_per_step": 0}, {"agent_outcome": "done!"}, {"on_unbranchable": "ignore"}],
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
                "exclude_unloaded_checkpoints": False,
            }
        )
        assert config.require_eval == RequireEval(
            evaluator_id="online_mind2web", label=1, slug="online_mind2web-ab"
        )
        assert config.exclude_unloaded_checkpoints is False
        assert (
            TraceBranchesTask_LoaderConfig.model_validate(
                {"step_selector": {"selector_id": "all"}}
            ).exclude_unloaded_checkpoints
            is True
        )
        for bad in (
            {"step_selector": {"selector_id": "every"}},
            {"step_selector": {"selector_id": "all"}, "branches_per_step": 0},
            {"step_selector": {"selector_id": "all"}, "require_eval": {"evaluator": "x"}},
            {"step_selector": {"selector_id": "all"}, "on_unbranchable": "ignore"},
            {"step_selector": {"selector_id": "all"}, "exclude_unloaded_checkpoints": "maybe"},
            {},
        ):
            with pytest.raises(ValidationError):
                TraceBranchesTask_LoaderConfig.model_validate(bad)


# ── task variants: same page, different need ─────────────────────────


VARIANT_TEXT = "find the kettle's warranty terms"


def variant(
    source_run_id: str = "run-a",
    step: int = 1,
    variant_id: str = "v1",
    task_text: str = VARIANT_TEXT,
    **extra: Any,
) -> dict[str, Any]:
    """One entry of a task-variants file."""
    return {
        "source_run_id": source_run_id,
        "step": step,
        "variant_id": variant_id,
        "task_text": task_text,
        **extra,
    }


def variants_file(root: Path, *entries: dict[str, Any], **document: Any) -> Path:
    """A ``task-variants-1`` file listing *entries* (``document`` overrides its top level)."""
    path = root / "variants.json"
    path.write_text(
        json.dumps({"format": "task-variants-1", "variants": list(entries), **document})
    )
    return path


class TestTaskVariants:
    def test_each_variant_is_a_group_of_branches_under_its_own_task(self, tmp_path: Path) -> None:
        source = write_source(tmp_path / "src", "run-a", steps=3)
        path = variants_file(
            tmp_path,
            variant(step=2, variant_id="warranty"),
            variant(step=1, variant_id="v1", note="written by a model"),
            variant(step=1, variant_id="v2", task_text="compare the kettle with the red one"),
        )
        tasks = loader(tmp_path / "src", task_variants=str(path), branches_per_step=2)
        # Sources by path, steps ascending, the variants of one step in file order.
        assert tasks.task_ids() == [
            "run-a/s1/vv1/b0",
            "run-a/s1/vv1/b1",
            "run-a/s1/vv2/b0",
            "run-a/s1/vv2/b1",
            "run-a/s2/vwarranty/b0",
            "run-a/s2/vwarranty/b1",
        ]
        task = tasks["run-a/s1/vv1/b1"]
        assert task.task_text == VARIANT_TEXT
        assert task.setup == BrowserSetup(start_url=START_URL)
        assert (task.difficulty, task.tags) == ("hard", ["cart"])
        assert tasks["run-a/s1/vv2/b0"].task_text == "compare the kettle with the red one"

        context = tasks.get_task_context("run-a/s1/vv1/b1")
        # Everything but the task stays the source's.
        assert context.env == {"start_url": START_URL, "replay_archive": str(source)}
        assert context.loop == {
            "checkpoint": Checkpoint(trace_dir=str(source), step=1, task_variant="v1")
        }
        assert context.branch == BranchProvenance(
            source_run_id="run-a",
            source_trace="run-a",
            source_benchmark="online_mind2web",
            source_subset="Hard",
            source_task_id="task-001",
            checkpoint_step=1,
            branch_index=1,
            branch_group_id="run-a/s1/vv1",
            task_variant=BranchTaskVariant(
                variant_id="v1",
                task_text=VARIANT_TEXT,
                source_task_text=TASK_TEXT,
                note="written by a model",
            ),
        )
        assert dict(tasks.skipped) == {}

    def test_the_provenance_records_both_tasks(self, tmp_path: Path) -> None:
        """What a trace's ``config.json`` carries: the variant with both task texts."""
        write_source(tmp_path / "src", "run-a", steps=2)
        path = variants_file(tmp_path, variant())
        tasks = loader(tmp_path / "src", task_variants=str(path))
        branch = tasks.get_task_context("run-a/s1/vv1/b0").branch
        assert branch is not None
        assert branch.model_dump(mode="json")["task_variant"] == {
            "variant_id": "v1",
            "task_text": VARIANT_TEXT,
            "source_task_text": TASK_TEXT,
            "note": None,
        }
        # A branch that keeps its source's task writes no task_variant at all.
        plain = loader(tmp_path / "src").get_task_context("run-a/s1/b0").branch
        assert plain is not None and "task_variant" not in plain.model_dump(mode="json")

    def test_the_file_names_the_checkpoints(self, tmp_path: Path) -> None:
        """A candidate no variant names gives no task, and no reason: nothing was asked."""
        write_source(tmp_path / "src", "run-a", steps=4)
        write_source(tmp_path / "src", "run-b", steps=2)
        path = variants_file(tmp_path, variant(step=3), variant("run-b", step=0))
        tasks = loader(tmp_path / "src", task_variants=str(path))
        assert tasks.task_ids() == ["run-a/s3/vv1/b0", "run-b/s0/vv1/b0"]
        assert dict(tasks.skipped) == {}

    def test_the_selector_still_chooses_among_the_candidates(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=4)
        path = variants_file(tmp_path, variant(step=1), variant(step=3))
        tasks = loader(tmp_path / "src", task_variants=str(path), step_selector=LastStep())
        assert tasks.task_ids() == ["run-a/s3/vv1/b0"]
        assert tasks.skipped == {
            "run-a/s1/vv1": "run-a: step 1 is a candidate step_selector 'last' did not choose"
        }

    @pytest.mark.parametrize(
        ("entries", "key", "reason"),
        [
            (
                [variant(), variant(task_text="another text"), variant(variant_id="v2")],
                "run-a/s1/vv1",
                "task variant 'v1' is listed 2 times for run 'run-a' step 1",
            ),
            (
                [variant(task_text="  \n"), variant(variant_id="v2")],
                "run-a/s1/vv1",
                "empty task_text",
            ),
            (
                [variant("run-zz"), variant(variant_id="v2")],
                "run-zz/s1/vv1",
                "no readable source run under",
            ),
            (
                [variant(step=3), variant(variant_id="v2")],
                "run-a/s3/vv1",
                "run-a: step 3 is a terminal step",
            ),
            (
                [variant(step=9), variant(variant_id="v2")],
                "run-a/s9/vv1",
                "run-a: step 9 is not committed (its last committed step is 3)",
            ),
            (
                [variant(step=2), variant(variant_id="v2")],
                "run-a/s2/vv1",
                "run-a: step 2's observation was not recorded",
            ),
        ],
        ids=[
            "duplicate",
            "empty_task",
            "unknown_run",
            "terminal",
            "past_the_end",
            "no_observation",
        ],
    )
    def test_a_variant_that_cannot_be_used_is_left_out_with_its_reason(
        self, tmp_path: Path, entries: list[dict[str, Any]], key: str, reason: str
    ) -> None:
        write_source(
            tmp_path / "src",
            "run-a",
            steps=[Step(), Step(), Step(error_phase="observe"), Step("terminal")],
        )
        tasks = loader(tmp_path / "src", task_variants=str(variants_file(tmp_path, *entries)))
        assert tasks.task_ids() == ["run-a/s1/vv2/b0"], "the rest of the file still branches"
        assert reason in tasks.skipped[key]
        assert set(tasks.skipped) == {key}

    def test_every_copy_of_a_duplicate_is_left_out(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=2)
        path = variants_file(tmp_path, variant(), variant(task_text="another text"))
        tasks = loader(tmp_path / "src", task_variants=str(path))
        assert tasks.task_ids() == [] and "listed 2 times" in tasks.skipped["run-a/s1/vv1"]

    def test_a_variant_of_a_source_left_out_says_why(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=2, agent_done=AgentDoneStatus.ERROR)
        write_source(tmp_path / "src", "run-b", steps=2, archivers=())
        path = variants_file(tmp_path, variant(), variant("run-b"))
        tasks = loader(
            tmp_path / "src",
            task_variants=str(path),
            agent_outcome="done",
            on_unbranchable="skip",
        )
        assert tasks.task_ids() == []
        assert tasks.skipped["run-a/s1/vv1"].startswith(
            "its source run 'run-a' is left out (run-a: filtered: its agent ended "
        )
        assert "(run-b: it did not record 'browser.network'" in tasks.skipped["run-b/s1/vv1"]

    def test_with_no_source_every_variant_is_left_out(self, tmp_path: Path) -> None:
        path = variants_file(tmp_path, variant())
        tasks = loader(tmp_path / "empty", task_variants=str(path))
        assert tasks.task_ids() == []
        assert "no readable source run under" in tasks.skipped["run-a/s1/vv1"]

    @pytest.mark.parametrize(
        ("content", "problem"),
        [
            ("not json", "is not JSON"),
            ("[]", "must be a JSON object"),
            (json.dumps({"variants": []}), "has format None"),
            (json.dumps({"format": "task-variants-2", "variants": []}), "'task-variants-2'"),
            (json.dumps({"format": "task-variants-1"}), "variants: Field required"),
            (
                json.dumps({"format": "task-variants-1", "variants": [], "by": "me"}),
                "by: Extra inputs are not permitted",
            ),
            (
                json.dumps({"format": "task-variants-1", "variants": [{**variant(), "step": "1"}]}),
                "variants.0.step",
            ),
            (
                json.dumps({"format": "task-variants-1", "variants": [variant(why="x")]}),
                "variants.0.why: Extra inputs are not permitted",
            ),
            (
                json.dumps({"format": "task-variants-1", "variants": [variant(), {"step": 1}]}),
                "variants.1.source_run_id: Field required",
            ),
            (
                json.dumps({"format": "task-variants-1", "variants": [variant(variant_id="a/b")]}),
                "variants.0.variant_id",
            ),
            (
                json.dumps({"format": "task-variants-1", "variants": [variant(variant_id="")]}),
                "variants.0.variant_id",
            ),
        ],
        ids=[
            "not_json",
            "not_an_object",
            "no_format",
            "unknown_format",
            "no_variants",
            "unknown_top_level_field",
            "step_not_an_integer",
            "unknown_entry_field",
            "missing_entry_field",
            "variant_id_with_a_slash",
            "empty_variant_id",
        ],
    )
    def test_a_malformed_file_fails_the_scan_naming_the_problem(
        self, tmp_path: Path, content: str, problem: str
    ) -> None:
        write_source(tmp_path / "src", "run-a", steps=2)
        path = tmp_path / "variants.json"
        path.write_text(content)
        tasks = loader(tmp_path / "src", task_variants=str(path))
        with pytest.raises(BenchmarkDataError) as info:
            tasks.task_ids()
        assert problem in str(info.value)
        assert "task_variants file" in str(info.value) and str(path) in str(info.value)

    def test_a_missing_file_fails_the_scan(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=2)
        tasks = loader(tmp_path / "src", task_variants=str(tmp_path / "nope.json"))
        with pytest.raises(BenchmarkDataError, match="cannot be read"):
            tasks.task_ids()

    def test_constructing_reads_no_variants_file(self, tmp_path: Path) -> None:
        built = TraceBranchesTaskLoader(task_variants=str(tmp_path / "not-yet.json"))
        assert built.task_variants == str(tmp_path / "not-yet.json")
        assert built.task_variants_path() == (tmp_path / "not-yet.json").resolve()
        assert TraceBranchesTaskLoader().task_variants_path() is None

    def test_a_relative_path_is_relative_to_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_source(tmp_path / "src", "run-a", steps=2)
        variants_file(tmp_path, variant())
        monkeypatch.chdir(tmp_path)
        tasks = TraceBranchesTaskLoader(data_dir="src", task_variants="variants.json")
        assert tasks.task_ids() == ["run-a/s1/vv1/b0"]

    def test_the_yaml_surface(self, tmp_path: Path) -> None:
        from spectragent.core.benchmark_runner import build_composer

        write_source(tmp_path / "src", "run-a", steps=2)
        path = variants_file(tmp_path, variant(step=0))
        built = (
            build_composer()
            .compose(
                {
                    "task_loader": [
                        {
                            "benchmark_name": "trace_branches",
                            "data_dir": str(tmp_path / "src"),
                            "step_selector": {"selector_id": "all"},
                            "task_variants": str(path),
                        }
                    ]
                }
            )
            .optional_single("task_loader")
        )
        assert isinstance(built, TraceBranchesTaskLoader)
        assert built.task_ids() == ["run-a/s0/vv1/b0"]


# ── excluded runs: recordings not to be branched ─────────────────────


def exclusions_file(root: Path, *entries: dict[str, Any], **document: Any) -> Path:
    """A ``run-exclusions-1`` file listing *entries* (``document`` overrides its top level)."""
    path = root / "exclusions.json"
    path.write_text(
        json.dumps({"format": "run-exclusions-1", "runs": list(entries), **document}),
        encoding="utf-8",
    )
    return path


def excluded(run_id: str, reason: str = "its page never rendered", **extra: Any) -> dict[str, Any]:
    return {"run_id": run_id, "reason": reason, **extra}


class TestExcludeRuns:
    def test_a_listed_source_is_filtered_out_with_its_reason(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=2)
        write_source(tmp_path / "src", "run-b", steps=2)
        path = exclusions_file(tmp_path, excluded("run-a", note="written by a health check"))
        tasks = loader(tmp_path / "src", exclude_runs=str(path))
        assert tasks.task_ids() == ["run-b/s0/b0", "run-b/s1/b0"]
        assert tasks.skipped["run-a"] == (
            "filtered: the exclude_runs file lists its run id: its page never rendered"
        )

    def test_every_reason_of_a_run_listed_twice(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=1)
        path = exclusions_file(
            tmp_path, excluded("run-a"), excluded("run-a", "requests failed"), excluded("run-a")
        )
        tasks = loader(tmp_path / "src", exclude_runs=str(path))
        assert tasks.task_ids() == []
        assert tasks.skipped["run-a"].endswith("its page never rendered; requests failed")

    def test_an_excluded_source_never_raises(self, tmp_path: Path) -> None:
        """Like every filter: an excluded source is left out even when it could not be branched."""
        write_source(tmp_path / "src", "run-a", steps=1, archivers=())
        path = exclusions_file(tmp_path, excluded("run-a"))
        assert loader(tmp_path / "src", exclude_runs=str(path)).task_ids() == []

    def test_a_listed_run_id_no_source_has_changes_nothing(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=1)
        path = exclusions_file(tmp_path, excluded("run-z"))
        tasks = loader(tmp_path / "src", exclude_runs=str(path))
        assert tasks.task_ids() == ["run-a/s0/b0"] and dict(tasks.skipped) == {}

    def test_the_variants_of_an_excluded_source_are_left_out(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=2)
        tasks = loader(
            tmp_path / "src",
            task_variants=str(variants_file(tmp_path, variant())),
            exclude_runs=str(exclusions_file(tmp_path, excluded("run-a"))),
        )
        assert tasks.task_ids() == []
        assert tasks.skipped["run-a/s1/vv1"] == (
            "its source run 'run-a' is left out (run-a: filtered: the exclude_runs file lists "
            "its run id: its page never rendered)"
        )

    @pytest.mark.parametrize(
        ("content", "problem"),
        [
            ("not json", "is not JSON"),
            ("[]", "must be a JSON object"),
            (json.dumps({"runs": []}), "has format None"),
            (json.dumps({"format": "run-exclusions-2", "runs": []}), "'run-exclusions-2'"),
            (json.dumps({"format": "run-exclusions-1"}), "runs: Field required"),
            (
                json.dumps({"format": "run-exclusions-1", "runs": [], "by": "me"}),
                "by: Extra inputs are not permitted",
            ),
            (
                json.dumps({"format": "run-exclusions-1", "runs": [excluded("run-a", why="x")]}),
                "runs.0.why: Extra inputs are not permitted",
            ),
            (
                json.dumps({"format": "run-exclusions-1", "runs": [{"run_id": "run-a"}]}),
                "runs.0.reason: Field required",
            ),
            (
                json.dumps({"format": "run-exclusions-1", "runs": [excluded("")]}),
                "runs.0.run_id",
            ),
            (
                json.dumps({"format": "run-exclusions-1", "runs": [excluded("run-a", "")]}),
                "runs.0.reason",
            ),
            (
                json.dumps({"format": "run-exclusions-1", "runs": [{"run_id": 7, "reason": "x"}]}),
                "runs.0.run_id",
            ),
        ],
        ids=[
            "not_json",
            "not_an_object",
            "no_format",
            "unknown_format",
            "no_runs",
            "unknown_top_level_field",
            "unknown_entry_field",
            "no_reason",
            "empty_run_id",
            "empty_reason",
            "run_id_not_a_string",
        ],
    )
    def test_a_malformed_file_fails_the_scan_naming_the_problem(
        self, tmp_path: Path, content: str, problem: str
    ) -> None:
        write_source(tmp_path / "src", "run-a", steps=1)
        path = tmp_path / "exclusions.json"
        path.write_text(content, encoding="utf-8")
        tasks = loader(tmp_path / "src", exclude_runs=str(path))
        with pytest.raises(BenchmarkDataError) as info:
            tasks.task_ids()
        assert problem in str(info.value)
        assert "exclude_runs file" in str(info.value) and str(path) in str(info.value)

    def test_a_missing_file_fails_the_scan(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=1)
        tasks = loader(tmp_path / "src", exclude_runs=str(tmp_path / "nope.json"))
        with pytest.raises(BenchmarkDataError, match="cannot be read"):
            tasks.task_ids()

    def test_constructing_reads_no_exclusions_file(self, tmp_path: Path) -> None:
        built = TraceBranchesTaskLoader(exclude_runs=str(tmp_path / "not-yet.json"))
        assert built.exclude_runs_path() == (tmp_path / "not-yet.json").resolve()
        assert TraceBranchesTaskLoader().exclude_runs_path() is None

    def test_a_relative_path_is_relative_to_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_source(tmp_path / "src", "run-a", steps=1)
        write_source(tmp_path / "src", "run-b", steps=1)
        exclusions_file(tmp_path, excluded("run-a"))
        monkeypatch.chdir(tmp_path)
        tasks = TraceBranchesTaskLoader(data_dir="src", exclude_runs="exclusions.json")
        assert tasks.task_ids() == ["run-b/s0/b0"]

    def test_the_yaml_surface(self, tmp_path: Path) -> None:
        from spectragent.core.benchmark_runner import build_composer

        write_source(tmp_path / "src", "run-a", steps=1)
        write_source(tmp_path / "src", "run-b", steps=1)
        path = exclusions_file(tmp_path, excluded("run-b"))
        built = (
            build_composer()
            .compose(
                {
                    "task_loader": [
                        {
                            "benchmark_name": "trace_branches",
                            "data_dir": str(tmp_path / "src"),
                            "step_selector": {"selector_id": "all"},
                            "exclude_runs": str(path),
                        }
                    ]
                }
            )
            .optional_single("task_loader")
        )
        assert isinstance(built, TraceBranchesTaskLoader)
        assert built.task_ids() == ["run-a/s0/b0"]


# ── checkpoints captured before the page finished loading ────────────

#: Why step 1 of a source recorded with :data:`NOT_LOADED` is left out.
_UNLOADED_AT_1 = (
    "step 1 was captured before its initial load finished (browser.capture_state: the wait "
    "ended capped after 5002 ms) (exclude_unloaded_checkpoints)"
)


class TestUnloadedCheckpoints:
    """``exclude_unloaded_checkpoints`` (§4.10, §4.4 "初次加载是否完成")."""

    def _three(self, middle: Any = NOT_LOADED) -> list[Step]:
        settled = Step(capture=None)
        return [settled, Step(capture=middle), settled]

    def test_an_unloaded_checkpoint_is_left_out_with_its_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_source(tmp_path, "run-a", steps=self._three())
        with caplog.at_level("INFO"):
            tasks = loader(tmp_path)
            assert tasks.task_ids() == ["run-a/s0/b0", "run-a/s2/b0"]
        assert dict(tasks.skipped) == {"run-a#s1": _UNLOADED_AT_1}
        assert "run-a#s1 left out" in caplog.text

    def test_off_every_candidate_is_a_checkpoint(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=self._three())
        tasks = loader(tmp_path, exclude_unloaded_checkpoints=False)
        assert tasks.task_ids() == ["run-a/s0/b0", "run-a/s1/b0", "run-a/s2/b0"]
        assert dict(tasks.skipped) == {}

    @pytest.mark.parametrize(
        "middle",
        [NEVER_QUIET, CAPTURE_FAILED, PageSettle(waited_ms=0, outcome="unknown", detail="x")],
        ids=["a DOM that never went quiet", "capture failed", "unknown"],
    )
    def test_no_evidence_of_an_unloaded_page_keeps_the_checkpoint(
        self, tmp_path: Path, middle: Any
    ) -> None:
        write_source(tmp_path, "run-a", steps=self._three(middle))
        tasks = loader(tmp_path)
        assert tasks.task_ids() == ["run-a/s0/b0", "run-a/s1/b0", "run-a/s2/b0"]
        assert dict(tasks.skipped) == {}

    def test_a_step_without_a_recorded_capture_state_is_kept(self, tmp_path: Path) -> None:
        """Negotiated by the run, but not recorded at this step: no evidence, no refusal."""
        write_source(tmp_path, "run-a", steps=[Step(), Step(capture=NOT_LOADED), Step()])
        tasks = loader(tmp_path, step_selector=ExplicitSteps(steps=[0, 2]))
        assert tasks.task_ids() == ["run-a/s0/b0", "run-a/s2/b0"]
        assert dict(tasks.skipped) == {}

    def test_a_run_that_never_negotiated_the_capture_state_is_kept(self, tmp_path: Path) -> None:
        write_source(tmp_path, "run-a", steps=3)
        assert loader(tmp_path).task_ids() == ["run-a/s0/b0", "run-a/s1/b0", "run-a/s2/b0"]

    def test_it_applies_after_the_selector(self, tmp_path: Path) -> None:
        """``last`` chose the unloaded step: the source gives no checkpoint, not an earlier one."""
        write_source(tmp_path, "run-a", steps=[Step(), Step(), Step(capture=NOT_LOADED)])
        tasks = loader(tmp_path, step_selector=LastStep())
        assert tasks.task_ids() == []
        assert set(tasks.skipped) == {"run-a#s2"}
        assert "exclude_unloaded_checkpoints" in tasks.skipped["run-a#s2"]

    def test_a_variant_at_an_unloaded_checkpoint_is_left_out(self, tmp_path: Path) -> None:
        write_source(tmp_path / "src", "run-a", steps=self._three())
        path = variants_file(
            tmp_path, variant(step=1, variant_id="v1"), variant(step=2, variant_id="v2")
        )
        tasks = loader(tmp_path / "src", task_variants=str(path))
        assert tasks.task_ids() == ["run-a/s2/vv2/b0"]
        assert dict(tasks.skipped) == {"run-a/s1/vv1": f"run-a: {_UNLOADED_AT_1}"}

    def test_a_capture_state_that_does_not_read_back_makes_the_source_unbranchable(
        self, tmp_path: Path
    ) -> None:
        source = write_source(tmp_path, "run-a", steps=self._three())
        (recorded,) = source.rglob("*browser.capture_state.json")
        recorded.write_text('{"settle": "not a record"}')
        with pytest.raises(BenchmarkDataError, match="step 1's capture state does not read back"):
            loader(tmp_path).task_ids()
        skipping = loader(tmp_path, on_unbranchable="skip")
        assert skipping.task_ids() == []
        assert "capture state does not read back" in skipping.skipped["run-a"]

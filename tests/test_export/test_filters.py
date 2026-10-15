"""Which recorded runs an export reads (arch §4.15 "源级检查与筛选").

``skipped``: what cannot be a source at all (unreadable, format 1,
``secrets_recorded``, no negotiation, a shared run id). ``filtered``: what the
``filters`` leave out (status, evaluation — a branch by its source run's —,
diverged restoration, replay document misses, sites). Every source here is
committed by the real ``WALWriter``.
"""

from __future__ import annotations

import json
from pathlib import Path

from spectragent.core.capabilities.browser.archive import ReplayLogArchiver
from spectragent.core.capabilities.browser.replay.replay_log import ReplayLog, ReplayLogEntry
from spectragent.core.data_loader.task.require_eval import RequireEval
from spectragent.core.trace.encoding import encode_archive_file
from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.sources import task_identity
from spectragent.core.trace.wal import WALWriter
from spectragent.export.canonical import canonicalise
from spectragent.export.filters import Source, filtered_out, open_sources
from spectragent.export.settings import ExportFilters
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.restoration import BranchProvenance, RestorationCheck
from tests._helpers.archive_recorder import open_session
from tests._helpers.export_traces import (
    START_URL,
    ExportStep,
    click_at,
    setup_record,
    ui_tars_step,
    write_ui_tars_run,
)
from tests._helpers.trace_builder import eval_result

GRADED = RequireEval(evaluator_id="online_mind2web", label=1)


def _sources(root: Path) -> dict[str, Source]:
    sources, skipped = open_sources(root, ["*"])
    assert skipped == {}
    return {source.rel: source for source in sources}


def _one(root: Path, run_id: str, **kwargs: object) -> Path:
    return write_ui_tars_run(root, run_id, [ui_tars_step(click_at(1, 1))], **kwargs)  # type: ignore[arg-type]


def _branch(source_run_id: str, step: int = 1) -> BranchProvenance:
    return BranchProvenance(
        source_run_id=source_run_id,
        source_trace=source_run_id,
        source_benchmark="online_mind2web",
        source_subset="test",
        source_task_id="task-001",
        checkpoint_step=step,
        branch_index=0,
        branch_group_id=f"{source_run_id}/s{step}",
    )


class TestSkipped:
    def test_secrets_recorded_older_format_no_negotiation_unreadable_unclosed(
        self, tmp_path: Path
    ) -> None:
        _one(tmp_path, "ok")
        _one(tmp_path, "secrets", secrets_recorded=True)
        _one(tmp_path, "hand-made", negotiation=None)
        _one(tmp_path, "writing", status=TraceStatus.WRITING, agent_done=None)
        old = _one(tmp_path, "old")
        manifest = json.loads((old / "manifest.json").read_text())
        manifest["format_version"] = 2
        (old / "manifest.json").write_text(json.dumps(manifest))
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "manifest.json").write_text("{not json")
        (tmp_path / "not-a-run").mkdir()  # no manifest: not even listed

        sources, skipped = open_sources(tmp_path, ["*"])
        assert [s.rel for s in sources] == ["ok"]
        assert sorted(skipped) == ["broken", "hand-made", "old", "secrets", "writing"]
        assert skipped["secrets"].startswith("recorded with record_secrets: true")
        assert skipped["hand-made"] == "no recorded negotiation (an assembly made by hand)"
        assert skipped["old"] == (
            "not a readable trace: manifest.json does not read back (TraceSchemaError)"
        ), "an older format is not read at all"
        assert skipped["writing"].startswith("its trace was never closed (status writing)")
        assert skipped["broken"] == (
            "not a readable trace: manifest.json does not read back (TraceSchemaError)"
        ), "the run-relative file, never the reader's absolute path"

    def test_a_shared_run_id_skips_every_sharer(self, tmp_path: Path) -> None:
        _one(tmp_path / "a", "same")
        _one(tmp_path / "b", "same")
        _one(tmp_path / "c", "other")
        sources, skipped = open_sources(tmp_path, ["*/*"])
        assert [s.rel for s in sources] == ["c/other"]
        assert sorted(skipped) == ["a/same", "b/same"]
        assert "also the run id of b/same" in skipped["a/same"]


class TestFiltered:
    def test_agent_outcome(self, tmp_path: Path) -> None:
        """How the run's agent ended is a filter, ``any`` by default."""
        _one(tmp_path, "done")
        _one(tmp_path, "failed", agent_done=AgentDoneStatus.ERROR)
        sources = _sources(tmp_path)
        assert filtered_out(sources["done"], ExportFilters(), sources) is None
        assert filtered_out(sources["failed"], ExportFilters(), sources) is None
        only_done = ExportFilters(agent_outcome="done")
        assert filtered_out(sources["done"], only_done, sources) is None
        assert filtered_out(sources["failed"], only_done, sources) == (
            "its agent ended error (agent_outcome: done)"
        )

    def test_an_incomplete_trace_exports_the_steps_it_is_complete_through(
        self, tmp_path: Path
    ) -> None:
        """The hard condition: past ``complete_through`` no step is a sample, whatever the filters."""
        source = write_ui_tars_run(
            tmp_path,
            "holed",
            [
                ui_tars_step(click_at(1, 1)),
                ui_tars_step(click_at(2, 2)),
                ui_tars_step(click_at(3, 3)),
            ],
            status=TraceStatus.INCOMPLETE,
            incomplete_reason="recording failed at step 2: OSError: disk full",
            agent_done=AgentDoneStatus.ERROR,
        )
        path = source / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["complete_through"] = 1  # what a hole at step 2 leaves
        path.write_text(json.dumps(manifest))
        sources = _sources(tmp_path)
        assert filtered_out(sources["holed"], ExportFilters(), sources) is None
        samples = canonicalise(sources["holed"].trace, rel_path="holed")
        assert [sample.step for sample in samples] == [0, 1]

    def test_require_eval(self, tmp_path: Path) -> None:
        _one(tmp_path, "passed", verdicts={"j": eval_result(evaluator_id="online_mind2web")})
        _one(
            tmp_path,
            "failed",
            verdicts={"j": eval_result(evaluator_id="online_mind2web", predicted_label=0)},
        )
        _one(
            tmp_path,
            "ungraded",
            verdicts={
                "j": eval_result(evaluator_id="online_mind2web", verdict_source="evaluation_error")
            },
        )
        sources = _sources(tmp_path)
        need = ExportFilters(require_eval=GRADED)
        assert filtered_out(sources["passed"], need, sources) is None
        for rel in ("failed", "ungraded"):
            assert filtered_out(sources[rel], need, sources) == (
                "it has no graded verdict from 'online_mind2web' with predicted_label 1 "
                "(require_eval)"
            )

    def test_require_eval_reads_the_branch_source_run(self, tmp_path: Path) -> None:
        _one(tmp_path, "src", verdicts={"j": eval_result(evaluator_id="online_mind2web")})
        _one(tmp_path, "src-s1-b0", branch=_branch("src"))
        _one(tmp_path, "orphan-s1-b0", branch=_branch("gone"))
        _one(
            tmp_path,
            "bad",
            verdicts={"j": eval_result(evaluator_id="online_mind2web", predicted_label=0)},
        )
        _one(tmp_path, "bad-s1-b0", branch=_branch("bad"))
        sources = _sources(tmp_path)
        need = ExportFilters(require_eval=GRADED)
        assert filtered_out(sources["src-s1-b0"], need, sources) is None  # its own evals/: none
        assert filtered_out(sources["orphan-s1-b0"], need, sources) == (
            "branch source 'gone' not among the sources"
        )
        assert filtered_out(sources["orphan-s1-b0"], ExportFilters(), sources) == (
            "branch source 'gone' not among the sources"
        ), "a branch's context is its source run's prefix: no source, no sample"
        assert filtered_out(sources["bad-s1-b0"], need, sources) == (
            "its source run bad has no graded verdict from 'online_mind2web' with "
            "predicted_label 1 (require_eval)"
        )

    def test_restoration_diverged(self, tmp_path: Path) -> None:
        """Any check that did not pass excludes the run — ``warn`` above all.

        A ``warn`` branch went on from a state that is not the recorded one,
        so its policy step's context (the source run's prefix) is not what
        it decided on. ``strict`` (``diverged``) is excluded too, though its
        run never reaches a policy step (next test).
        """
        passed = RestorationCheck(step=0, verdict="ok")
        warned = RestorationCheck(
            step=0,
            verdict="warn",
            reason="url_mismatch",
            reasons=["url_mismatch", "screenshot_distance"],
        )
        diverged = warned.model_copy(update={"verdict": "diverged"})
        _one(tmp_path, "src")
        for name, check in (("passed", passed), ("warned", warned), ("diverged", diverged)):
            write_ui_tars_run(
                tmp_path,
                name,
                [
                    ExportStep(actions=[click_at(1, 1)], origin="restored", restoration=check),
                    ui_tars_step(click_at(2, 2)),
                ],
                branch=_branch("src"),
            )
        sources = _sources(tmp_path)
        assert filtered_out(sources["passed"], ExportFilters(), sources) is None
        assert filtered_out(sources["warned"], ExportFilters(), sources) == (
            "restoration diverged at step(s) 0 (warn: url_mismatch, screenshot_distance) "
            "(exclude_restoration_diverged)"
        )
        assert filtered_out(sources["diverged"], ExportFilters(), sources) == (
            "restoration diverged at step(s) 0 (diverged: url_mismatch, screenshot_distance) "
            "(exclude_restoration_diverged)"
        )
        keep = ExportFilters(exclude_restoration_diverged=False)
        for name in ("warned", "diverged"):
            assert filtered_out(sources[name], keep, sources) is None

    def test_a_strict_divergence_never_reaches_a_policy_step(self, tmp_path: Path) -> None:
        """What ``strict`` leaves behind has no sample, whatever the filters.

        ``BranchLoop`` ends a run whose restoration diverged under ``strict``
        on the restored step (``error_phase="restore"``, ``env_status`` failed):
        its agent ended in error and no policy step was committed. So the
        restoration filter is not what keeps it out of a dataset — the
        ``warn`` case above is the one only it catches.
        """
        diverged = RestorationCheck(
            step=1, verdict="diverged", reason="url_mismatch", reasons=["url_mismatch"]
        )
        _one(tmp_path, "src")
        write_ui_tars_run(
            tmp_path,
            "src-s1-b0",
            [
                ExportStep(
                    actions=[click_at(1, 1)],
                    origin="restored",
                    restoration=RestorationCheck(step=0, verdict="ok"),
                ),
                ExportStep(origin="restored", error_phase="restore", restoration=diverged),
            ],
            branch=_branch("src"),
            agent_done=AgentDoneStatus.ERROR,
        )
        sources = _sources(tmp_path)
        strict = sources["src-s1-b0"]
        done_only = ExportFilters(exclude_restoration_diverged=False, agent_outcome="done")
        assert filtered_out(strict, done_only, sources) == (
            "its agent ended error (agent_outcome: done)"
        )
        anything = ExportFilters(exclude_restoration_diverged=False)
        assert filtered_out(strict, anything, sources) is None
        assert canonicalise(strict.trace, rel_path=strict.rel) == [], "no exportable step"

    async def test_max_document_misses(self, tmp_path: Path) -> None:
        log = ReplayLog(repeat_limit=1)
        session, _ = open_session(ReplayLogArchiver(), replay_log=log)
        document = ReplayLogEntry(
            kind="miss",
            captured_at=1.0,
            cycle=1,
            method="GET",
            url="https://shop.example.test/",
            resource_type="Document",
            frame="main",
            request_class="document",
            reason="no_match",
            answer="failed",
            diverged=True,
        )
        for _ in range(3):
            log.append(document)  # two of the three fold into a summary
        extra = [encode_archive_file(f, step=0) for f in await session.collect()]
        write_ui_tars_run(
            tmp_path,
            "replayed",
            [ExportStep(actions=[click_at(1, 1)], extra_files=extra)],
        )
        _one(tmp_path, "live")
        sources = _sources(tmp_path)
        assert filtered_out(sources["live"], ExportFilters(), sources) is None
        assert filtered_out(sources["replayed"], ExportFilters(), sources) == (
            "3 main-frame document miss(es) in replay (max_document_misses: 0)"
        )
        assert (
            filtered_out(sources["replayed"], ExportFilters(max_document_misses=3), sources) is None
        )
        assert (
            filtered_out(
                sources["replayed"], ExportFilters(max_document_misses="unlimited"), sources
            )
            is None
        )

    def test_sites(self, tmp_path: Path) -> None:
        _one(tmp_path, "shop")
        sources = _sources(tmp_path)
        assert task_identity(sources["shop"].config).site == "shop.example.test"
        for entry in ("example.test", "shop.example.test", "SHOP.example.test"):
            reason = filtered_out(sources["shop"], ExportFilters(exclude_sites=(entry,)), sources)
            assert reason == f"site shop.example.test is excluded ({entry!r} in exclude_sites)"
        for entry in ("ample.test", "other.example.test"):
            assert (
                filtered_out(sources["shop"], ExportFilters(exclude_sites=(entry,)), sources)
                is None
            )


def test_a_branch_is_keyed_by_its_source_task(tmp_path: Path) -> None:
    run = _one(tmp_path, "src-s1-b0", branch=_branch("src"), task_setup=setup_record())
    (source,) = open_sources(tmp_path, ["*"])[0]
    identity = task_identity(source.config)
    assert run.name == "src-s1-b0"
    assert (identity.benchmark, identity.subset, identity.task_id) == (
        "online_mind2web",
        "test",
        "task-001",
    )
    assert identity.key == "online_mind2web/test/task-001"
    assert identity.start_url == START_URL and identity.site == "shop.example.test"


def test_light_recording_is_skipped_as_out_of_scope_not_corrupt(tmp_path: Path) -> None:
    wal = WALWriter(tmp_path, "light")
    wal.init_run(TraceConfig(recording=RecordingPlan(trajectory=False, observations=False)))
    wal.finalize(TraceStatus.COMPLETE)
    sources, skipped = open_sources(tmp_path, ["*"])
    assert sources == []
    assert "did not select" in skipped["light"]
    assert "trajectory, observations" in skipped["light"]
    assert "unreadable" not in skipped["light"]

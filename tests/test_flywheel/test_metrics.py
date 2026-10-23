"""The report's metrics over a chain root of WAL-committed runs with known answers (arch §4.16).

The chain is ``tests/_helpers/flywheel_traces.py::known_chain``; every
expected number below is derived in that module's docstring from the runs it
commits. Beside each metric: "not measured" is ``null`` with a reason, never
``0``; what was not committed is invisible; a folded replay-log summary
counts every entry it folds.
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ExchangeLine, load_archive
from spectragent.core.capabilities.browser.settle import PageSettle
from spectragent.core.llm.messages import (
    AssistantMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.trace.digest import tree_digest
from spectragent.core.trace.encoding import (
    EncodedLLMCall,
    PendingLLMCall,
    TraceFile,
    encode_call_metrics,
    encode_llm_call,
)
from spectragent.core.trace.models import TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.secret_patterns import iter_readable
from spectragent.core.trace.wal import WALWriter
from spectragent.flywheel.metrics import (
    NO_RESULT,
    ROLES,
    UNREADABLE_RESULT,
    ChainLayout,
    ChainRuns,
    DatasetFormatError,
    Planted,
    Privacy,
    TokenUse,
    agent_outcome,
    archive_bytes,
    archive_overhead,
    body_completeness_metric,
    branch_coverage,
    branch_document_misses,
    duplicate_branches,
    empty_branch,
    ended_unclean,
    env_times,
    export_determinism,
    export_yield,
    open_chain,
    open_role,
    percentile,
    privacy,
    ratio,
    read_dataset_manifest,
    replay_determinism,
    request_texts,
    restoration,
    restoration_rate,
    role_token_use,
    run_tier,
    same_path_fidelity,
    settle_wait,
    sites,
    storage,
)
from spectragent.flywheel.report import NO_SPEC_LIMIT, build_report, render_markdown, report_json
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.step_record import StepRecord
from tests._helpers.flywheel_traces import (
    BIG,
    STYLE,
    KnownChain,
    Step,
    check,
    click,
    config,
    miss,
    provenance,
    write_plain,
    write_replayed,
)
from tests._helpers.fragment_samples import PNG_BYTES
from tests._helpers.trace_builder import (
    agent_result,
    archive_file,
    as_chat,
    prompt,
    read_back,
    step_record,
    usage,
)
from tests.test_flywheel.conftest import empty_layout


def screenshot_file(run_dir: Path, step: int) -> Path:
    """*step*'s committed screenshot artifact, as the manifest lists it."""
    (entry,) = [item for item in RunTrace(run_dir).manifest.steps if item.step == step]
    artifact = entry.fragments["browser.screenshot"].artifact
    assert artifact is not None
    return run_dir / artifact


def damaged_copy(known: KnownChain, tmp_path: Path) -> Path:
    """A copy of the known chain root, to damage."""
    root = tmp_path / "chain"
    shutil.copytree(known.root, root)
    return root


class TestSettleWait:
    """The observation's settle wait, read from the record every browser capture carries."""

    @staticmethod
    def _role(tmp_path: Path) -> Any:
        settled = PageSettle(waited_ms=600, outcome="settled")
        write_plain(
            tmp_path,
            "shop",
            [
                Step(duration_ms=3000, color=10, settle=settled),
                Step(
                    duration_ms=6000,
                    color=20,
                    settle=PageSettle(waited_ms=5000, outcome="capped", waiting_for=["dom"]),
                ),
                Step(
                    "terminal",
                    color=30,
                    settle=PageSettle(waited_ms=2, outcome="unknown", detail="no world"),
                ),
            ],
            task_id="shop-1",
            site="shop.test",
        )
        write_plain(
            tmp_path,
            "news",
            [Step(color=40, settle=PageSettle(waited_ms=900, outcome="settled")), Step(color=50)],
            task_id="news-1",
            site="news.test",
        )
        write_plain(tmp_path, "blank", [Step(duration_ms=100)], task_id="b-1", site="b.test")
        return open_role(tmp_path, "record")

    def test_p50_p95_and_the_caps_share(self, tmp_path: Path) -> None:
        found = settle_wait(self._role(tmp_path))
        assert found.model_dump() == {
            "role": "record",
            "runs": 3,
            "observations": 4,  # a step without a record (news's second, blank's) is left out
            "sites": 2,
            "p50_ms": 900.0,  # nearest rank of [2, 600, 900, 5000]
            "p95_ms": 5000.0,
            "capped": {"numerator": 1, "denominator": 4, "value": 0.25, "reason": None},
            "unknown": 1,
            "skipped": {},
        }

    def test_the_report_shows_it_under_archive_overhead(self, tmp_path: Path) -> None:
        root = empty_layout(tmp_path).root
        self._role(root / "traces" / "record")
        report = build_report(ChainLayout(root), evidence="rehearsal")
        (record,) = report.metrics.archive_overhead.settle_wait
        assert (record.role, record.observations) == ("record", 4)
        assert (
            "- settle wait before each observation (record): p50 900 ms, p95 5000 ms over "
            "4 observation(s) of 3 run(s); ended by the cap: 0.250 (1/4)"
        ) in render_markdown(report)
        document = json.loads(report_json(report))
        assert document["metrics"]["archive_overhead"]["settle_wait"][0]["capped"]["numerator"] == 1


class TestShapes:
    def test_a_ratio_over_nothing_is_null_with_its_reason(self) -> None:
        empty = ratio(0, 0, empty="nothing measured")
        assert (empty.value, empty.reason, empty.denominator) == (None, "nothing measured", 0)
        assert ratio(1, 4, empty="x").value == 0.25

    def test_a_percentile_of_nothing_raises(self) -> None:
        with pytest.raises(ValueError, match="no values"):
            percentile([], 50)
        assert percentile([1000, 1200], 50) == 1000.0  # nearest rank, the benchmark's rounding
        assert percentile([1000, 1200], 95) == 1200.0


class TestOpeningTheRoles:
    def test_runs_by_path_and_the_unreadable_one_skipped(self, chain: ChainRuns) -> None:
        branch = chain.role("branch")
        assert [run.rel for run in branch.runs] == ["br-b0", "br-b1", "br-b2"]
        assert branch.skipped == {
            "broken": "not a readable trace: manifest.json does not read back (TraceSchemaError)"
        }, "named relative to the run, never the reader's absolute path"
        assert [run.pointer for run in chain.role("record").runs] == [
            "traces/record/rec-blocked",
            "traces/record/rec-broken",
            "traces/record/rec-shop",
        ]

    def test_a_role_that_never_ran_is_absent_not_empty(self, tmp_path: Path) -> None:
        role = open_role(tmp_path / "nowhere", "control")
        assert (role.present, role.runs, dict(role.skipped)) == (False, (), {})
        (tmp_path / "there").mkdir()
        assert open_role(tmp_path / "there", "control").present is True

    def test_an_older_format_is_skipped_with_its_reason(
        self, known: KnownChain, tmp_path: Path
    ) -> None:
        copy = tmp_path / "control"
        shutil.copytree(known.runs["ctl-shop"], copy / "old")
        manifest = copy / "old" / "manifest.json"
        document = json.loads(manifest.read_text())
        document["format_version"] = 2
        manifest.write_text(json.dumps(document))
        role = open_role(copy, "control")
        assert role.runs == ()
        assert role.skipped["old"] == (
            "not a readable trace: manifest.json does not read back (TraceSchemaError)"
        )

    def test_a_branch_run_finds_its_source_under_traces_record(self, chain: ChainRuns) -> None:
        (b0,) = [run for run in chain.role("branch").runs if run.rel == "br-b0"]
        source = chain.source_of(b0)
        assert source is not None and source.pointer == "traces/record/rec-shop"
        assert b0.site == "shop.test", "a branch run's site is its source task's"


class TestArchiveOverhead:
    def test_latency_against_the_control_group(self, chain: ChainRuns) -> None:
        found = archive_overhead(chain.role("record"), chain.role("control"))
        assert found.archived.model_dump() == {
            "role": "record",
            "runs": 3,
            "steps": 2,  # rec-shop's steps 1 and 2: step 0 holds the setup
            "sites": 1,  # only rec-shop measured a step after step 0
            "tier": "browser:pinned",
            "p50_ms": 1000.0,
            "p95_ms": 1200.0,
        }
        assert found.control is not None
        assert (found.control.p50_ms, found.control.p95_ms) == (600.0, 700.0)
        assert (found.p50_overhead_ms, found.p95_overhead_ms, found.reason) == (400.0, 500.0, None)

    def test_env_time_leaves_out_step_0_and_the_unmeasured(
        self, chain: ChainRuns, tmp_path: Path
    ) -> None:
        (shop,) = [run for run in chain.role("record").runs if run.rel == "rec-shop"]
        assert env_times(shop) == [1000, 1200]
        write_plain(
            tmp_path,
            "unmeasured",
            [Step(duration_ms=5000), Step(duration_ms=None), Step("terminal", duration_ms=100)],
            task_id="t",
            site="x.test",
        )
        (run,) = open_role(tmp_path, "record").runs
        assert env_times(run) == []

    def test_no_control_group_is_null_not_zero(self, chain: ChainRuns, tmp_path: Path) -> None:
        found = archive_overhead(chain.role("record"), open_role(tmp_path / "none", "control"))
        assert (found.p50_overhead_ms, found.p95_overhead_ms) == (None, None)
        assert found.reason is not None and "no control group" in found.reason

    def test_groups_at_different_tiers_are_not_compared(
        self, chain: ChainRuns, tmp_path: Path
    ) -> None:
        unpinned = {
            "environment": [
                {
                    "capabilities": [
                        {"capability_id": "browser", "replayability": {"replayability_id": "none"}}
                    ]
                }
            ]
        }
        write_plain(
            tmp_path,
            "ctl-unpinned",
            [Step(duration_ms=3000), Step(duration_ms=900), Step(duration_ms=1000)],
            task_id="shop-1",
            site="shop.test",
            resolved=unpinned,
        )
        control = open_role(tmp_path, "control")
        assert run_tier(control.runs[0].config) == "browser:none"
        found = archive_overhead(chain.role("record"), control)
        assert found.p50_overhead_ms is None
        assert found.reason == (
            "the groups are at different tiers (record browser:pinned, control browser:none)"
        )

    def test_bytes_before_and_after_dedup(self, chain: ChainRuns) -> None:
        found = archive_overhead(chain.role("record"), chain.role("control")).bytes
        total = found.total
        assert (found.runs, found.steps) == (3, 5), "rec-broken's step counts, its env/ is empty"
        assert (
            total.before == total.after + total.saved_by_revisit + total.saved_by_content_addressing
        )
        assert total.saved_by_content_addressing == len(BIG), "app2.js reused app.js's blob"
        (shop,) = [run for run in chain.role("record").runs if run.rel == "rec-shop"]
        styles = {
            line.url: (step, line)
            for step, line in shop.index_lines
            if isinstance(line, ExchangeLine) and line.url.endswith(("/style.css", "/style2.css"))
        }
        first_step, first = styles["http://shop.test/style.css"]
        repeated_step, repeated = styles["http://shop.test/style2.css"]
        assert first_step == repeated_step == 0, "both responses entered the same collection"
        assert first.revisit_of is None and repeated.revisit_of == first.response_record_id
        assert first.payload_sha256 == repeated.payload_sha256
        assert first.warc.response is not None and repeated.warc.response is not None
        assert total.saved_by_revisit == first.warc.response.length - repeated.warc.response.length
        assert total.saved_by_revisit > len(STYLE) // 3, "a large inline body was actually replaced"
        archive = load_archive(shop.trace)
        bodies = [archive.body(record) for record in archive.index.records if record.url in styles]
        assert bodies == [STYLE, STYLE], "the revisit restores the exact original body"
        assert found.per_step_before == total.before / 5
        assert found.per_step_after == total.after / 5

    def test_it_carries_the_settle_wait_of_each_role_that_ran(
        self, chain: ChainRuns, tmp_path: Path
    ) -> None:
        roles = [chain.role(role) for role in ("record", "control", "same_path", "branch")]
        missing = open_role(tmp_path / "none", "control")
        found = archive_overhead(
            chain.role("record"), chain.role("control"), roles=[*roles, missing]
        )
        assert [wait.role for wait in found.settle_wait] == [
            "record",
            "control",
            "same_path",
            "branch",
        ], "a role that did not run has no entry"
        # The known chain was written without the wait: no record, null with the reason.
        record = found.settle_wait[0]
        assert (record.observations, record.p50_ms, record.capped.value) == (0, None, None)
        assert record.capped.reason is not None and "no observation" in record.capped.reason

    def test_a_run_without_traffic_has_only_its_fingerprint(self, chain: ChainRuns) -> None:
        (broken,) = [run for run in chain.role("record").runs if run.rel == "rec-broken"]
        found = archive_bytes(broken)
        fingerprint = broken.trace.archive_run_file("browser")
        assert fingerprint is not None
        assert (found.before, found.after) == (fingerprint.size, fingerprint.size)
        assert (found.saved_by_revisit, found.saved_by_content_addressing) == (0, 0)


class TestBodyCompleteness:
    def test_every_record_has_an_explicit_state(self, chain: ChainRuns) -> None:
        found = body_completeness_metric(chain.role("record"))
        assert found.by_state == {"incomplete": 1, "ok": 5, "streamed_later": 2, "too_large": 1}
        assert (found.complete.numerator, found.complete.denominator) == (9, 9)
        assert (found.with_body.numerator, found.with_body.denominator) == (7, 9)

    def test_no_record_is_null(self, tmp_path: Path) -> None:
        found = body_completeness_metric(open_role(tmp_path / "none", "record"))
        assert (found.complete.value, found.with_body.value) == (None, None)
        assert found.records == 0


class TestSamePathFidelity:
    @pytest.mark.parametrize("checked_count", [1, 5, 6])
    def test_complete_checkpoint_coverage_is_separate_from_the_agent_outcome(
        self, tmp_path: Path, checked_count: int
    ) -> None:
        root = tmp_path / "coverage"
        write_plain(
            root / "traces" / "record",
            "source",
            [Step(color=10) for _ in range(6)],
            task_id="task",
            site="shop.test",
        )
        wal = WALWriter(root / "traces" / "same_path", "replay")
        wal.init_run(
            config(
                task_id="branch",
                site="shop.test",
                loop="branch",
                branch=provenance("source", 5, 0, "task"),
            )
        )
        for index in range(checked_count):
            spec = Step(
                origin="policy" if index == 5 else "restored",
                color=10,
                restoration=check(index),
                error_phase="llm" if index == 5 else "",
            )
            wal.commit_step(index, spec.record(index), fragments=spec.fragments(index))
        if checked_count < 6:
            wal.commit_step(
                checked_count,
                StepRecord(
                    step=checked_count,
                    error_phase="observe",
                    error="browser stopped responding",
                ),
            )
        wal.write_result(agent_result(agent_done="error", agent_reported_success=None))
        wal.finalize(TraceStatus.COMPLETE)

        result = same_path_fidelity(open_chain(ChainLayout(root))).overall
        assert result.errored_runs == 1
        assert result.restorer.value == 1
        assert result.runs_reaching_checkpoint.numerator == int(checked_count == 6)
        assert result.runs_passing.numerator == int(checked_count == 6)
        if checked_count < 6:
            assert result.first_failures[0].step == checked_count
            assert "no restoration check" in (result.first_failures[0].note or "")
        else:
            assert result.first_failures == [], (
                "a later model failure does not undo restored states"
            )

    @pytest.mark.parametrize(
        "evidence, numerator",
        [
            ({"url_match": True}, 5),
            ({"url_match": False}, 0),
            # The strict measure reads the same URL evidence as the restorer: a URL that
            # differs only in a parameter the replay's page-URL rules leave out is equal.
            (
                {
                    "url_match": True,
                    "recorded_url": "https://shop.test/all?vt=1",
                    "restored_url": "https://shop.test/all?vt=2",
                    "url_ignored_query": ["vt"],
                    "url_rule_ids": ["shop-vt"],
                },
                5,
            ),
        ],
    )
    def test_url_and_image_equality_requires_a_measured_match(
        self, chain: ChainRuns, evidence: dict[str, object], numerator: int
    ) -> None:
        measured = open_chain(chain.layout)
        for run in measured.role("same_path").runs:
            run.steps = tuple(
                record.model_copy(
                    update={
                        "restoration": record.restoration.model_copy(
                            update={
                                "evidence": [
                                    e.model_copy(update=evidence)
                                    for e in record.restoration.evidence
                                ]
                            }
                        )
                    }
                )
                if record.restoration
                else record
                for record in run.steps
            )
        result = same_path_fidelity(measured).overall
        assert (result.strict.numerator, result.strict.denominator) == (numerator, 7)
        assert result.url_coverage.value == 1
        assert result.exact_screenshot.numerator == 5

    def test_unknown_urls_do_not_pass_the_strict_measure(self, chain: ChainRuns) -> None:
        measured = open_chain(chain.layout)
        for run in measured.role("same_path").runs:
            run.steps = tuple(
                record.model_copy(
                    update={
                        "restoration": record.restoration.model_copy(
                            update={
                                "evidence": [
                                    e.model_copy(
                                        update={"url_match": None, "signature_match": None}
                                    )
                                    for e in record.restoration.evidence
                                ]
                            }
                        )
                    }
                )
                if record.restoration is not None
                else record
                for record in run.steps
            )
        result = same_path_fidelity(measured).overall
        assert result.strict.value is None
        assert result.strict.denominator == 0
        assert (result.exact_screenshot.numerator, result.exact_screenshot.denominator) == (5, 7)
        assert result.url_coverage.value == 0
        assert result.signature_coverage.value == 0
        assert result.restorer.numerator == 6

    def test_overall_and_by_site(self, chain: ChainRuns) -> None:
        found = same_path_fidelity(chain)
        overall = found.overall
        assert (overall.runs, overall.verified_steps) == (3, 7)
        assert (overall.restorer.numerator, overall.restorer.denominator) == (6, 7)
        assert (overall.exact_screenshot.numerator, overall.exact_screenshot.denominator) == (
            5,
            7,
        ), (
            "sp-shop-b1's step 1 passed the restorer (within the dHash bound) yet its "
            "screenshot is not the source's byte for byte"
        )
        assert (overall.runs_passing.numerator, overall.runs_passing.denominator) == (2, 3)
        shop, blocked = found.by_site["shop.test"], found.by_site["blocked.test"]
        assert (
            shop.restorer.value,
            shop.exact_screenshot.numerator,
            shop.exact_screenshot.denominator,
        ) == (1.0, 5, 6)
        assert overall.strict.value is None, "this fixture records no URL comparisons"
        assert blocked.restorer.value == 0.0
        assert [f.model_dump() for f in overall.first_failures] == [
            {
                "run": "traces/same_path/sp-blocked-b0",
                "step": 0,
                "reasons": ["document_miss", "screenshot_distance"],
                "note": None,
            }
        ]
        assert found.unmatched == []

    def test_media_coverage_mismatches_and_alignment_by_site(self, chain: ChainRuns) -> None:
        """Media is counted only where a comparison was actually made; alignment by outcome."""
        from spectragent.schemas.restoration import (
            MediaAlignment,
            MediaComparison,
            MediaEvidence,
        )

        assert same_path_fidelity(chain).overall.media_coverage.value == 0
        measured = open_chain(chain.layout)
        compared = MediaEvidence(
            comparisons=[MediaComparison(element="main#0 video", correspondence="missing")],
            alignment=[MediaAlignment(element="main#0 video", outcome="not_restorable")],
        )
        unknown = MediaEvidence(
            comparisons=None,
            detail="the recorded screenshot carries no media state",
            alignment=[],
        )
        for run in measured.role("same_path").runs:
            media = compared if run.site == "shop.test" else unknown
            run.steps = tuple(
                record.model_copy(
                    update={
                        "restoration": record.restoration.model_copy(
                            update={
                                "evidence": [
                                    e.model_copy(update={"media": media})
                                    for e in record.restoration.evidence
                                ],
                                "reasons": [
                                    *(["media_mismatch"] if run.site == "shop.test" else []),
                                ],
                            }
                        )
                    }
                )
                if record.restoration is not None
                else record
                for record in run.steps
            )
        found = same_path_fidelity(measured)
        shop, blocked = found.by_site["shop.test"], found.by_site["blocked.test"]
        assert (shop.media_coverage.numerator, shop.media_coverage.denominator) == (6, 6)
        assert shop.media_mismatches == 6
        assert shop.media_alignment == {"not_restorable": 6}
        assert blocked.media_coverage.value == 0, "a comparison not made is not covered"
        assert blocked.media_mismatches == 0 and blocked.media_alignment == {}
        assert found.overall.media_coverage.numerator == 6

    def test_steps_under_media_area_rules_are_counted_by_site(self, chain: ChainRuns) -> None:
        """A verdict that rests on a media-area declaration is visible, not just a higher rate."""
        from spectragent.schemas.restoration import MediaAreaEvidence

        assert same_path_fidelity(chain).overall.media_area_steps == 0
        measured = open_chain(chain.layout)
        declared = MediaAreaEvidence(
            rule_ids=["shop-media"], masked_share=0.25, unmasked_distance=9, masked_distance=0
        )
        for run in measured.role("same_path").runs:
            if run.site != "shop.test":
                continue
            run.steps = tuple(
                record.model_copy(
                    update={
                        "restoration": record.restoration.model_copy(
                            update={
                                "evidence": [
                                    e.model_copy(update={"media_areas": declared})
                                    for e in record.restoration.evidence
                                ]
                            }
                        )
                    }
                )
                if record.restoration is not None
                else record
                for record in run.steps
            )
        found = same_path_fidelity(measured)
        assert found.by_site["shop.test"].media_area_steps == 6
        assert found.by_site["blocked.test"].media_area_steps == 0
        assert found.overall.media_area_steps == 6
        assert found.overall.restorer == same_path_fidelity(chain).overall.restorer, (
            "counting the declaration does not change the verdicts"
        )

    def test_errored_runs_count_either_ledger(self, chain: ChainRuns, tmp_path: Path) -> None:
        """Not ending cleanly: an incomplete or unclosed trace, or an agent in error / timeout / veto."""
        assert same_path_fidelity(chain).overall.errored_runs == 0
        record = {run.rel: run for run in chain.role("record").runs}
        assert ended_unclean(record["rec-broken"]), "its agent ended in error"
        assert not ended_unclean(record["rec-shop"])
        copy = tmp_path / "record"
        shutil.copytree(record["rec-shop"].trace.trace_dir, copy / "rec-shop")
        manifest = copy / "rec-shop" / "manifest.json"
        document = json.loads(manifest.read_text())
        document.update(status="incomplete", incomplete_reason="the close-out failed")
        manifest.write_text(json.dumps(document))
        (incomplete,) = open_role(copy, "record").runs
        assert ended_unclean(incomplete), "its trace is incomplete, whatever its agent did"

    def test_a_source_not_under_traces_record_is_named(
        self, known: KnownChain, tmp_path: Path
    ) -> None:
        root = tmp_path / "chain"
        shutil.copytree(known.runs["sp-shop-b0"], root / "traces" / "same_path" / "sp-shop-b0")
        found = same_path_fidelity(open_chain(ChainLayout(root)))
        assert found.unmatched == ["traces/same_path/sp-shop-b0"]
        assert found.overall.strict.value is None, "no source: nothing to compare the hashes with"
        assert found.overall.restorer.value == 1.0, "the restorer's own verdicts still count"

    def test_no_same_path_run_is_null(self, tmp_path: Path) -> None:
        found = same_path_fidelity(open_chain(empty_layout(tmp_path)))
        assert found.overall.restorer.value is None
        assert found.overall.restorer.reason == "no verified step"
        assert found.by_site == {}

    def test_a_missing_screenshot_leaves_the_run_out_of_the_strict_measure_only(
        self, known: KnownChain, tmp_path: Path
    ) -> None:
        root = damaged_copy(known, tmp_path)
        screenshot_file(root / "traces" / "same_path" / "sp-shop-b1", 1).unlink()
        found = same_path_fidelity(open_chain(ChainLayout(root)))
        reason = (
            "its step 1 screenshot does not read back (TraceSchemaError): left out of the "
            "image equality measures"
        )
        assert found.overall.skipped == {"sp-shop-b1": reason}
        assert found.by_site["shop.test"].skipped == {"sp-shop-b1": reason}
        assert found.by_site["blocked.test"].skipped == {}
        exact = found.overall.exact_screenshot
        assert (exact.numerator, exact.denominator) == (3, 4), "sp-shop-b0's 3 + blocked's 1"
        restorer = found.overall.restorer
        assert (restorer.numerator, restorer.denominator) == (6, 7), "step records still count"
        assert (found.overall.runs, found.overall.runs_passing.numerator) == (3, 2)

    def test_an_empty_source_screenshot_names_the_source(
        self, known: KnownChain, tmp_path: Path
    ) -> None:
        root = damaged_copy(known, tmp_path)
        screenshot_file(root / "traces" / "record" / "rec-shop", 2).write_bytes(b"")
        found = same_path_fidelity(open_chain(ChainLayout(root)))
        reason = (
            "its source traces/record/rec-shop: step 2 screenshot does not read back "
            "(TraceSchemaError): left out of the image equality measures"
        )
        assert found.overall.skipped == {"sp-shop-b0": reason, "sp-shop-b1": reason}
        exact = found.overall.exact_screenshot
        assert (exact.numerator, exact.denominator) == (0, 1), "only sp-blocked-b0 is compared"


class TestRestoration:
    def test_checks_by_reason_and_retargets(self, chain: ChainRuns) -> None:
        found = restoration(chain.role("branch"))
        assert (found.ok, found.total, found.rate) == (4, 6, 4 / 6)
        assert found.by_reason == {
            "document_miss": 1,
            "retarget_not_found": 1,
            "screenshot_distance": 1,
        }
        assert found.retarget_failures == {"not_found": {"geometry_signature": 1}}
        assert found.summary().startswith("4/6 checks passed (67%)")

    def test_an_uncommitted_step_is_invisible(self, known: KnownChain, tmp_path: Path) -> None:
        """A step file the manifest never listed (a crash before the rename) checks nothing."""
        run = tmp_path / "br-b0"
        shutil.copytree(known.runs["br-b0"], run)
        orphan = json.loads((run / "steps" / "001.json").read_text())
        orphan["step"] = 7
        orphan["restoration"]["step"] = 7
        orphan["restoration"]["verdict"] = "diverged"
        orphan["restoration"]["reason"] = "url_mismatch"
        orphan["restoration"]["reasons"] = ["url_mismatch"]
        (run / "steps" / "007.json").write_text(json.dumps(orphan))
        found = restoration(open_role(tmp_path, "branch"))
        assert (found.ok, found.total, found.by_reason) == (2, 2, {})

    def test_no_check_is_null_not_zero(self) -> None:
        found = restoration_rate([])
        assert (found.total, found.rate) == (0, None)
        assert found.model_dump()["rate"] is None, "the rate is in the report, as null"


class TestBranchCoverage:
    def test_from_the_checkpoint_on(self, chain: ChainRuns) -> None:
        found = branch_coverage(chain.role("branch"))
        assert found.branches == 3
        assert (found.zero_document_miss.numerator, found.zero_document_miss.denominator) == (2, 3)
        assert found.document_misses == 1, (
            "br-b1's POST; br-b2's miss is before its checkpoint — restoration's, not coverage's"
        )

    def test_other_misses_by_action_type_resource_host_and_reason(self, chain: ChainRuns) -> None:
        found = branch_coverage(chain.role("branch")).other_misses
        assert found.model_dump() == {
            "total": 5,
            "by_action": {"browser.click_coordinate": 1, "terminal": 1, "trailing": 3},
            "by_resource_type": {"Image": 3, "Script": 1, "XHR": 1},
            "by_host": {"ads.test": 3, "shop.test": 2},
            "by_reason": {"no_match": 5},
            "by_class": {"subresource": 5},
        }, "br-b1's close-out folded three image misses into one summary: they count 3"

    def test_no_branch_is_null(self, tmp_path: Path) -> None:
        found = branch_coverage(open_role(tmp_path / "none", "branch"))
        assert found.zero_document_miss.value is None
        assert found.zero_document_miss.reason == "no branch run"


class TestReplayDeterminism:
    def test_same_input_pairs_and_checkpoint_states(self, chain: ChainRuns) -> None:
        found = replay_determinism(chain).by_role
        for role in ("same_path", "branch"):
            item = found[role]
            assert (item.runs, item.groups, item.unpaired, item.without_terminal) == (3, 1, 1, 0)
            assert (item.identical.numerator, item.identical.denominator) == (1, 1)
            assert (item.checkpoint_identical.numerator, item.checkpoint_identical.denominator) == (
                1,
                1,
            )

    def test_a_different_terminal_screenshot_is_counted(
        self, known: KnownChain, tmp_path: Path
    ) -> None:
        root = tmp_path / "chain"
        branch = root / "traces" / "branch"
        for name in ("br-b0", "br-b2"):
            shutil.copytree(known.runs[name], branch / name)
        # b2 ends somewhere else: rewrite its terminal screenshot artifact in place.
        manifest = json.loads((branch / "br-b2" / "manifest.json").read_text())
        terminal = manifest["steps"][-1]["fragments"]["browser.screenshot"]["artifact"]
        other = (known.runs["br-b1"] / terminal).read_bytes()
        (branch / "br-b2" / terminal).write_bytes(other)
        found = replay_determinism(open_chain(ChainLayout(root))).by_role["branch"]
        assert (found.identical.numerator, found.identical.denominator) == (0, 1)

    def test_nothing_to_compare_is_null(self, tmp_path: Path) -> None:
        found = replay_determinism(open_chain(empty_layout(tmp_path))).by_role
        assert found["branch"].identical.value is None
        assert found["same_path"].checkpoint_identical.value is None

    def test_an_unreadable_screenshot_leaves_the_run_out(
        self, known: KnownChain, tmp_path: Path
    ) -> None:
        root = damaged_copy(known, tmp_path)
        screenshot_file(root / "traces" / "branch" / "br-b2", 2).unlink()  # its terminal frame
        found = replay_determinism(open_chain(ChainLayout(root))).by_role
        branch = found["branch"]
        assert branch.skipped == {
            "br-b2": "its step 2 screenshot does not read back (TraceSchemaError)"
        }
        assert (branch.runs, branch.groups, branch.unpaired) == (2, 0, 2), "b0 lost its pair"
        assert branch.identical.value is None
        checkpoint = branch.checkpoint_identical
        assert (checkpoint.numerator, checkpoint.denominator) == (1, 1), "b0 and b1 at step 1"
        assert found["same_path"].skipped == {}


class TestDamagedEvidence:
    """One damaged run never takes the report down (arch §4.16): its measures skip it, named."""

    def test_the_report_still_builds_with_the_reasons_in_its_json(
        self, known: KnownChain, tmp_path: Path
    ) -> None:
        root = damaged_copy(known, tmp_path)
        screenshot_file(root / "traces" / "same_path" / "sp-blocked-b0", 0).write_bytes(b"")
        screenshot_file(root / "traces" / "branch" / "br-b0", 1).unlink()
        document = json.loads(report_json(build_report(ChainLayout(root), evidence="live")))
        fidelity = document["metrics"]["same_path_fidelity"]["overall"]
        assert fidelity["skipped"] == {
            "sp-blocked-b0": "its step 0 screenshot does not read back (TraceSchemaError): left "
            "out of the image equality measures"
        }
        determinism = document["metrics"]["replay_determinism"]["by_role"]
        assert determinism["same_path"]["skipped"] == {
            "sp-blocked-b0": "its step 0 screenshot does not read back (TraceSchemaError)"
        }
        assert determinism["branch"]["skipped"] == {
            "br-b0": "its step 1 screenshot does not read back (TraceSchemaError)"
        }
        restoration = document["metrics"]["restoration"]
        assert (restoration["ok"], restoration["total"]) == (4, 6), "measures that do not read it"


class TestExportDeterminism:
    def test_identical_exports(self, layout: ChainLayout) -> None:
        found = export_determinism(layout)
        assert found.identical is True and found.first == found.second
        assert found.first is not None and found.first.files == 3

    def test_a_differing_byte_is_seen(self, layout: ChainLayout, tmp_path: Path) -> None:
        root = tmp_path / "chain"
        shutil.copytree(layout.datasets_dir, root / "datasets")
        target = root / "datasets" / "second" / "train.jsonl"
        target.write_bytes(target.read_bytes() + b" ")
        assert export_determinism(ChainLayout(root)).identical is False

    def test_one_export_is_null(self, layout: ChainLayout, tmp_path: Path) -> None:
        root = tmp_path / "chain"
        shutil.copytree(layout.dataset("first"), root / "datasets" / "first")
        found = export_determinism(ChainLayout(root))
        assert (found.identical, found.second) == (None, None)
        assert found.reason == "no second export (repeat: 1 does not measure it)"

    def test_the_digest_covers_paths_and_bytes(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "x.txt").write_text("1")
        first = tree_digest(tmp_path / "a")
        (tmp_path / "a" / "x.txt").rename(tmp_path / "a" / "y.txt")
        assert tree_digest(tmp_path / "a") != first


#: A planted credential, and a URL whose page carries a site's own public key — both kept on
#: disk under ``env/`` and in the step record, where the privacy measure does not search.
PLANTED_VALUE = "pw-Qx41"
SITE_KEY_URL = "https://maps.test/js?key=AIzaSyPublicBrowserKey0123"
#: A real image sent inline, in standard base64: the trace stores its decoded bytes.
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


def _call(step: int, call: int, messages: list[Any]) -> EncodedLLMCall:
    pending = PendingLLMCall(messages)
    pending.complete(ChatInvokeCompletion(completion="click(1, 1)", usage=usage()))
    return encode_llm_call(step, call, pending)


def _private_evidence(run_dir_step: int) -> list[TraceFile]:
    """What stays on this machine: the planted value and a key-carrying URL, in env/."""
    line = json.dumps({"url": SITE_KEY_URL, "post": f"password={PLANTED_VALUE}"}).encode()
    return [archive_file("browser.network", "index.jsonl", line, step=run_dir_step)]


def privacy_chain(root: Path) -> ChainLayout:
    """Two readable runs that sent the model something, two that do not read back, a dataset.

    - ``record/run-a`` (a.test): step 0 sent the planted value twice in one text part, once more
      inside an inline image's data URL (not text: left out; its base64 is not the standard
      encoding, so the trace stores the URL's own text), a real PNG inline (stored decoded), and
      an image by a URL carrying a token; its env/ evidence and its step record (the typed
      action) hold the planted value and a site's key-carrying URL too.
    - ``record/run-b`` (b.test): a trailing call (a step the loop never surrendered) whose
      history holds a bearer token.
    - ``same_path/damaged``: its one recorded call no longer parses.
    - ``branch/broken``: a trace whose manifest does not read back.
    - ``datasets/first/train.jsonl``: the planted value once, an openai-style key once.
    """
    layout = ChainLayout(root)
    wal = WALWriter(layout.traces("record"), "run-a")
    wal.init_run(config(task_id="a-1", site="a.test", archived=False))
    typed = StepRecord(
        step=0,
        llm_output="Thought: sign in",
        actions=[Action(name="browser.type", params={"index": 3, "text": PLANTED_VALUE})],
        action_results=[ActionResult(success=True)],
    )
    request = [
        SystemMessage(content=f"Never open {SITE_KEY_URL.split('?')[0]}"),
        UserMessage(
            content=[
                ContentPartTextParam(text=f"type {PLANTED_VALUE} then {PLANTED_VALUE} again"),
                ContentPartImageParam(
                    image_url=ImageURL(url=f"data:image/png;base64,{PLANTED_VALUE}AAAA")
                ),
                ContentPartImageParam(image_url=ImageURL(url=PNG_DATA_URL)),
                ContentPartImageParam(
                    image_url=ImageURL(url="https://img.test/shot.png?token=abcdefgh12345678")
                ),
            ]
        ),
    ]
    wal.commit_step(0, typed, llm_calls=[_call(0, 0, request)], extra_files=_private_evidence(0))
    wal.write_result(agent_result())
    wal.finalize(TraceStatus.COMPLETE)

    wal = WALWriter(layout.traces("record"), "run-b")
    wal.init_run(config(task_id="b-1", site="b.test", archived=False))
    history = [
        UserMessage(content="what next?"),
        AssistantMessage(content="Authorization: Bearer abcdefghijklmnopqrstuvwxyz"),
    ]
    wal.commit_trailing(llm_calls=[_call(0, 0, history)])
    wal.finalize(TraceStatus.INCOMPLETE, incomplete_reason="cancelled")

    wal = WALWriter(layout.traces("same_path"), "damaged")
    wal.init_run(config(task_id="a-1", site="a.test", archived=False))
    wal.commit_step(0, step_record(0), llm_calls=[_call(0, 0, prompt("go"))])
    wal.finalize(TraceStatus.COMPLETE)
    (wal.run_dir / RunTrace(wal.run_dir).manifest.steps[0].llm_calls[0]).write_text("{not json")

    broken = layout.traces("branch") / "broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{not json")

    dataset = layout.dataset("first")
    dataset.mkdir(parents=True)
    (dataset / "train.jsonl").write_text(
        json.dumps({"text": f"typed {PLANTED_VALUE}", "key": "sk-abcdefghijklmnopqrstu"}) + "\n"
    )
    return layout


class TestPrivacy:
    """The two gates: what was sent to the model, and the datasets (arch §4.16, §5.6)."""

    @pytest.fixture
    def found(self, tmp_path: Path) -> Privacy:
        planted = [
            Planted(label="site password", value=PLANTED_VALUE, source="value"),
            Planted(label="token", value="never-written-anywhere", source="value"),
            Planted(label="teacher api key", value=None, source="env:FLYWHEEL_TEST_UNSET"),
        ]
        return privacy(open_chain(privacy_chain(tmp_path / "chain")), planted)

    def test_what_was_sent_to_the_model(self, found: Privacy) -> None:
        gate = found.model_requests
        assert gate.items == 2, "run-a's call and run-b's trailing call"
        assert [(p.label, p.occurrences, p.items) for p in gate.planted] == [
            ("site password", 2, 1),  # the text part; not the inline image's data
            ("token", 0, 0),
            ("teacher api key", None, None),
        ]
        assert gate.planted[2].reason == "env:FLYWHEEL_TEST_UNSET is not set: nothing to search for"
        assert gate.shapes == {"bearer token": 1, "url carrying a token": 1}
        assert gate.shape_items == {"bearer token": 1, "url carrying a token": 1}

    def test_the_datasets(self, found: Privacy) -> None:
        gate = found.datasets
        assert gate.items == 1
        assert [(p.label, p.occurrences, p.items) for p in gate.planted] == [
            ("site password", 1, 1),
            ("token", 0, 0),
            ("teacher api key", None, None),
        ]
        assert gate.shapes == {"openai-style api key": 1}

    def test_env_and_the_rest_of_a_trace_are_not_searched(
        self, found: Privacy, tmp_path: Path
    ) -> None:
        """The planted value and a site's own key are on disk, in env/ and the step record."""
        run_a = tmp_path / "chain" / "traces" / "record" / "run-a"
        holding = {
            path.relative_to(run_a).parts[0]
            for path, text in iter_readable(run_a)
            if PLANTED_VALUE in text or "key=AIza" in text
        }
        assert {"env", "steps", "messages"} <= holding
        # ...and only the model requests' and the dataset's occurrences were counted.
        counted = [found.model_requests.planted[0], found.datasets.planted[0]]
        assert [p.occurrences for p in counted] == [2, 1]
        assert "url carrying a token" not in found.datasets.shapes

    def test_runs_that_do_not_read_back_are_named(self, found: Privacy) -> None:
        assert (found.runs, found.sites) == (2, 2)
        assert found.skipped == {
            "traces/branch/broken": (
                "not a readable trace: manifest.json does not read back (TraceSchemaError)"
            ),
            "traces/same_path/damaged": (
                "a recorded model call does not read back (TraceSchemaError)"
            ),
        }

    def test_values_are_never_written(self, found: Privacy) -> None:
        dumped = found.model_dump_json()
        for value in (PLANTED_VALUE, "sk-abcdef", "abcdefghijklmnop", "AIza"):
            assert value not in dumped

    def test_inline_image_data_is_not_text_but_an_image_url_is(self) -> None:
        call = read_back(
            _call(
                0,
                0,
                [
                    UserMessage(
                        content=[
                            ContentPartTextParam(text="look"),
                            ContentPartImageParam(
                                image_url=ImageURL(url="data:image/png;base64,QUJD")
                            ),
                            ContentPartImageParam(image_url=ImageURL(url="https://img.test/a.png")),
                        ]
                    )
                ],
            )
        )
        texts = request_texts(call)
        assert "look" in texts and "https://img.test/a.png" in texts
        assert not [text for text in texts if text.startswith("data:")]

    def test_it_reads_the_prompts_back_whole_from_their_stored_images(
        self, found: Privacy, tmp_path: Path
    ) -> None:
        """The request's two inline images are stored apart; the measure reads them back in place.

        The URL with the planted value is stored as its own text (its base64 is not the
        standard encoding), the PNG decoded. Both come back where they stood, and are skipped
        as image data: only the text part's two occurrences count.
        """
        run_a = tmp_path / "chain" / "traces" / "record" / "run-a"
        trace = RunTrace(run_a)
        stored = sorted(Path(rel).suffix for rel in trace.manifest.prompt_images)
        assert stored == [".png", ".txt"]
        (call,) = trace.llm_messages(0)
        user = as_chat(call).messages[1]
        assert isinstance(user, UserMessage) and isinstance(user.content, list)
        urls = [part.image_url.url for part in user.content if part.type == "image_url"]
        assert urls == [
            f"data:image/png;base64,{PLANTED_VALUE}AAAA",
            PNG_DATA_URL,
            "https://img.test/shot.png?token=abcdefgh12345678",
        ]
        assert found.model_requests.planted[0].occurrences == 2

    def test_stored_prompt_images_are_their_own_storage_category(self, tmp_path: Path) -> None:
        found = storage(open_chain(privacy_chain(tmp_path / "chain")))
        images = found.by_role["record"].categories["messages:images"]
        text = f"data:image/png;base64,{PLANTED_VALUE}AAAA".encode()
        assert (images.files, images.bytes) == (2, len(PNG_BYTES) + len(text))
        assert images.compressible_bytes == len(text), "the PNG is reported as size only"

    def test_the_known_chain_leaks_nothing(self, layout: ChainLayout) -> None:
        found = privacy(
            open_chain(layout), [Planted(label="x", value="planted-xyz-123", source="value")]
        )
        assert found.datasets.items > 0
        for gate in (found.model_requests, found.datasets):
            assert (gate.planted[0].occurrences, gate.shapes) == (0, {})

    def test_the_recommendation_names_the_gate(self, tmp_path: Path) -> None:
        layout = privacy_chain(tmp_path / "chain")
        report = build_report(
            layout,
            evidence="live",
            planted=[Planted(label="site password", value=PLANTED_VALUE, source="value")],
        )
        verdict = report.recommendations.metrics.privacy.verdict or ""
        assert verdict.startswith("leak — sent to the model: site password, 2 credential-shaped")
        assert "in the datasets: site password, 1 credential-shaped string(s)" in verdict
        assert verdict.endswith("; 2 run(s) not read (see skipped)")


class TestTokenUse:
    """Every role's recorded tokens, read from the call records (arch §4.16 "token 上限")."""

    def test_the_tokens_every_role_recorded(self, tmp_path: Path) -> None:
        chain = open_chain(privacy_chain(tmp_path / "chain"))
        uses = {role: role_token_use(chain.role(role)) for role in ROLES}
        # run-a's call and run-b's trailing call each reported 15 tokens.
        record = uses["record"]
        assert (record.runs, record.tokens, record.calls, record.calls_without_usage) == (
            2,
            30,
            2,
            [],
        )
        # A damaged prompt does not erase its separately committed billing facts.
        assert (uses["same_path"].runs, uses["same_path"].tokens) == (1, 15)
        assert uses["same_path"].skipped == {}
        assert uses["branch"].skipped == {
            "traces/branch/broken": (
                "not a readable trace: manifest.json does not read back (TraceSchemaError)"
            )
        }
        assert uses["control"] == TokenUse(
            runs=0, tokens=0, calls=0, calls_without_usage=[], skipped={}
        )

    def test_the_report_counts_them_against_the_spec_s_limit(self, tmp_path: Path) -> None:
        layout = privacy_chain(tmp_path / "chain")
        under = build_report(layout, evidence="live", token_limit=46).token_use
        assert (under.limit, under.tokens, under.limit_reached, under.reason) == (
            46,
            45,
            False,
            None,
        )
        assert "stopped at its token limit" not in render_markdown(
            build_report(layout, evidence="live", token_limit=46)
        )

        reached = build_report(layout, evidence="live", token_limit=45)
        assert reached.token_use.limit_reached is True
        markdown = render_markdown(reached)
        assert (
            "**The chain stopped at its token limit.** Its runs recorded 45 tokens, at or over "
            "the limit of 45"
        ) in markdown
        assert "- limit: 45 tokens (reached); used: 45 tokens" in markdown
        assert "- record: 30 tokens over 2 call(s) in 2 run(s); 0 call(s) reported no usage" in (
            markdown
        )
        assert "- same_path: 15 tokens over 1 call(s) in 1 run(s)" in markdown

        unlimited = build_report(layout, evidence="live", token_limit="unlimited")
        assert (unlimited.token_use.limit, unlimited.token_use.limit_reached) == (
            "unlimited",
            False,
        )
        assert "- limit: unlimited; used: 45 tokens" in render_markdown(unlimited)

        unknown = build_report(layout, evidence="live").token_use
        assert (unknown.limit, unknown.limit_reached, unknown.reason) == (
            None,
            None,
            NO_SPEC_LIMIT,
        )


class TestSiteCounts:
    """Every measure a recommendation rests on counts the distinct sites its data came from."""

    def test_on_the_known_chain(self, layout: ChainLayout) -> None:
        report = build_report(layout, evidence="live")
        metrics, sections, inputs = report.metrics, report.sections, report.decision_inputs
        found = {
            "archived": metrics.archive_overhead.archived.sites,  # rec-shop measured steps 1, 2
            "control": metrics.archive_overhead.control.sites
            if metrics.archive_overhead.control
            else None,
            "body": metrics.body_completeness.sites,  # rec-broken archived no record
            "fidelity": metrics.same_path_fidelity.sites,
            "restoration": metrics.restoration.sites,
            "coverage": metrics.branch_coverage.sites,
            "determinism": metrics.replay_determinism.sites,  # the two shop pairs
            "anti_bot": sections.anti_bot.sites,
            "duplicates": sections.duplicate_branches.sites,
            "post": sections.post_branch_misses.sites,
            "storage": sections.storage.sites,
            "document_misses": inputs.branch_document_misses.sites,
            "site_rules": inputs.site_rule_candidates.sites,
        }
        assert found == {
            "archived": 1,
            "control": 1,
            "body": 2,
            "fidelity": 2,
            "restoration": 1,
            "coverage": 1,
            "determinism": 1,
            "anti_bot": 3,
            "duplicates": 1,
            "post": 1,
            "storage": 3,
            "document_misses": 1,
            "site_rules": 2,
        }
        recommendation = report.recommendations
        assert recommendation.metrics.privacy.sites == 3, "every run whose requests were read"
        assert recommendation.sections.export_yield.sites == 1, "the export's sources: shop only"
        assert recommendation.sections.sites.sites == 3


class TestEmptyBranches:
    """A branch that executed no action from its checkpoint on is left out and counted apart."""

    @staticmethod
    async def _branches(root: Path) -> None:
        directory = root / "traces" / "branch"
        page_navigation = miss("http://shop.test/timer", document=True)
        cases: dict[str, list[Step]] = {
            # The teacher clicked; the page it opened was never recorded.
            "acted": [
                Step("restored", [click(1, 1)], restoration=check(0)),
                Step(
                    "policy",
                    [click(5, 5)],
                    restoration=check(1),
                    replay=[miss("http://shop.test/new", document=True)],
                ),
                Step("terminal"),
            ],
            # The teacher's call failed; the page navigated by itself meanwhile.
            "model": [
                Step("restored", [click(1, 1)], restoration=check(0)),
                Step("policy", error_phase="llm", restoration=check(1), replay=[page_navigation]),
                Step("terminal"),
            ],
            # The reply parsed to nothing, or did not parse: no action, no error phase.
            "silent": [
                Step("restored", [click(1, 1)], restoration=check(0)),
                Step("policy", restoration=check(1)),
                Step("terminal"),
            ],
            # The restoration ended the run before the teacher was asked.
            "unrestored": [Step("restored", error_phase="restore", restoration=check(0))],
        }
        for index, (run_id, steps) in enumerate(cases.items()):
            await write_replayed(
                directory,
                run_id,
                steps,
                task_id="shop-1",
                site="shop.test",
                branch=provenance("rec-shop", 1, index, "shop-1"),
            )

    async def test_reasons_coverage_duplicates_and_misses(self, tmp_path: Path) -> None:
        await self._branches(tmp_path)
        chain = open_chain(ChainLayout(tmp_path))
        runs = chain.role("branch").by_rel()
        assert {rel: empty_branch(run) for rel, run in runs.items()} == {
            "acted": None,
            "model": "model_call_failed",
            "silent": "no_action",
            "unrestored": "ended_before_decision",
        }
        coverage = branch_coverage(chain.role("branch"))
        assert coverage.branches == 4 and coverage.sites == 1
        assert coverage.empty.model_dump() == {
            "total": 3,
            "by_reason": {"ended_before_decision": 1, "model_call_failed": 1, "no_action": 1},
        }
        share = coverage.zero_document_miss
        assert (share.numerator, share.denominator) == (0, 1), (
            "counted as branches without a document miss, the three empty ones made it 3 of 4"
        )
        assert coverage.document_misses == 1, "the page's own navigation is no teacher outcome"
        duplicates = duplicate_branches(chain.role("branch"), None)
        assert (duplicates.not_compared, duplicates.duplicates, duplicates.rate.denominator) == (
            3,
            0,
            1,
        ), "three empty checkpoint steps are not two duplicates"
        misses = branch_document_misses(chain.role("branch"))
        assert (misses.branches, misses.document_misses) == (1, 1)
        per_site = sites(chain, fidelity=same_path_fidelity(chain), blocked=[], exclude_below=0.8)
        assert per_site.by_site["shop.test"].branch_zero_document_miss.denominator == 1

    async def test_a_continue_step_that_acts_makes_the_branch_non_empty(
        self, tmp_path: Path
    ) -> None:
        await write_replayed(
            tmp_path,
            "late",
            [
                Step("restored", [click(1, 1)], restoration=check(0)),
                Step("policy", restoration=check(1)),  # no action at the checkpoint
                Step("policy", [click(9, 9)]),  # a continue step acts
            ],
            task_id="shop-1",
            site="shop.test",
            branch=provenance("rec-shop", 1, 0, "shop-1"),
        )
        (run,) = open_role(tmp_path, "branch").runs
        assert empty_branch(run) is None


class TestUnreadableResults:
    """A recording whose ``result.json`` does not read back is named, never a crash."""

    @pytest.mark.parametrize("damage", ["garbage", "directory"])
    def test_sites_and_the_unclean_ending(
        self, known: KnownChain, tmp_path: Path, damage: str
    ) -> None:
        record = tmp_path / "traces" / "record"
        shutil.copytree(known.runs["rec-shop"], record / "rec-shop")
        result = record / "rec-shop" / "result.json"
        result.unlink()
        if damage == "garbage":
            result.write_text("{not json")
        else:
            result.mkdir()  # reading it raises IsADirectoryError, an OSError
        chain = open_chain(ChainLayout(tmp_path))
        (run,) = chain.role("record").runs
        assert agent_outcome(run) == UNREADABLE_RESULT
        assert ended_unclean(run)
        found = sites(chain, fidelity=same_path_fidelity(chain), blocked=[], exclude_below=0.8)
        assert found.by_site["shop.test"].record_outcome == {UNREADABLE_RESULT: 1}

    def test_no_result_is_said_apart(self, known: KnownChain, tmp_path: Path) -> None:
        record = tmp_path / "record"
        shutil.copytree(known.runs["rec-shop"], record / "rec-shop")
        (record / "rec-shop" / "result.json").unlink()
        (run,) = open_role(record, "record").runs
        assert agent_outcome(run) == NO_RESULT


class TestDatasetSamples:
    """The samples are read through the export's own model: never a silent zero."""

    def test_a_sample_that_does_not_validate_is_refused_by_name(
        self, known: KnownChain, tmp_path: Path
    ) -> None:
        root = tmp_path / "chain"
        shutil.copytree(known.root / "datasets", root / "datasets")
        samples = root / "datasets" / "first" / "train.samples.jsonl"
        lines = samples.read_text().splitlines()
        renamed = json.loads(lines[1])
        renamed["sample"]["labels"]["status"] = renamed["sample"]["labels"].pop("step_status")
        lines[1] = json.dumps(renamed)
        samples.write_text("\n".join(lines) + "\n")
        layout = ChainLayout(root)
        with pytest.raises(DatasetFormatError, match=r"datasets/first/train.samples.jsonl line 2"):
            export_yield(layout, read_dataset_manifest(layout))


def test_light_branch_metrics_do_not_fabricate_an_empty_trajectory(tmp_path: Path) -> None:
    layout = empty_layout(tmp_path)
    wal = WALWriter(layout.traces("branch"), "light")
    declared = config(
        task_id="task",
        site="example.test",
        loop="branch",
        archived=False,
        branch=provenance("source", 0, 0, "task"),
    ).model_copy(update={"recording": RecordingPlan(trajectory=False, observations=False)})
    wal.init_run(declared)
    pending = PendingLLMCall(None, capture_trajectory=False)
    pending.complete(ChatInvokeCompletion(completion="unused", usage=None))
    wal.commit_step(0, StepRecord(step=0), call_metrics=[encode_call_metrics(0, 0, pending)])
    wal.finalize(TraceStatus.COMPLETE)
    report = build_report(layout, evidence="rehearsal")
    cover = report.metrics.branch_coverage
    assert cover.empty.total == 0 and cover.branches == 0
    assert "did not select" in cover.skipped["light"]
    assert report.sections.duplicate_branches.not_compared == 0
    assert "light" in report.sections.duplicate_branches.skipped
    assert report.token_use.roles["branch"].calls == 1
    assert report.token_use.roles["branch"].calls_without_usage
    assert "did not select" in report.metrics.privacy.skipped["traces/branch/light"]

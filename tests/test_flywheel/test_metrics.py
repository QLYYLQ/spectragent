"""PRD §9 metrics over a chain root of WAL-committed runs with known answers (arch §4.16).

The chain is ``tests/_helpers/flywheel_traces.py::known_chain``; every
expected number below is derived in that module's docstring from the runs it
commits. Beside each metric: "not measured" is ``null`` with a reason, never
``0``; what was not committed is invisible; a folded replay-log summary
counts every entry it folds.
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest

from spectragent.core.trace.reader import RunTrace
from spectragent.flywheel.metrics import (
    ChainLayout,
    ChainRuns,
    Planted,
    archive_bytes,
    archive_overhead,
    body_completeness_metric,
    branch_coverage,
    env_times,
    export_determinism,
    open_chain,
    open_role,
    percentile,
    privacy,
    ratio,
    replay_determinism,
    restoration,
    restoration_rate,
    run_tier,
    same_path_fidelity,
    tree_digest,
)
from spectragent.flywheel.report import build_report, report_json
from tests._helpers.flywheel_traces import BIG, KnownChain, Step, write_plain
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


class TestShapes:
    def test_a_ratio_over_nothing_is_null_with_its_reason(self) -> None:
        empty = ratio(0, 0, empty="nothing measured")
        assert (empty.value, empty.reason, empty.denominator) == (None, "nothing measured", 0)
        assert ratio(1, 4, empty="x").value == 0.25

    def test_a_percentile_of_nothing_raises(self) -> None:
        with pytest.raises(ValueError, match="no values"):
            percentile([], 50)
        assert percentile([1000, 1200], 50) == 1000.0  # nearest rank, M29's rounding
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

    def test_a_format_1_trace_is_skipped_with_its_reason(
        self, known: KnownChain, tmp_path: Path
    ) -> None:
        copy = tmp_path / "control"
        shutil.copytree(known.runs["ctl-shop"], copy / "old")
        manifest = copy / "old" / "manifest.json"
        document = json.loads(manifest.read_text())
        document["format_version"] = 1
        manifest.write_text(json.dumps(document))
        role = open_role(copy, "control")
        assert role.runs == () and "format 1 trace" in role.skipped["old"]

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
        off = {
            "environment": [
                {
                    "capabilities": [
                        {"capability_id": "browser", "replayability": {"replayability_id": "off"}}
                    ]
                }
            ]
        }
        write_plain(
            tmp_path,
            "ctl-off",
            [Step(duration_ms=3000), Step(duration_ms=900), Step(duration_ms=1000)],
            task_id="shop-1",
            site="shop.test",
            resolved=off,
        )
        control = open_role(tmp_path, "control")
        assert run_tier(control.runs[0].config) == "browser:off"
        found = archive_overhead(chain.role("record"), control)
        assert found.p50_overhead_ms is None
        assert found.reason == (
            "the groups are at different tiers (record browser:pinned, control browser:off)"
        )

    def test_bytes_before_and_after_dedup(self, chain: ChainRuns) -> None:
        found = archive_overhead(chain.role("record"), chain.role("control")).bytes
        total = found.total
        assert (found.runs, found.steps) == (3, 5), "rec-broken's step counts, its env/ is empty"
        assert (
            total.before == total.after + total.saved_by_revisit + total.saved_by_content_addressing
        )
        assert total.saved_by_content_addressing == len(BIG), "app2.js reused app.js's blob"
        assert total.saved_by_revisit > 0, "style2.css was written as a revisit of style.css"
        assert found.per_step_before == total.before / 5
        assert found.per_step_after == total.after / 5

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
    def test_overall_and_by_site(self, chain: ChainRuns) -> None:
        found = same_path_fidelity(chain)
        overall = found.overall
        assert (overall.runs, overall.verified_steps) == (3, 7)
        assert (overall.restorer.numerator, overall.restorer.denominator) == (6, 7)
        assert (overall.strict.numerator, overall.strict.denominator) == (5, 7), (
            "sp-shop-b1's step 1 passed the restorer (within the dHash bound) yet its "
            "screenshot is not the source's byte for byte"
        )
        assert (overall.runs_passing.numerator, overall.runs_passing.denominator) == (2, 3)
        shop, blocked = found.by_site["shop.test"], found.by_site["blocked.test"]
        assert (shop.restorer.value, shop.strict.numerator, shop.strict.denominator) == (1.0, 5, 6)
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
            "strict measure"
        )
        assert found.overall.skipped == {"sp-shop-b1": reason}
        assert found.by_site["shop.test"].skipped == {"sp-shop-b1": reason}
        assert found.by_site["blocked.test"].skipped == {}
        strict = found.overall.strict
        assert (strict.numerator, strict.denominator) == (3, 4), "sp-shop-b0's 3 + blocked's 1"
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
            "(TraceSchemaError): left out of the strict measure"
        )
        assert found.overall.skipped == {"sp-shop-b0": reason, "sp-shop-b1": reason}
        strict = found.overall.strict
        assert (strict.numerator, strict.denominator) == (0, 1), "only sp-blocked-b0 is compared"


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
            "out of the strict measure"
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


class TestPrivacy:
    def test_planted_values_and_shapes_counted_never_written(self, tmp_path: Path) -> None:
        layout = empty_layout(tmp_path)
        run = layout.traces("record") / "run"
        run.mkdir(parents=True)
        (run / "a.json").write_text('{"x": "pw-Qx41-pw-Qx41"}')
        (run / "b.warc.gz").write_bytes(gzip.compress(b"Authorization: pw-Qx41"))
        dataset = layout.dataset("first")
        dataset.mkdir(parents=True)
        (dataset / "train.jsonl").write_text('{"key": "sk-abcdefghijklmnopqrstu"}')
        planted = [
            Planted(label="site password", value="pw-Qx41", source="value"),
            Planted(label="token", value="never-written-anywhere", source="value"),
            Planted(label="teacher api key", value=None, source="env:FLYWHEEL_TEST_UNSET"),
        ]
        found = privacy(layout, planted)
        assert found.files_scanned == 3
        assert [(p.label, p.occurrences, p.files) for p in found.planted] == [
            ("site password", 3, 2),  # two in a.json, one inside the gzip
            ("token", 0, 0),
            ("teacher api key", None, None),
        ]
        assert (
            found.planted[2].reason == "env:FLYWHEEL_TEST_UNSET is not set: nothing to search for"
        )
        assert found.shapes == {"openai-style api key": 1}
        dumped = found.model_dump_json()
        assert "pw-Qx41" not in dumped and "sk-abcdef" not in dumped

    def test_the_known_chain_leaks_nothing(self, layout: ChainLayout) -> None:
        found = privacy(layout, [Planted(label="x", value="planted-xyz-123", source="value")])
        assert found.files_scanned > 0
        assert (found.planted[0].occurrences, found.shapes) == (0, {})

"""The report's sections and decision rules over the known-answer chain (arch §4.16).

Each section's numbers come from ``tests/_helpers/flywheel_traces.py::known_chain``;
each rule is exercised on both sides of its named threshold, and on no data
(``insufficient``, no verdict — never a default conclusion).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.replay.jsonp import CallbackRewrite
from spectragent.core.capabilities.browser.replay.replay_log import ReplayLogEntry
from spectragent.flywheel import report as rules
from spectragent.flywheel.metrics import (
    BranchCoverage,
    BranchDocumentMisses,
    ChainLayout,
    ChainRuns,
    DatasetFormatError,
    EmptyBranches,
    MissDistribution,
    Planted,
    PlantedCount,
    PostBranchMisses,
    Privacy,
    PrivacyGate,
    SiteRuleCandidates,
    anti_bot,
    branch_document_misses,
    duplicate_branches,
    export_yield,
    post_branch_misses,
    ratio,
    read_dataset_manifest,
    retention,
    same_path_fidelity,
    site_rules,
    sites,
    storage,
)
from spectragent.flywheel.report import FlywheelReport, build_report, render_markdown
from tests._helpers.flywheel_traces import (
    Step,
    click,
    dataset_manifest,
    hit,
    provenance,
    write_plain,
    write_replayed,
)
from tests.test_flywheel.conftest import empty_layout


class TestSites:
    def test_by_site_and_the_exclusion_list(self, chain: ChainRuns) -> None:
        found = sites(
            chain,
            fidelity=same_path_fidelity(chain),
            blocked=["blocked.test"],
            exclude_below=rules.SITE_MIN_FIDELITY,
        )
        shop = found.by_site["shop.test"]
        assert (shop.tasks, shop.record_runs, shop.record_status) == (1, 1, {"complete": 1})
        assert shop.record_outcome == {"done": 1}
        broken = found.by_site["broken.test"]
        # Two ledgers: its agent ended in error, its trace recorded all of it.
        assert (broken.record_status, broken.record_outcome) == ({"complete": 1}, {"error": 1})
        assert (shop.same_path.value, shop.restoration.numerator, shop.restoration.denominator) == (
            1.0,
            4,
            6,
        )
        # Every replayed miss of the site counts here, before a checkpoint too: br-b1's POST and
        # br-b2's restoration-time document miss; 2 same-path near misses and br-b0's two, br-b1's
        # three folded image misses.
        counts = (shop.branch_zero_document_miss.numerator, shop.document_misses, shop.other_misses)
        assert counts == (2, 2, 7)
        assert found.by_site["broken.test"].record_failed_at_step0 == 1
        assert found.by_site["broken.test"].same_path.reason == "no same-path run of this site"
        assert [(item.site, item.reasons) for item in found.exclusions] == [
            (
                "blocked.test",
                ["an anti-bot challenge answered its recording", "same-path fidelity 0.00 < 0.8"],
            ),
            ("broken.test", ["1 recording(s) failed at step 0"]),
        ]
        assert found.exclude_sites == ["blocked.test", "broken.test"]
        assert found.yaml == "filters:\n  exclude_sites: [blocked.test, broken.test]\n"

    def test_the_exclusion_threshold_is_the_named_one(self, chain: ChainRuns) -> None:
        lenient = sites(chain, fidelity=same_path_fidelity(chain), blocked=[], exclude_below=0.0)
        assert lenient.exclude_sites == ["broken.test"], "0.0 is not below 0.0; no challenge given"


class TestCallbackRewrites:
    """Every JSONP answer a replay served under the name the page asked for is counted, by site."""

    async def test_rewrites_are_counted_through_folded_repeats(self, tmp_path: Path) -> None:
        layout = empty_layout(tmp_path)
        write_plain(layout.traces("record"), "rec-jsonp", [Step()], task_id="t1", site="jsonp.test")

        def served(kind: str, requested: str | None) -> ReplayLogEntry:
            entry = hit(f"http://jsonp.test/hot?callback={requested or 'cb_0'}")
            rewrite = (
                CallbackRewrite(parameter="callback", recorded="cb_0", requested=requested)
                if requested is not None
                else None
            )
            return entry.model_copy(
                update={"kind": kind, "resource_type": "Script", "callback_rewrite": rewrite}
            )

        # Same path: one hit as recorded, one rewritten, then five rewritten repeats of which
        # the log writes three in full and folds two into one summary.
        same_path = [served("hit", None), served("hit", "cb_1")]
        same_path += [served("exhausted", f"cb_{n}") for n in range(2, 7)]
        await write_replayed(
            layout.traces("same_path"),
            "rec-jsonp-s0-b0",
            [Step(origin="restored", replay=same_path), Step()],
            task_id="t1",
            site="jsonp.test",
            branch=provenance("rec-jsonp", 0, 0, "t1"),
            repeat_limit=3,
        )
        await write_replayed(
            layout.traces("branch"),
            "rec-jsonp-s0-b1",
            [Step(replay=[served("hit", "cb_7")])],
            task_id="t1",
            site="jsonp.test",
            branch=provenance("rec-jsonp", 0, 1, "t1"),
        )
        report = build_report(layout, evidence="rehearsal")
        assert report.sections.sites.by_site["jsonp.test"].callback_rewrites == 1 + 5 + 1
        assert "JSONP callback names rewritten 7" in render_markdown(report)


class TestSubstitutions:
    """Answers from an identical record are counted by site and by how the source request ended."""

    async def test_counted_by_source_outcome_through_folded_repeats(self, tmp_path: Path) -> None:
        layout = empty_layout(tmp_path)
        write_plain(layout.traces("record"), "rec-pic", [Step()], task_id="t1", site="pic.test")

        def substituted(outcome: str) -> ReplayLogEntry:
            return hit("http://pic.test/eggs.webp").model_copy(
                update={
                    "kind": "substituted",
                    "resource_type": "Image",
                    "served_record_id": "donor",
                    "source_outcome": outcome,
                    "unavailable_policy": "serve_identical",
                }
            )

        # Five substitutions for cancelled requests (three in full, two folded), one for a
        # body the recorder missed; a miss and a hit count as neither.
        same_path = [substituted("page_cancelled_before_response") for _ in range(5)]
        same_path += [substituted("memory_cache"), hit("http://pic.test/other.webp")]
        await write_replayed(
            layout.traces("same_path"),
            "rec-pic-s0-b0",
            [Step(origin="restored", replay=same_path), Step()],
            task_id="t1",
            site="pic.test",
            branch=provenance("rec-pic", 0, 0, "t1"),
            repeat_limit=3,
        )
        report = build_report(layout, evidence="rehearsal")
        stats = report.sections.sites.by_site["pic.test"]
        assert stats.substitutions == {"memory_cache": 1, "page_cancelled_before_response": 5}
        assert stats.other_misses == 0
        assert (
            'responses answered by an identical record {"memory_cache": 1, '
            '"page_cancelled_before_response": 5}' in render_markdown(report)
        )

    async def test_a_site_without_substitutions_counts_none(self, chain: ChainRuns) -> None:
        by_site = sites(
            chain, fidelity=same_path_fidelity(chain), blocked=[], exclude_below=0.8
        ).by_site
        assert by_site and all(item.substitutions == {} for item in by_site.values())


class TestAFailedModelCallIsNotTheSites:
    """A failed model call — a 429, a timeout — is the route's failure, never site evidence."""

    def test_it_excludes_nothing_and_is_flagged(self, tmp_path: Path) -> None:
        layout = empty_layout(tmp_path)
        record = layout.traces("record")
        runs = {
            "rec-env": ("env.test", [Step(error_phase="observe")]),
            "rec-llm": ("llm.test", [Step(error_phase="llm")]),
            "rec-parse": ("parse.test", [Step(error_phase="parse")]),
            "rec-later": ("later.test", [Step(actions=[click(1, 1)]), Step(error_phase="llm")]),
        }
        for run_id, (site, steps) in runs.items():
            write_plain(record, run_id, steps, task_id=run_id, site=site)
        report = build_report(layout, evidence="live")
        found = report.sections.sites
        assert found.exclude_sites == ["env.test"], "only the environment's failure at step 0"
        counts = {
            site: (item.record_failed_at_step0, item.record_model_failures)
            for site, item in found.by_site.items()
        }
        assert counts == {
            "env.test": (1, 0),
            "later.test": (0, 1),
            "llm.test": (0, 1),
            "parse.test": (0, 0),
        }
        assert report.inputs.roles["record"].model_failures == ["rec-later", "rec-llm"]
        assert "model call failed: `rec-llm`" in render_markdown(report)


class TestAntiBot:
    def test_the_challenged_recording(self, chain: ChainRuns) -> None:
        found = anti_bot(chain.role("record"))
        assert found.runs_checked == 3 and found.blocked_sites == ["blocked.test"]
        (hit,) = found.hits
        assert hit.model_dump() == {
            "site": "blocked.test",
            "run": "traces/record/rec-blocked",
            "step": 0,
            "status": 403,
            "url": "http://blocked.test/",
            "signals": ["cf-mitigated: challenge", "server: cloudflare + cf-ray"],
        }


class TestDuplicatesAndPostMisses:
    def test_duplicate_branches_against_the_dataset(
        self, chain: ChainRuns, layout: ChainLayout
    ) -> None:
        found = duplicate_branches(chain.role("branch"), read_dataset_manifest(layout))
        assert (found.branches, found.groups, found.duplicates) == (3, 1, 1)
        assert found.rate.value == pytest.approx(1 / 3)
        assert found.dataset_duplicates == 1

    def test_post_branch_misses(self, chain: ChainRuns) -> None:
        found = post_branch_misses(chain.role("branch"))
        assert (found.branches, found.document_misses, found.other_misses) == (3, 1, 0)
        assert (
            found.branches_with_post_miss.numerator,
            found.branches_with_post_miss.denominator,
        ) == (
            1,
            3,
        )


class TestStorageAndRetention:
    def test_bytes_by_category_and_gzip_where_it_can_help(self, chain: ChainRuns) -> None:
        found = storage(chain)
        assert set(found.by_role) == {"record", "control", "same_path", "branch"}
        record = found.by_role["record"]
        assert (record.runs, record.steps) == (3, 5)
        assert record.bytes == sum(item.bytes for item in record.categories.values())
        shots = record.categories["artifacts:browser.screenshot"]
        assert (shots.compressible_bytes, shots.ratio) == (0, None), "PNG: size only"
        warc_and_index = record.categories["env:browser.network"]
        assert 0 < warc_and_index.compressible_bytes < warc_and_index.bytes, (
            "the index is text, the WARC is gzip already"
        )
        assert set(found.datasets) == {"first", "second"}

    def test_retention_projects_from_the_chain_s_own_ratios(
        self, chain: ChainRuns, layout: ChainLayout
    ) -> None:
        stored = storage(chain)
        found = retention(chain, stored, samples=5, tasks=(10, 20))
        assert found.runs_per_task["control"] == pytest.approx(1 / 3)
        assert found.per_sample == stored.datasets["first"] / 5
        assert found.per_task is not None
        assert [(p.tasks, p.bytes) for p in found.projections] == [
            (10, 10 * found.per_task),
            (20, 20 * found.per_task),
        ]

    def test_no_recording_projects_nothing(self, tmp_path: Path) -> None:
        empty = empty_layout(tmp_path)
        from spectragent.flywheel.metrics import open_chain

        chain = open_chain(empty)
        found = retention(chain, storage(chain), samples=None, tasks=(10,))
        assert (found.per_task, found.projections) == (None, [])


class TestExportYield:
    def test_from_the_first_dataset(self, layout: ChainLayout) -> None:
        found = export_yield(layout, read_dataset_manifest(layout))
        assert found.present and found.samples == {"train": 5}
        assert (found.action_failed.numerator, found.action_failed.denominator) == (1, 5)
        assert found.untranslatable == {"no_coordinate": 1}
        assert found.sources_filtered == {
            "branch/br-b1": "1 main-frame document miss(es) in replay"
        }

    def test_no_export_is_null(self, tmp_path: Path) -> None:
        found = export_yield(empty_layout(tmp_path), None)
        assert (found.present, found.action_failed.value) == (False, None)

    @pytest.mark.parametrize(
        "damage",
        [
            lambda m: m["split"].update(counts=m["split"].pop("samples")),  # a renamed key
            lambda m: m.update(format="af-sft-1"),  # an older format
            lambda m: m.pop("leak_scan"),  # a missing section
        ],
        ids=["renamed", "older", "missing"],
    )
    def test_a_manifest_this_build_does_not_write_is_refused_by_name(
        self, tmp_path: Path, damage: Any
    ) -> None:
        """Read through the export's own model: never a silent zero."""
        layout = empty_layout(tmp_path)
        directory = layout.dataset("first")
        directory.mkdir(parents=True)
        manifest = json.loads(dataset_manifest(samples=3).model_dump_json())
        damage(manifest)
        (directory / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(DatasetFormatError, match="datasets/first/manifest.json"):
            read_dataset_manifest(layout)


class TestDecisionInputs:
    def test_branch_document_misses_and_site_rule_candidates(self, chain: ChainRuns) -> None:
        misses = branch_document_misses(chain.role("branch"))
        assert misses.document_misses == 1
        assert (misses.get_no_match.numerator, misses.get_no_match.denominator) == (0, 1), (
            "the one document miss after a checkpoint is br-b1's POST"
        )
        found = site_rules(chain)
        assert found.model_dump() == {
            "runs": 6,
            "sites": 2,
            "by_host": {"shop.test": 3},
            "params": {"shop.test": {"session": 3}},
            "skipped": {},
        }


# ── the rules ─────────────────────────────────────────────────────────


class TestTheDecisionsOnTheKnownChain:
    def test_each_decision_states_its_rule_evidence_and_basis(self, report: FlywheelReport) -> None:
        decisions = report.decisions
        assert decisions.miss_policy.verdict == (
            "evaluate a fallback for documents: only 2 of 3 branches stayed inside the archive "
            "(1 POST document miss(es))"
        )
        assert decisions.recording_coverage.verdict is not None
        assert decisions.recording_coverage.verdict.startswith("do not expand coverage: 0 of 1")
        assert (
            decisions.site_rules.verdict
            == "generate ignore_query rules for review: shop.test [session]"
        )
        assert decisions.retention.verdict is not None and "per task" in decisions.retention.verdict
        for name in ("miss_policy", "recording_coverage", "site_rules", "retention"):
            item = getattr(decisions, name)
            assert (item.status, item.basis) == ("partial", "rehearsal"), name
            assert item.evidence and item.rule, name


def _coverage(clean: int, branches: int, *, sites: int = 10, empty: int = 0) -> BranchCoverage:
    return BranchCoverage(
        branches=branches + empty,
        empty=EmptyBranches(total=empty, by_reason={"model_call_failed": empty} if empty else {}),
        sites=sites,
        zero_document_miss=ratio(clean, branches, empty="no branch run"),
        document_misses=branches - clean,
        other_misses=MissDistribution(),
    )


def _post(misses: int, branches: int) -> PostBranchMisses:
    return PostBranchMisses(
        branches=branches,
        sites=10,
        document_misses=misses,
        other_misses=0,
        branches_with_post_miss=ratio(min(misses, branches), branches, empty="no branch run"),
    )


class TestRuleThresholds:
    @pytest.mark.parametrize(
        ("clean", "branches", "starts"),
        [(9, 10, "keep strict"), (8, 10, "evaluate a fallback for documents"), (0, 0, None)],
    )
    def test_miss_policy(
        self, report: FlywheelReport, clean: int, branches: int, starts: str | None
    ) -> None:
        metrics = report.metrics.model_copy(update={"branch_coverage": _coverage(clean, branches)})
        sections = report.sections.model_copy(update={"post_branch_misses": _post(0, branches)})
        found = rules._decide_miss_policy(metrics, sections, "live")
        if starts is None:
            assert (found.status, found.verdict) == ("insufficient", None)
        else:
            assert found.status == "supported" and found.verdict is not None
            assert found.verdict.startswith(starts)
        assert "0.9" in found.rule and "STRICT_MIN_IN_ARCHIVE_SHARE" in found.rule

    def test_empty_branches_leave_the_strict_share_and_are_counted(
        self, report: FlywheelReport
    ) -> None:
        """Three failed teacher calls beside 8 of 10 clean branches: still 8 of 10, not 11 of 13."""
        coverage = _coverage(8, 10, empty=3)
        metrics = report.metrics.model_copy(update={"branch_coverage": coverage})
        sections = report.sections.model_copy(update={"post_branch_misses": _post(0, 13)})
        found = rules._decide_miss_policy(metrics, sections, "live")
        assert found.verdict == (
            "evaluate a fallback for documents: only 8 of 10 branches stayed inside the archive "
            "(0 POST document miss(es); 3 empty branch(es) left out)"
        )
        assert any(
            ref.pointer.endswith("/empty/total") and ref.value == 3 for ref in found.evidence
        )

    def test_only_empty_branches_is_insufficient_and_says_so(self, report: FlywheelReport) -> None:
        metrics = report.metrics.model_copy(update={"branch_coverage": _coverage(0, 0, empty=2)})
        found = rules._decide_miss_policy(metrics, report.sections, "live")
        assert (found.status, found.verdict) == ("insufficient", None)
        assert found.status_reason == "every branch was empty (2: no action from the checkpoint on)"

    @pytest.mark.parametrize(
        ("unrecorded", "misses", "starts"),
        [
            (1, 2, "expand coverage"),
            (1, 3, "do not expand coverage: 1 of 3"),
            (0, 0, "do not expand coverage: no"),
        ],
    )
    def test_recording_coverage(
        self, report: FlywheelReport, unrecorded: int, misses: int, starts: str
    ) -> None:
        inputs = report.decision_inputs.model_copy(
            update={
                "branch_document_misses": BranchDocumentMisses(
                    branches=4,
                    sites=10,
                    document_misses=misses,
                    get_no_match=ratio(unrecorded, misses, empty="no document miss in any branch"),
                )
            }
        )
        found = rules._decide_recording_coverage(inputs, "live")
        assert found.verdict is not None and found.verdict.startswith(starts)
        assert found.status == "supported"

    def test_recording_coverage_without_branches_is_insufficient(
        self, report: FlywheelReport
    ) -> None:
        inputs = report.decision_inputs.model_copy(
            update={
                "branch_document_misses": BranchDocumentMisses(
                    branches=0,
                    sites=0,
                    document_misses=0,
                    get_no_match=ratio(0, 0, empty="no document miss in any branch"),
                )
            }
        )
        found = rules._decide_recording_coverage(inputs, "rehearsal")
        assert (found.status, found.verdict) == ("insufficient", None)

    @pytest.mark.parametrize(
        ("misses", "verdict"),
        [
            (2, "generate ignore_query rules for review: a.test [q]"),
            (1, "keep hand-written rules: no host reached 2 near misses"),
        ],
    )
    def test_site_rules(self, report: FlywheelReport, misses: int, verdict: str) -> None:
        inputs = report.decision_inputs.model_copy(
            update={
                "site_rule_candidates": SiteRuleCandidates(
                    runs=4, sites=1, by_host={"a.test": misses}, params={"a.test": {"q": misses}}
                )
            }
        )
        assert rules._decide_site_rules(inputs, "live").verdict == verdict

    def test_no_data_anywhere_is_insufficient_everywhere(self, tmp_path: Path) -> None:
        empty = build_report(empty_layout(tmp_path), evidence="live")
        for group in (
            empty.recommendations.metrics,
            empty.recommendations.sections,
            empty.decisions,
        ):
            for name, item in group:
                assert (item.status, item.verdict) == ("insufficient", None), name

    def test_live_evidence_from_too_few_sites_is_partial(self, layout: ChainLayout) -> None:
        """The known chain is live-shaped data from three sites, one of them branched."""
        live = build_report(layout, evidence="live")
        miss_policy = live.decisions.miss_policy
        assert (miss_policy.status, miss_policy.sites) == ("partial", 1)
        assert miss_policy.status_reason == (
            "insufficient sample: data from 1 site(s); supported needs at least 10 "
            "(SUPPORTED_MIN_SITES)"
        )
        assert miss_policy.verdict is not None, "partial still says what the evidence shows"
        export = live.recommendations.metrics.export_determinism
        assert (export.status, export.sites) == ("partial", 1), "the export's one source site"

    @pytest.mark.parametrize(("sites", "status"), [(9, "partial"), (10, "supported")])
    def test_supported_needs_ten_sites(self, tmp_path: Path, sites: int, status: str) -> None:
        """Live recordings from *sites* sites: the sites section is supported from ten on."""
        assert rules.SUPPORTED_MIN_SITES == 10
        layout = empty_layout(tmp_path)
        for index in range(sites):
            write_plain(
                layout.traces("record"),
                f"rec-{index}",
                [Step(actions=[click(1, 1)])],
                task_id=f"t{index}",
                site=f"site{index}.test",
            )
        found = build_report(layout, evidence="live").recommendations.sections.sites
        assert (found.status, found.sites) == (status, sites)
        assert (found.status_reason is None) == (status == "supported")
        rehearsed = build_report(layout, evidence="rehearsal").recommendations.sections.sites
        assert rehearsed.status == "partial", "rehearsal evidence is never more than partial"


UNSET_KEY = Planted(label="teacher api key", value=None, source="env:OPENAI_API_KEY")
SITE_PASSWORD = Planted(label="site password", value="pw-never-written-7", source="value")


class TestThePrivacyRule:
    """Privacy is "planted occurrences = 0": a credential not searched for concludes nothing."""

    @pytest.mark.parametrize(
        "planted",
        [[UNSET_KEY], [], [SITE_PASSWORD, UNSET_KEY]],
        ids=["env_unset", "no_spec", "one_of_two_unset"],
    )
    def test_no_leak_is_concluded_while_a_planted_value_went_unsearched(
        self, layout: ChainLayout, planted: list[Planted]
    ) -> None:
        live = build_report(layout, evidence="live", planted=planted)
        found = live.recommendations.metrics.privacy
        assert (found.status, found.verdict) == ("insufficient", None)
        privacy = live.metrics.privacy
        assert privacy.datasets.items > 0
        assert privacy.model_requests.shapes == privacy.datasets.shapes == {}
        assert "needs every planted credential searched for" in found.rule
        unset = [item.label for item in planted if item.value is None]
        assert found.status_reason == (
            f"not searched: {', '.join(unset)}"
            if unset
            else "no planted credential to search for (the chain spec plants none, or there is "
            "no spec)"
        )

    def test_every_planted_value_searched_and_absent_concludes_no_leak(
        self, layout: ChainLayout
    ) -> None:
        live = build_report(layout, evidence="live", planted=[SITE_PASSWORD])
        found = live.recommendations.metrics.privacy
        files = live.metrics.privacy.datasets.items
        assert (found.status, found.sites, found.verdict) == (
            "partial",
            3,
            "no planted credential (1 searched) and no credential shape in what was sent to the "
            f"model (0 request(s)) or in the datasets ({files} file(s)); 1 run(s) not read (see "
            "skipped)",
        ), "every read run's site counts; three are too few for supported"

    def test_the_rule_names_the_two_gates_and_what_is_not_searched(
        self, layout: ChainLayout
    ) -> None:
        found = build_report(layout, evidence="live").recommendations.metrics.privacy
        assert "what was sent to the model" in found.rule and "exported datasets" in found.rule
        assert "env/ and the rest of a trace stay on this machine and are not searched" in (
            found.rule
        )

    def test_a_credential_shape_is_a_leak_even_with_a_value_unsearched(self) -> None:
        unsearched = PlantedCount(
            label="teacher api key",
            source="env:OPENAI_API_KEY",
            occurrences=None,
            items=None,
            reason="env:OPENAI_API_KEY is not set: nothing to search for",
        )
        privacy = Privacy(
            model_requests=PrivacyGate(
                items=3,
                planted=[unsearched],
                shapes={"openai-style api key": 1},
                shape_items={"openai-style api key": 1},
            ),
            datasets=PrivacyGate(items=2, planted=[unsearched], shapes={}, shape_items={}),
            runs=10,
            sites=10,
        )
        found = rules._rec_privacy(privacy, "live")
        assert (found.status, found.verdict) == (
            "supported",
            "leak — sent to the model: no planted value, 1 credential-shaped string(s): fix "
            "before the next run or export; not searched: teacher api key",
        )

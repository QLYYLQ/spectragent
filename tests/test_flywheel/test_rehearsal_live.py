"""The metrics chain rehearsed end to end on the fixture site (the metrics gate's acceptance, arch §4.16 "演练").

One rehearsal — ``run_rehearsal``, the function behind the one command
``python -m tests._helpers.flywheel_rehearsal --out DIR``, called in this
process — runs the whole chain in a real Chrome with scripted models:
record → control → same-path replay → branch → export twice → report. In
this process on purpose, deep in the full suite and with the sign-in task
recorded right before the drift task: that is where a model call's TCP
connect once never completed (fixed in the LLM providers, arch §4.7). Each
value of the report is asserted together with the reason it has
that value, read back from the runs themselves where it can be:

- the deterministic shop flow (a GET form submission included) replays
  exactly — fidelity 1.0, strict hashes too; the drift site's click asks a
  ``performance.timeOrigin`` URL no recording holds, so its checkpoint check
  fails on ``document_miss`` and its replays end on Chrome's error page,
  which names the URL (the one same-input pair whose screenshots differ);
- the branch that submits the POST "quick add" form misses its document; the
  two that repeat the recording stay inside the archive, the second flagged
  as a duplicate in the dataset too;
- the 403 with Cloudflare's marks puts its site on the exclusion list, and
  so does the drift site's fidelity;
- the credentials the sign-in page assembled and sent every way a page can,
  and the teacher's key, occur nowhere under ``traces/`` or ``datasets/``;
- every recommendation is ``partial``: fixture evidence, from three sites;
- nothing left the machine.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from spectragent.core.capabilities.browser.archive.network import INDEX_NAME
from spectragent.core.capabilities.browser.replay.archive_index import (
    BodyLine,
    ExchangeLine,
    IndexLine,
    load_archive,
)
from spectragent.flywheel.chain import ChainSpec, build_spec_report
from spectragent.flywheel.metrics import ChainLayout, open_chain
from spectragent.flywheel.report import report_json
from tests._helpers.flywheel_rehearsal import (
    BRANCH_STEP,
    PLANTED,
    REHEARSAL_TOKEN_LIMIT,
    SHOP_KETTLE,
    TASKS,
    run_rehearsal,
)
from tests._helpers.scripted_llm import REPLY_USAGE

pytestmark = pytest.mark.requires_chrome

SHOP, DRIFT, BLOCKED = "shop.example.test", "drift.example.test", "blocked.example.test"


@dataclass
class Rehearsed:
    """What the rehearsal left: its directory, its summary (``Rehearsal.summary``), the chain root."""

    out: Path
    summary: dict[str, Any]

    @property
    def layout(self) -> ChainLayout:
        return ChainLayout(Path(self.summary["chain_root"]))


@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory: pytest.TempPathFactory) -> Rehearsed:
    out = tmp_path_factory.mktemp("rehearsal")
    ran = run_rehearsal(out)
    # Through JSON, exactly as the command writes ``rehearsal.json``.
    return Rehearsed(out=ran.out, summary=json.loads(json.dumps(ran.summary())))


@pytest.fixture(scope="module")
def report(rehearsal: Rehearsed) -> dict:
    return json.loads((rehearsal.layout.report_dir / "report.json").read_text())


def test_the_chain_ran_every_stage_with_the_scripts(rehearsal: Rehearsed) -> None:
    stages = rehearsal.summary["stages"]
    assert set(stages) == {"record", "control", "same_path", "branch", "export", "report"}
    assert all(outcome["error"] is None for outcome in stages.values())
    assert [len(stages[s]["runs"]) for s in ("record", "control", "same_path", "branch")] == [
        4,
        4,
        8,
        3,
    ]
    routed = {
        stage: [tuple(item) for item in items]
        for stage, items in rehearsal.summary["routed"].items()
    }
    assert rehearsal.summary["model_errors"] == {
        s: [] for s in ("record", "control", "same_path", "branch")
    }
    recorded_calls = sum(len(task.replies) for task in TASKS)
    assert len(routed["record"]) == len(routed["control"]) == recorded_calls
    # Same-path: one decision per task, at each source's last step (the selector `last`).
    assert sorted(routed["same_path"]) == sorted(
        (task.task_id, len(task.replies) - 1) for task in TASKS for _ in range(2)
    )
    assert routed["branch"] == [(SHOP_KETTLE.task_id, BRANCH_STEP)] * 3


def test_rehearsal_values_are_explained(rehearsal: Rehearsed, report: dict) -> None:
    metrics, sections = report["metrics"], report["sections"]
    fidelity = metrics["same_path_fidelity"]["by_site"]
    # The deterministic flows replay exactly: every check, every screenshot byte.
    assert fidelity[SHOP]["restorer"]["value"] == fidelity[SHOP]["strict"]["value"] == 1.0
    assert fidelity[BLOCKED]["restorer"]["value"] == 1.0, "the archive serves the 403 as recorded"
    # The drift site: the clicked link's URL carries performance.timeOrigin, which pinned does
    # not fix, so the replay asks a document the recording never fetched.
    assert fidelity[DRIFT]["restorer"] == {
        "numerator": 2,
        "denominator": 4,
        "value": 0.5,
        "reason": None,
    }
    reasons = {
        tuple(f["reasons"]) for f in metrics["same_path_fidelity"]["overall"]["first_failures"]
    }
    # The screenshot's same-capture source URL now proves that Chrome's error page
    # is not the recorded story. A document miss and URL mismatch are required;
    # the dHash distance may sit on either side of the 5-bit threshold as the
    # error page's time-origin digits change.
    assert reasons and reasons <= {
        ("document_miss", "url_mismatch"),
        ("document_miss", "url_mismatch", "screenshot_distance"),
    }
    overall = metrics["same_path_fidelity"]["overall"]
    assert overall["url_coverage"]["value"] == 1.0
    assert overall["runs_reaching_checkpoint"]["numerator"] == 8
    assert overall["runs_passing"]["numerator"] == 6, "the two drift paths fail restoration"
    chain = open_chain(rehearsal.layout)
    # Each replay misses the one URL its own time origin names. Chrome reloads its error page
    # once on its own, about a second later: the observation waits for the page to settle, so
    # the run is still there to ask the same URL again (the terminal observation collects it).
    drift_misses = {
        run.rel: {entry.url for _step, entry, _weight in run.decisions if entry.diverged}
        for run in chain.role("same_path").runs
        if run.site == DRIFT
    }
    assert len(drift_misses) == 2 and all(len(urls) == 1 for urls in drift_misses.values())
    (first,), (second,) = drift_misses.values()
    assert "next.html?visit=" in first and "next.html?visit=" in second
    assert first != second, "each replay has its own time origin"

    assert metrics["restoration"]["ok"] == metrics["restoration"]["total"] == 3 * (BRANCH_STEP + 1)

    coverage = metrics["branch_coverage"]
    assert coverage["zero_document_miss"]["numerator"] == 2 and coverage["branches"] == 3
    (post,) = [
        entry
        for run in chain.role("branch").runs
        for _step, entry, _weight in run.decisions
        if entry.diverged
    ]
    assert (post.method, post.resource_type, urlsplit(post.url).path) == (
        "POST",
        "Document",
        "/flywheel_shop/cart",
    )
    assert sections["post_branch_misses"]["document_misses"] == 1
    # The product page asks recommend.json with its time origin. Its asynchronous miss
    # can be collected with the click or the terminal observation; the committed batch
    # determines attribution, not which action caused the request.
    recommend_misses = [
        (run, step, entry, weight)
        for run in chain.role("branch").runs
        for step, entry, weight in run.decisions
        if entry.kind == "miss" and not entry.diverged
    ]
    assert len(recommend_misses) == 2
    collected_as = []
    for run, step, entry, weight in recommend_misses:
        assert (weight, entry.method, urlsplit(entry.url).path, entry.resource_type) == (
            1,
            "GET",
            "/flywheel_shop/recommend.json",
            "XHR",
        )
        assert (entry.request_class, entry.reason) == ("subresource", "no_match")
        assert step is not None and step in (BRANCH_STEP, BRANCH_STEP + 1)
        owner = run.step(step)
        assert owner is not None
        if step == BRANCH_STEP:
            assert owner.origin == "policy"
            assert [action.name for action in owner.actions] == ["browser.click_coordinate"]
            collected_as.append("browser.click_coordinate")
        else:
            assert owner.origin == "terminal" and not owner.actions
            collected_as.append("terminal")
    assert coverage["other_misses"]["total"] == 2
    assert coverage["other_misses"]["by_host"] == {SHOP: 2}
    assert coverage["other_misses"]["by_action"] == dict(Counter(collected_as))

    assert sections["duplicate_branches"]["duplicates"] == 1
    assert sections["duplicate_branches"]["dataset_duplicates"] == 1

    determinism = metrics["replay_determinism"]["by_role"]
    assert determinism["branch"]["identical"]["value"] == 1.0
    assert (
        determinism["same_path"]["identical"]["numerator"],
        determinism["same_path"]["identical"]["denominator"],
    ) == (3, 4), "the drift pair: Chrome's error page names the URL it could not load"

    assert sections["sites"]["exclude_sites"] == [BLOCKED, DRIFT]
    (hit,) = sections["anti_bot"]["hits"]
    assert (hit["site"], hit["status"], hit["signals"]) == (
        BLOCKED,
        403,
        ["cf-mitigated: challenge", "server: cloudflare + cf-ray"],
    )

    assert metrics["export_determinism"]["identical"] is True
    assert sections["export_yield"]["sources_filtered"] and all(
        "max_document_misses" in reason
        for reason in sections["export_yield"]["sources_filtered"].values()
    ), "the POST branch's document miss keeps it out of the dataset"

    privacy = metrics["privacy"]
    assert privacy["model_requests"]["items"] > 0 and privacy["datasets"]["items"] > 0
    for gate in ("model_requests", "datasets"):
        assert [(p["label"], p["occurrences"]) for p in privacy[gate]["planted"]] == [
            (label, 0) for label, _value in PLANTED
        ], gate
        assert privacy[gate]["shapes"] == {}, gate

    overhead = metrics["archive_overhead"]
    assert overhead["archived"]["tier"] == overhead["control"]["tier"] == "browser:pinned"
    assert overhead["p50_overhead_ms"] is not None and overhead["reason"] is None
    bytes_ = overhead["bytes"]["total"]
    # A collection's first logo/CSS response must own its payload. Reloads may
    # repeat assets within that same collection. Verify every revisit's local
    # reference and body, including the login API pair whose tiny payload costs
    # less than the revisit's headers, before comparing the signed metric.
    savings = []
    login_savings = []
    repeated_assets = []
    asset_collections = set()
    for run in chain.role("record").runs:
        archive = load_archive(run.trace)
        records = {record.record_id: record for record in archive.index.records}
        for _step, entry in run.trace.iter_archive("browser.network", include_trailing=True):
            if entry.name != INDEX_NAME:
                continue
            exchanges = [
                line.root
                for line in run.trace.archive_lines(entry.path, IndexLine)
                if isinstance(line.root, ExchangeLine)
            ]
            full = {
                line.response_record_id: line
                for line in exchanges
                if not line.revisit_of and line.response_record_id
            }
            seen_assets = set()
            for line in exchanges:
                path = urlsplit(line.url).path
                if path in ("/flywheel_shop/site.css", "/flywheel_shop/logo.png"):
                    repeated_assets.append(line)
                    asset_collections.add((run.rel, entry.path))
                    if path not in seen_assets:
                        assert line.revisit_of is None, (
                            "separate collections own independent responses"
                        )
                    seen_assets.add(path)
                if line.revisit_of is None:
                    continue
                original = full[line.revisit_of]  # the reference must stay inside this index batch
                assert original.payload_sha256 == line.payload_sha256
                body = archive.body(records[line.record_id])
                assert archive.body(records[original.record_id]) == body
                assert original.warc.response is not None and line.warc.response is not None
                saving = original.warc.response.length - line.warc.response.length
                if {path, urlsplit(original.url).path} == {
                    "/flywheel_shop/api/me",
                    "/flywheel_shop/api/login",
                }:
                    assert body == b'{"ok": true}'
                    assert saving < 0, "the revisit costs more than this tiny complete response"
                    login_savings.append(saving)
                savings.append(saving)
    assert len(repeated_assets) > 2, "the shop assets were recorded on multiple pages"
    assert len(asset_collections) > 1, "the repeated assets must span independent collections"
    assert len(login_savings) == 1, "the login API pair must deduplicate within its collection"
    assert bytes_["saved_by_revisit"] == sum(savings)
    assert bytes_["before"] == (
        bytes_["after"] + bytes_["saved_by_revisit"] + bytes_["saved_by_content_addressing"]
    )
    assert metrics["body_completeness"]["complete"]["value"] == 1.0

    endpoints = {
        (item["host"], item["method"], item["path"]): (
            item["runs"],
            item["requests"],
            item["query"],
            item["body"],
        )
        for item in report["decision_inputs"]["site_rule_candidates"]["endpoints"]
    }
    # Counted once per replay run: four shop replays ask once each; the two drift replays ask
    # their URL twice (the error page's own reload).
    assert endpoints == {
        (SHOP, "GET", "/flywheel_shop/recommend.json"): (4, 4, {"session": 4}, {}),
        (DRIFT, "GET", "/flywheel_drift/next.html"): (2, 4, {"visit": 2}, {}),
    }
    decisions = report["decisions"]
    assert decisions["miss_policy"]["verdict"].startswith("evaluate a fallback for documents")
    assert decisions["recording_coverage"]["verdict"].startswith("do not expand coverage: 0 of 1")
    assert (
        "drift.example.test GET /flywheel_drift/next.html [query visit]"
        in decisions["site_rules"]["verdict"]
    )
    statuses = {item["status"] for item in decisions.values()}
    statuses |= {
        item["status"] for group in report["recommendations"].values() for item in group.values()
    }
    assert statuses == {"partial"}, "fixture evidence is never more than partial"
    assert coverage["empty"] == {"total": 0, "by_reason": {}}, "every scripted teacher acted"


def test_report_recomputes_byte_identical(rehearsal: Rehearsed, report: dict) -> None:
    layout = rehearsal.layout
    written = (layout.report_dir / "report.json").read_text()
    again = build_spec_report(ChainSpec.load(rehearsal.out / "chain.yaml"))
    assert report_json(again) == written
    for path in (layout.report_dir / "report.json", layout.report_dir / "report.md"):
        text = path.read_text()
        assert str(layout.root) not in text and str(Path.home()) not in text
        assert all(value not in text for _label, value in PLANTED)


def test_the_token_limit_counted_every_scripted_call(rehearsal: Rehearsed, report: dict) -> None:
    """The real bench, handed what was left of the chain's limit, counted each call it made.

    Every scripted reply reports the same usage (``REPLY_USAGE``), so each stage's tokens are
    that many times the requests its model answered — read back from the call records, with
    no metrics hook involved.
    """
    record = json.loads(rehearsal.layout.chain_record.read_text())
    routed = rehearsal.summary["routed"]
    per_call = REPLY_USAGE["total_tokens"]
    assert record["token_limit"] == REHEARSAL_TOKEN_LIMIT
    assert record["stopped_by_token_limit"] is None
    left = REHEARSAL_TOKEN_LIMIT
    for stage in ("record", "control", "same_path", "branch"):
        entry = record["stages"][stage]
        (argv,) = entry["argv"]
        assert argv[-2:] == ["--token-limit", str(left)], stage
        assert entry["token_use"]["tokens"] == per_call * len(routed[stage]), stage
        assert entry["token_use"]["calls"] == len(routed[stage]), stage
        assert entry["token_use"]["calls_without_usage"] == [], stage
        left -= entry["token_use"]["tokens"]
    assert record["tokens_used"] == REHEARSAL_TOKEN_LIMIT - left
    use = report["token_use"]
    assert (use["limit"], use["tokens"], use["limit_reached"]) == (
        REHEARSAL_TOKEN_LIMIT,
        record["tokens_used"],
        False,
    )


def test_no_non_loopback_egress(rehearsal: Rehearsed) -> None:
    egress = rehearsal.summary["egress"]
    assert egress["connections"] > 0, "the audit saw the run's own connections"
    assert egress["non_loopback"] == []
    hosts = {
        urlsplit(line.url).hostname
        for run in open_chain(rehearsal.layout).role("record").runs
        for _step, line in run.index_lines
        if not isinstance(line, BodyLine)
    }
    assert hosts <= {SHOP, DRIFT, BLOCKED}, hosts
    served = set(rehearsal.summary["served_hosts"])
    assert served <= {SHOP, DRIFT, BLOCKED}, "only the recordings reached the fixture"

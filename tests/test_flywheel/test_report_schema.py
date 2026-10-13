"""The report's shape: every item covered, evidence that resolves, determinism, secrecy (arch §4.16 "报告").

The metrics gate's acceptance — "the report covers every metric and gives an
evidence-backed recommendation for every open decision"
(``docs/execution_dag.md``) — is checked here on its coverage half: every
metric, section and decision is in ``report.json`` and ``report.md`` with a
recommendation; on a rehearsal basis, or from fewer sites than a supported
recommendation needs, each one says ``partial`` and why.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from spectragent.flywheel.metrics import ChainLayout, Planted
from spectragent.flywheel.report import (
    INSUFFICIENT_SAMPLE,
    REHEARSAL_NOTE,
    REPORT_FORMAT,
    FlywheelReport,
    build_report,
    render_markdown,
    report_json,
    write_report,
)
from spectragent.schemas.dataset import DATASET_FORMAT
from tests.test_flywheel.conftest import empty_layout

#: The report's metrics, sections and pending decisions (arch §4.16).
REPORT_METRICS = (
    "archive_overhead",
    "body_completeness",
    "same_path_fidelity",
    "restoration",
    "branch_coverage",
    "replay_determinism",
    "export_determinism",
    "privacy",
)
REPORT_SECTIONS = (
    "sites",
    "anti_bot",
    "duplicate_branches",
    "post_branch_misses",
    "storage",
    "retention",
    "export_yield",
)
REPORT_DECISIONS = ("miss_policy", "recording_coverage", "site_rules", "retention")
MD_HEADINGS = (
    "### 归档开销",
    "### body 完整率",
    "### 原路径回放保真",
    "### 恢复校验率",
    "### 分叉结果覆盖",
    "### 回放确定性",
    "### 导出确定性",
    "### 隐私",
    "### 站点与剔除名单",
    "### anti-bot / Cloudflare",
    "### 重复分叉率",
    "### POST 分叉的 miss",
    "### 存储增长与压缩评估",
    "### 保留期与容量",
    "### 导出产出",
    "### Miss policy",
    "### Recording coverage",
    "### Site rules",
    "### Retention",
)


def resolve(document: Any, pointer: str) -> Any:
    """RFC 6901."""
    for raw in pointer.split("/")[1:]:
        part = raw.replace("~1", "/").replace("~0", "~")
        document = document[int(part)] if isinstance(document, list) else document[part]
    return document


def recommendations(report: FlywheelReport) -> dict[str, Any]:
    found = {f"metrics.{k}": v for k, v in report.recommendations.metrics}
    found |= {f"sections.{k}": v for k, v in report.recommendations.sections}
    found |= {f"decisions.{k}": v for k, v in report.decisions}
    return found


def test_every_report_item_is_present(report: FlywheelReport) -> None:
    document = json.loads(report_json(report))
    assert document["format"] == REPORT_FORMAT == "af-flywheel-report-5"
    assert set(document["token_use"]["roles"]) == {"record", "control", "same_path", "branch"}
    assert set(document["metrics"]) == set(REPORT_METRICS)
    assert set(document["sections"]) == set(REPORT_SECTIONS)
    assert set(document["decisions"]) == set(REPORT_DECISIONS)
    assert set(document["recommendations"]["metrics"]) == set(REPORT_METRICS)
    assert set(document["recommendations"]["sections"]) == set(REPORT_SECTIONS)
    markdown = render_markdown(report)
    for heading in ("## Token use", *MD_HEADINGS):
        assert heading in markdown, heading
    assert markdown.count("**Recommendation**") == len(REPORT_METRICS) + len(REPORT_SECTIONS) + len(
        REPORT_DECISIONS
    )


def test_every_evidence_pointer_resolves_to_its_value(report: FlywheelReport) -> None:
    document = json.loads(report_json(report))
    for name, item in recommendations(report).items():
        assert item.evidence, f"{name} rests on no evidence"
        for ref in item.evidence:
            assert resolve(document, ref.pointer) == ref.value, (name, ref.pointer)


def test_rehearsal_basis_is_partial(report: FlywheelReport) -> None:
    statuses = {name: item.status for name, item in recommendations(report).items()}
    assert set(statuses.values()) <= {"partial", "insufficient"}
    assert statuses["decisions.miss_policy"] == "partial"
    assert all(item.basis == "rehearsal" for item in recommendations(report).values())
    reason = report.decisions.miss_policy.status_reason
    assert reason is not None and REHEARSAL_NOTE in reason and INSUFFICIENT_SAMPLE in reason, (
        "the known chain is fixture data from one branched site: both reasons"
    )
    assert REHEARSAL_NOTE in render_markdown(report)


def test_no_data_is_insufficient(tmp_path: Path) -> None:
    empty = build_report(empty_layout(tmp_path), evidence="rehearsal")
    for name, item in recommendations(empty).items():
        assert (item.status, item.verdict, item.sites) == ("insufficient", None, 0), name
        assert item.status_reason, f"{name} does not say why it has no data"


def test_every_status_below_supported_says_why(report: FlywheelReport) -> None:
    for name, item in recommendations(report).items():
        assert (item.status == "supported") == (item.status_reason is None), name
        if item.status == "partial":
            assert f"- why partial: {item.status_reason}" in render_markdown(report), name


def test_the_inputs_name_what_ran(report: FlywheelReport) -> None:
    roles = report.inputs.roles
    assert {role: item.runs for role, item in roles.items()} == {
        "record": 3,
        "control": 1,
        "same_path": 3,
        "branch": 3,
    }
    assert roles["branch"].skipped == {
        "broken": "not a readable trace: manifest.json does not read back (TraceSchemaError)"
    }
    (entry,) = roles["record"].components
    assert entry.runs == 3 and entry.components["agent"] == "ui_tars"
    assert report.inputs.datasets["first"].format == DATASET_FORMAT


def test_report_recomputes_byte_identical(layout: ChainLayout, tmp_path: Path) -> None:
    planted = [Planted(label="site password", value="pw-Qx41", source="value")]
    first = build_report(layout, evidence="rehearsal", planted=planted, name="known")
    second = build_report(layout, evidence="rehearsal", planted=planted, name="known")
    assert report_json(first) == report_json(second)
    a = write_report(first, tmp_path / "a")
    b = write_report(second, tmp_path / "b")
    assert [path.read_bytes() for path in a] == [path.read_bytes() for path in b]
    assert [path.name for path in a] == ["report.json", "report.md"]


def test_no_planted_value_absolute_path_or_clock_in_the_report(
    layout: ChainLayout, tmp_path: Path
) -> None:
    planted = [
        Planted(label="site password", value="pw-Qx41-never-written", source="value"),
        Planted(label="teacher api key", value="sk-should-not-appear-0123456789", source="value"),
    ]
    written = write_report(
        build_report(layout, evidence="rehearsal", planted=planted), tmp_path / "out"
    )
    for path in written:
        text = path.read_text()
        for value in ("pw-Qx41-never-written", "sk-should-not-appear-0123456789"):
            assert value not in text, path.name
        assert str(layout.root) not in text and str(Path.home()) not in text, path.name
        assert "/private/" not in text and "/tmp/" not in text, path.name
        assert "2026-" not in text, f"{path.name} holds a date"
    document = json.loads(written[0].read_text())
    for gate in ("model_requests", "datasets"):
        assert [item["label"] for item in document["metrics"]["privacy"][gate]["planted"]] == [
            "site password",
            "teacher api key",
        ]


#: Planning codes a reader of the report could not resolve (AGENTS.md §3.6): local-plan
#: section and question numbers, module ids, defect and spike labels.
PLANNING_CODE = re.compile(
    r"(?<![A-Za-z0-9])(?:PRD|M\d{2}|[Qq]\d|H\d{2}|S2[ab]?)(?![A-Za-z0-9])|§(?:9|10|11|12)(?![0-9.])"
)


def test_the_report_names_things_without_planning_codes(
    report: FlywheelReport, tmp_path: Path
) -> None:
    for coded in ("decisions.q1_miss_policy", "Q4_EXPAND", "(PRD §11)", "M29's bound", "S2b"):
        assert PLANNING_CODE.search(coded), f"the pattern misses {coded!r}"
    assert not PLANNING_CODE.search("arch §4.14, sha256 0f1e, 1.5 MB, M2M, Q10x"), "no false hit"
    for path in write_report(report, tmp_path / "out"):
        found = PLANNING_CODE.findall(path.read_text())
        assert not found, (path.name, found)

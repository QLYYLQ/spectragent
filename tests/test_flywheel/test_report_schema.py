"""The report's shape: every PRD item covered, evidence that resolves, determinism, secrecy (arch §4.16 "报告").

PRD M33's acceptance — "the report covers every §9 row and gives an
evidence-backed recommendation for every open decision" — is checked here on
its coverage half: every row, section and decision is in ``report.json`` and
``report.md`` with a recommendation; on a rehearsal basis each one says
``partial``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spectragent.flywheel.metrics import ChainLayout, Planted
from spectragent.flywheel.report import (
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

#: PRD §9's rows, §10 / §11's items M33 owns, PRD M33's decisions.
PRD_METRICS = (
    "archive_overhead",
    "body_completeness",
    "same_path_fidelity",
    "restoration",
    "branch_coverage",
    "replay_determinism",
    "export_determinism",
    "privacy",
)
PRD_SECTIONS = (
    "sites",
    "anti_bot",
    "duplicate_branches",
    "post_branch_misses",
    "storage",
    "retention",
    "export_yield",
)
PRD_DECISIONS = ("q1_miss_policy", "q4_coverage", "site_rules", "q7_retention")
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
    "### Q1",
    "### Q4",
    "### Site rules",
    "### Q7",
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


def test_every_prd_item_is_present(report: FlywheelReport) -> None:
    document = json.loads(report_json(report))
    assert document["format"] == REPORT_FORMAT
    assert set(document["metrics"]) == set(PRD_METRICS)
    assert set(document["sections"]) == set(PRD_SECTIONS)
    assert set(document["decisions"]) == set(PRD_DECISIONS)
    assert set(document["recommendations"]["metrics"]) == set(PRD_METRICS)
    assert set(document["recommendations"]["sections"]) == set(PRD_SECTIONS)
    markdown = render_markdown(report)
    for heading in MD_HEADINGS:
        assert heading in markdown, heading
    assert markdown.count("**Recommendation**") == len(PRD_METRICS) + len(PRD_SECTIONS) + len(
        PRD_DECISIONS
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
    assert statuses["decisions.q1_miss_policy"] == "partial"
    assert all(item.basis == "rehearsal" for item in recommendations(report).values())
    assert REHEARSAL_NOTE in render_markdown(report)


def test_no_data_is_insufficient(tmp_path: Path) -> None:
    empty = build_report(empty_layout(tmp_path), evidence="rehearsal")
    for name, item in recommendations(empty).items():
        assert (item.status, item.verdict) == ("insufficient", None), name


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
    assert [item["label"] for item in document["metrics"]["privacy"]["planted"]] == [
        "site password",
        "teacher api key",
    ]

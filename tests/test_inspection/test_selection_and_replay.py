"""File provenance, replay evidence and lineage remain independent selection axes."""

import json
from pathlib import Path

import pytest

from spectragent.core.archive.privacy import RedactionLedger
from spectragent.core.trace.models import (
    TaskSetupRecord,
    TraceConfig,
    TraceNegotiation,
    TraceStatus,
)
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection import open_repository
from spectragent.schemas.config_source import ConfigSource
from spectragent.schemas.restoration import BranchProvenance
from tests._helpers.trace_builder import agent_result, archive_file, step_record


def _run(root: Path, name: str, config: TraceConfig) -> WALWriter:
    wal = WALWriter(root, name)
    wal.init_run(config)
    wal.commit_step(0, step_record(0))
    wal.write_result(agent_result())
    wal.finalize(TraceStatus.COMPLETE)
    return wal


def test_file_history_filter_includes_versions_not_namesakes_or_unknown(tmp_path: Path) -> None:
    root = tmp_path / "results"
    selected = tmp_path / "one" / "a.yaml"
    other = tmp_path / "two" / "a.yaml"
    for name, source in (
        ("version-one", ConfigSource.from_path(selected, data=b"seed: 1")),
        ("version-two", ConfigSource.from_path(selected, data=b"seed: 2")),
        ("namesake", ConfigSource.from_path(other, data=b"seed: 1")),
        ("unknown", None),
    ):
        _run(root, name, TraceConfig(config_source=source, resolved={"variant": name}))
    # Source selection is historical: no current YAML is read or required.
    assert not selected.exists()
    repository = open_repository(root, config_source=selected)
    rows = repository.list_runs()
    assert {row["key"] for row in rows} == {"version-one", "version-two"}
    assert len({row["config_source"]["sha256"] for row in rows}) == 2
    assert not repository.matches("unknown")
    assert not repository.matches("namesake")
    # Navigation retains the search root, independently of list filters.
    assert repository.run("namesake")["summary"]["key"] == "namesake"
    selected_hash = rows[0]["config_hash"]
    assert (
        len(open_repository(root, config_source=selected, config_hash=selected_hash).list_runs())
        == 1
    )
    assert (
        next(row for row in open_repository(root).list_runs() if row["key"] == "unknown")[
            "config_source"
        ]
        is None
    )


def _branch() -> BranchProvenance:
    return BranchProvenance(
        source_run_id="source",
        source_trace="source",
        source_task_id="task",
        checkpoint_step=0,
        branch_index=0,
        branch_group_id="group",
    )


def test_branch_source_navigation_retains_root_across_source_filter(tmp_path: Path) -> None:
    origin = ConfigSource.from_path(tmp_path / "branch.yaml", data=b"branch")
    _run(tmp_path / "record", "source", TraceConfig(task_id="task"))
    _run(tmp_path / "branches", "child", TraceConfig(branch=_branch(), config_source=origin))
    repository = open_repository(tmp_path, config_source=origin.path)
    assert [row["key"] for row in repository.list_runs()] == ["branches/child"]
    execution = repository.run("branches/child")["execution"]
    assert execution["source_key"] == "record/source"
    assert execution["checkpoint"] == 0
    assert execution["source_resolution"] == "found"
    assert repository.step(execution["source_key"], execution["checkpoint"])["record"]["step"] == 0
    _run(tmp_path / "another", "source", TraceConfig())
    assert repository.run("branches/child")["execution"]["source_resolution"] == "ambiguous"


def test_replay_source_outside_root_is_never_opened(tmp_path: Path) -> None:
    source = _run(tmp_path / "outside", "source", TraceConfig())
    config = TraceConfig(
        task_setup=TaskSetupRecord(
            context={"replay_archive": str(source.run_dir.resolve())}, redactions=RedactionLedger()
        )
    )
    _run(tmp_path / "selected", "replay", config)
    repository = open_repository(tmp_path / "selected")
    detail = repository.run("replay")
    assert detail["summary"]["execution_kind"] == "replay"
    assert detail["execution"]["source_resolution"] == "outside_root"
    assert detail["source_key"] is None


def _decision(kind: str, **fields: object) -> dict:
    return {
        "kind": kind,
        "captured_at": 100.0,
        "cycle": 1,
        "method": "GET",
        "url": "https://fixture.invalid/resource",
        **fields,
    }


def test_replay_log_counts_folded_and_trailing_decisions_only_when_committed(
    tmp_path: Path,
) -> None:
    wal = WALWriter(tmp_path, "replay")
    wal.init_run(TraceConfig(negotiation=TraceNegotiation(active_archivers=["browser.replay_log"])))
    entries = [
        _decision("hit"),
        _decision(
            "summary", folded_kind="miss", count=7, first_captured_at=90.0, last_captured_at=100.0
        ),
        _decision(
            "substituted",
            record_id="cancelled",
            served_record_id="donor",
            source_outcome="page_cancelled_before_response",
        ),
    ]
    wal.commit_step(
        0,
        step_record(0),
        extra_files=[
            archive_file(
                "browser.replay_log",
                "replay.jsonl",
                "\n".join(json.dumps(entry) for entry in entries).encode(),
                step=0,
            )
        ],
    )
    wal.commit_trailing(
        extra_files=[
            archive_file(
                "browser.replay_log",
                "replay.jsonl",
                json.dumps(_decision("miss")).encode(),
                trailing=0,
            )
        ]
    )
    orphan = wal.run_dir / "env" / "uncommitted.jsonl"
    orphan.write_text(json.dumps(_decision("miss")))
    report = open_repository(tmp_path).run("replay")["replay_log"]
    assert report["recorded"] is True and report["error"] is None
    assert report["counts"] == {
        "hit": 1,
        "miss": 8,
        "exhausted": 0,
        "substituted": 1,
        "synthetic": 0,
    }
    assert [entry["step"] for entry in report["entries"]] == [0, 0, 0, None]
    assert report["entries"][1]["weight"] == 7


@pytest.mark.parametrize("recorded", [True, False])
def test_empty_and_unrecorded_replay_logs_are_different(tmp_path: Path, recorded: bool) -> None:
    negotiation = TraceNegotiation(active_archivers=["browser.replay_log"] if recorded else [])
    _run(tmp_path, "run", TraceConfig(negotiation=negotiation))
    report = open_repository(tmp_path).run("run")["replay_log"]
    assert report["recorded"] is recorded
    assert (report["counts"] is None) is not recorded
    assert report["entries"] == []


def test_broken_replay_archive_remains_an_explicit_error(tmp_path: Path) -> None:
    wal = WALWriter(tmp_path, "broken")
    wal.init_run(TraceConfig(negotiation=TraceNegotiation(active_archivers=["browser.replay_log"])))
    wal.commit_step(
        0,
        step_record(0),
        extra_files=[
            archive_file(
                "browser.replay_log",
                "replay.jsonl",
                b"{",
                step=0,
            )
        ],
    )
    detail = open_repository(tmp_path).run("broken")
    assert detail["steps"]
    assert detail["replay_log"]["counts"] is None
    assert detail["replay_log"]["error"]

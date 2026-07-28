"""Tests for the shared loader plumbing in ``data_loader/_common.py``.

Covers the environment-aware data-directory convention and the hard
errors that replaced the old silent ``return []`` / ``return {}`` paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectragent import BENCHMARK_DATA_ROOT
from spectragent.core.data_loader._common import (
    BenchmarkDataError,
    load_split_map,
    parse_file,
    resolve_data_dir,
    resolve_split_file,
    resolve_task_id,
)


class TestResolveDataDir:
    def test_env_type_is_the_directory_segment(self) -> None:
        assert resolve_data_dir("browser", "lexbench_browser", None) == (
            BENCHMARK_DATA_ROOT / "browser" / "lexbench_browser"
        )

    def test_non_browser_env_types_are_addressable(self) -> None:
        # The whole point of the fix: these were unreachable when the
        # "browser" segment was hardcoded.
        assert resolve_data_dir("docker", "swebench", None) == (
            BENCHMARK_DATA_ROOT / "docker" / "swebench"
        )
        assert resolve_data_dir("desktop", "osworld", None) == (
            BENCHMARK_DATA_ROOT / "desktop" / "osworld"
        )
        assert resolve_data_dir("mobile", "androidworld", None) == (
            BENCHMARK_DATA_ROOT / "mobile" / "androidworld"
        )

    def test_no_browser_segment_leaks_in(self) -> None:
        resolved = resolve_data_dir("docker", "swebench", None)
        assert "browser" not in resolved.parts

    def test_explicit_data_dir_wins(self, tmp_path: Path) -> None:
        assert resolve_data_dir("docker", "swebench", str(tmp_path)) == tmp_path


class TestLoadSplitMap:
    def test_reads_split_mapping(self, tmp_path: Path) -> None:
        (tmp_path / "data_info.json").write_text(
            json.dumps({"split": {"All": "tasks.jsonl"}})
        )
        assert load_split_map(tmp_path) == {"All": "tasks.jsonl"}

    def test_missing_data_info_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BenchmarkDataError, match="No data_info.json"):
            load_split_map(tmp_path)

    def test_empty_split_raises(self, tmp_path: Path) -> None:
        (tmp_path / "data_info.json").write_text(json.dumps({"split": {}}))
        with pytest.raises(BenchmarkDataError, match="no usable 'split'"):
            load_split_map(tmp_path)

    def test_missing_split_key_raises(self, tmp_path: Path) -> None:
        (tmp_path / "data_info.json").write_text(json.dumps({"huggingface": {}}))
        with pytest.raises(BenchmarkDataError, match="no usable 'split'"):
            load_split_map(tmp_path)


class TestResolveSplitFile:
    def test_resolves_declared_subset(self, tmp_path: Path) -> None:
        (tmp_path / "data_info.json").write_text(
            json.dumps({"split": {"All": "tasks.jsonl"}})
        )
        assert resolve_split_file(tmp_path, "All") == tmp_path / "tasks.jsonl"

    def test_unknown_subset_raises_and_lists_available(self, tmp_path: Path) -> None:
        (tmp_path / "data_info.json").write_text(
            json.dumps({"split": {"All": "tasks.jsonl", "Hard": "hard.json"}})
        )
        with pytest.raises(BenchmarkDataError) as exc:
            resolve_split_file(tmp_path, "nope")
        assert "All" in str(exc.value) and "Hard" in str(exc.value)


class TestParseFile:
    def test_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text('{"task_id": "a"}\n\n{"task_id": "b"}\n')
        assert parse_file(path) == [{"task_id": "a"}, {"task_id": "b"}]

    def test_json_list(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(json.dumps([{"task_id": "a"}]))
        assert parse_file(path) == [{"task_id": "a"}]

    def test_json_wrapper_key(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(json.dumps({"total": 1, "tasks": [{"task_id": "a"}]}))
        assert parse_file(path) == [{"task_id": "a"}]

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BenchmarkDataError, match="Data file not found"):
            parse_file(tmp_path / "absent.jsonl")

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text("")
        with pytest.raises(BenchmarkDataError, match="Parsed zero records"):
            parse_file(path)

    def test_object_without_record_list_raises(self, tmp_path: Path) -> None:
        # Previously: silently returned [] and the loader looked "empty".
        path = tmp_path / "t.json"
        path.write_text(json.dumps({"metadata": {"version": 1}}))
        with pytest.raises(BenchmarkDataError) as exc:
            parse_file(path)
        assert "no record list" in str(exc.value)
        assert "metadata" in str(exc.value)

    def test_scalar_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(json.dumps("not records"))
        with pytest.raises(BenchmarkDataError, match="expected a list of records"):
            parse_file(path)


class TestResolveTaskId:
    def test_prefers_task_id(self) -> None:
        assert resolve_task_id({"task_id": "a", "id": 1}) == "a"

    def test_falls_back_to_id_and_coerces_to_str(self) -> None:
        # tasks_private.jsonl keys on an integer ``id``.
        assert resolve_task_id({"id": 1}) == "1"

    def test_falls_back_to_instance_id(self) -> None:
        # SWE-bench's native identity key.
        assert resolve_task_id({"instance_id": "django__django-11333"}) == (
            "django__django-11333"
        )

    def test_no_identity_raises(self) -> None:
        with pytest.raises(BenchmarkDataError, match="no task identity"):
            resolve_task_id({"query": "do a thing"})

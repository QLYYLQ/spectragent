"""``--benchmark <alias|path.yaml>`` and the run root it makes (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from spectragent.benchmarks.selection import (
    BenchmarkSelectionError,
    aliases,
    is_path,
    read_benchmark,
    run_root,
)

_BLOCK = {"benchmark_id": "online_mind2web", "task_loader": {"benchmark_name": "online_mind2web"}}


def _document(tmp_path: Path, raw: object, name: str = "mine.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.mark.parametrize(
    ("selection", "path"),
    [
        ("online_mind2web", False),
        ("androidworld", False),
        ("mine.yaml", True),
        ("mine.yml", True),
        ("./mine", True),
        ("benchmarks/mine", True),
    ],
)
def test_a_selection_is_a_path_or_an_alias(selection: str, path: bool) -> None:
    assert is_path(selection) is path


def test_every_shipped_alias_reads() -> None:
    assert set(aliases()) == {"androidworld", "browsecomp", "lexbench_browser", "online_mind2web"}
    for alias in aliases():
        block = read_benchmark(alias)
        assert block["benchmark_id"] == alias
        assert block["task_loader"]["benchmark_name"] == alias


def test_an_unknown_alias_names_the_shipped_ones() -> None:
    with pytest.raises(BenchmarkSelectionError) as refused:
        read_benchmark("online_mindweb")
    assert "no shipped benchmark has this alias" in str(refused.value)
    assert "online_mind2web" in str(refused.value)


def test_a_path_reads_the_users_own(tmp_path: Path) -> None:
    path = _document(tmp_path, {"benchmark": [_BLOCK]})
    assert read_benchmark(str(path)) == _BLOCK
    with pytest.raises(BenchmarkSelectionError, match="no such file"):
        read_benchmark(str(tmp_path / "missing.yaml"))


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ({"benchmark": [_BLOCK], "agent": []}, "must hold only a 'benchmark:' section"),
        ({"task_loader": [{"benchmark_name": "online_mind2web"}]}, "must hold only"),
        ({"benchmark": [_BLOCK, _BLOCK]}, "exactly one benchmark block"),
        ({"benchmark": []}, "exactly one benchmark block"),
        ({"benchmark": _BLOCK}, "exactly one benchmark block"),
        ([_BLOCK], "must hold only"),
    ],
)
def test_a_benchmark_yaml_is_one_block_under_benchmark(
    tmp_path: Path, raw: object, problem: str
) -> None:
    with pytest.raises(BenchmarkSelectionError, match=problem):
        read_benchmark(str(_document(tmp_path, raw)))


def test_the_run_root_holds_the_run_configuration_and_the_benchmark(tmp_path: Path) -> None:
    block = read_benchmark(str(_document(tmp_path, {"benchmark": [_BLOCK]})))
    run = {"agent": [{"agent_id": "ui_tars"}]}
    assert run_root(run, block) == {**run, "benchmark": [_BLOCK]}
    assert run_root(run) == run
    assert "benchmark" not in run, "the run configuration itself is not changed"


def test_a_run_configuration_never_holds_a_benchmark(tmp_path: Path) -> None:
    block = read_benchmark(str(_document(tmp_path, {"benchmark": [_BLOCK]})))
    for selected in ((), (block,)):
        with pytest.raises(BenchmarkSelectionError, match="select it with --benchmark"):
            run_root({"agent": [], "benchmark": [_BLOCK]}, *selected)

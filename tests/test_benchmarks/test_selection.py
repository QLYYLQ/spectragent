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
    read_spec,
    run_root,
    spec_of,
)
from spectragent.schemas.benchmark_spec import BenchmarkSpec, BlockFingerprints

_LOADER = {"benchmark_name": "online_mind2web", "step_budget": 25}
_BLOCK = {"benchmark_id": "online_mind2web", "task_loader": _LOADER}


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
    assert "otherwise pass the path of a benchmark YAML" in str(refused.value)


def test_without_a_source_checkout_an_alias_says_to_pass_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An installed wheel ships no configs/benchmarks/.
    monkeypatch.setattr("spectragent.benchmarks.selection.ALIAS_DIR", tmp_path / "absent")
    with pytest.raises(BenchmarkSelectionError) as refused:
        read_benchmark("online_mind2web")
    message = str(refused.value)
    assert "the YAMLs under configs/benchmarks/ of a source checkout" in message
    assert "otherwise pass the path of a benchmark YAML" in message


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


def test_a_subset_is_written_into_the_task_loader(tmp_path: Path) -> None:
    block = read_benchmark("online_mind2web", subset="Hard")
    assert block["task_loader"]["subset"] == "Hard"
    assert read_benchmark("online_mind2web")["task_loader"]["subset"] == "All", (
        "the shipped YAML is not changed"
    )
    path = _document(tmp_path, {"benchmark": [_BLOCK]})
    assert read_benchmark(str(path), subset="Hard") == {
        **_BLOCK,
        "task_loader": {**_LOADER, "subset": "Hard"},
    }
    no_loader = _document(tmp_path, {"benchmark": [{"benchmark_id": "x"}]}, "no_loader.yaml")
    with pytest.raises(BenchmarkSelectionError, match="has no task_loader to take it"):
        read_benchmark(str(no_loader), subset="Hard")


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


def _selected(selection: str, *, subset: str | None = None) -> BenchmarkSpec:
    """The record of selecting *selection*, as ``bench`` makes it from the block it read."""
    return spec_of(selection, read_benchmark(selection, subset=subset), subset=subset)


def test_a_spec_records_an_alias_as_given_and_a_path_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So a branch sweep run from another directory finds the same file again."""
    monkeypatch.chdir(tmp_path)
    _document(tmp_path, {"benchmark": [_BLOCK]})
    alias = _selected("online_mind2web")
    assert (alias.benchmark_id, alias.selection, alias.subset) == (
        "online_mind2web",
        "online_mind2web",
        None,
    )
    spec = _selected("./mine.yaml", subset="Hard")
    assert spec.selection == str(tmp_path.resolve() / "mine.yaml")
    assert spec.describe() == f"--benchmark {spec.selection} --subset Hard"


def test_a_spec_reads_its_block_again_with_its_subset(tmp_path: Path) -> None:
    path = _document(tmp_path, {"benchmark": [_BLOCK]})
    spec = _selected(str(path), subset="Hard")
    assert read_spec(spec) == read_benchmark(str(path), subset="Hard")
    assert read_spec(spec)["task_loader"]["subset"] == "Hard"


def test_a_spec_whose_yaml_now_holds_another_benchmark_is_refused(tmp_path: Path) -> None:
    path = _document(tmp_path, {"benchmark": [_BLOCK]})
    spec = _selected(str(path)).model_copy(update={"benchmark_id": "androidworld"})
    with pytest.raises(BenchmarkSelectionError, match="'online_mind2web', not 'androidworld'"):
        read_spec(spec)


def test_a_block_whose_setup_changed_since_is_refused_naming_the_part(tmp_path: Path) -> None:
    """A branch runs its source's benchmark as the source ran it, or not at all."""
    block = {**_BLOCK, "setup": {"setup_id": "androidworld", "root": "/opt/aw"}}
    path = _document(tmp_path, {"benchmark": [block]})
    spec = _selected(str(path))
    _document(tmp_path, {"benchmark": [{**block, "setup": {**block["setup"], "root": "/srv/aw"}}]})
    with pytest.raises(BenchmarkSelectionError) as refused:
        read_spec(spec)
    message = str(refused.value)
    assert "its setup component (setup) changed since the run selected it" in message
    assert "task source" not in message and "capability" not in message


def test_a_step_budget_changed_since_is_refused_as_a_changed_task_source(tmp_path: Path) -> None:
    """``step_budget`` is part of the task loader block, so a branch's drift check sees it (§4.10.1)."""
    path = _document(tmp_path, {"benchmark": [_BLOCK]})
    spec = _selected(str(path))
    assert read_spec(spec)["task_loader"]["step_budget"] == 25
    changed = {**_BLOCK, "task_loader": {**_LOADER, "step_budget": 50}}
    _document(tmp_path, {"benchmark": [changed]})
    with pytest.raises(BenchmarkSelectionError) as refused:
        read_spec(spec)
    message = str(refused.value)
    assert "its task source (task_loader) changed since the run selected it" in message
    assert "setup" not in message and "capability" not in message


def test_a_verifier_listed_since_is_allowed(tmp_path: Path) -> None:
    """A verifier never changes a run: listing one later changes no fingerprint."""
    path = _document(tmp_path, {"benchmark": [_BLOCK]})
    spec = _selected(str(path))
    judged = {**_BLOCK, "verifiers": [{"verifier_id": "online_mind2web"}]}
    _document(tmp_path, {"benchmark": [judged]})
    assert read_spec(spec) == judged


def test_an_absent_part_and_an_empty_one_are_the_same() -> None:
    assert BlockFingerprints.of({**_BLOCK, "capability": []}) == BlockFingerprints.of(_BLOCK)


def test_a_branch_tasks_run_root_holds_its_task_source_and_its_benchmark(tmp_path: Path) -> None:
    source = _document(tmp_path, {"benchmark": [_BLOCK]})
    task_source = {
        "benchmark_id": "trace_branches",
        "task_loader": {"benchmark_name": "trace_branches"},
    }
    root = run_root({"agent": []}, task_source, read_spec(_selected(str(source))))
    assert root["benchmark"] == [task_source, _BLOCK]

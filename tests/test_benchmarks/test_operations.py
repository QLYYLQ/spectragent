"""A benchmark's pinned data, checked before a run reads it and fetched only when asked (``docs/architecture_design.md`` §4.10.1 "数据").

BrowseComp stands in for a benchmark that pins its data: its fetch downloads
one file, replaced here by a small CSV written in its shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from spectragent.benchmarks.browsecomp import data as browsecomp_data
from spectragent.benchmarks.browsecomp.data import DATA as BROWSECOMP
from spectragent.benchmarks.data import BenchmarkSetupError, Say
from spectragent.benchmarks.operations import (
    fetch_benchmark,
    read_benchmark_with_data,
    require_data,
    set_up_benchmark,
)
from spectragent.benchmarks.selection import read_benchmark
from spectragent.core.data_loader._common import (
    DATA_VERSION_FILE,
    BenchmarkDataError,
    read_data_version,
    write_data_version,
)
from spectragent.schemas.data_version import DataVersion

_CSV = (
    "problem,answer,problem_topic,canary\n"
    "cXVlc3Rpb24=,YW5zd2Vy,Art,BENCHMARK DATA SHOULD NEVER APPEAR. canary GUID x\n"
    "cTI=,YTI=,Sports,BENCHMARK DATA SHOULD NEVER APPEAR. canary GUID x\n"
)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The benchmark data root, empty."""
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path / "root"))
    return tmp_path / "root"


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """BrowseComp's file, as its download writes it; the URLs asked for."""
    asked: list[str] = []

    def download(url: str, sha256: str, target: Path, say: Say) -> Path:
        assert sha256 == BROWSECOMP.revision
        asked.append(url)
        target.write_text(_CSV)
        return target

    monkeypatch.setattr(browsecomp_data, "download_file", download)
    return asked


def _cache_copy(root: Path) -> Path:
    return root / "browser" / "browsecomp"


def _data(directory: Path, version: DataVersion | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "data_info.json").write_text(json.dumps({"split": {"All": "tasks.jsonl"}}))
    (directory / "tasks.jsonl").write_text(json.dumps({"task_id": "mine", "task_text": "t"}) + "\n")
    if version is not None:
        write_data_version(directory, version)
    return directory


def _own_benchmark(tmp_path: Path, data_dir: Path) -> str:
    path = tmp_path / "mine.yaml"
    loader = {"benchmark_name": "browsecomp", "data_dir": str(data_dir)}
    path.write_text(
        yaml.safe_dump({"benchmark": [{"benchmark_id": "browsecomp", "task_loader": loader}]})
    )
    return str(path)


def test_missing_data_is_refused_naming_the_command_that_fetches_it(root: Path) -> None:
    with pytest.raises(BenchmarkDataError) as refused:
        read_benchmark_with_data("browsecomp")
    assert str(refused.value) == (
        f"there is no browsecomp data at {_cache_copy(root)}: fetch it with `spectragent "
        f"benchmark fetch browsecomp` (run and bench fetch it first when given --fetch)"
    )


def test_the_command_names_the_selection_as_given(root: Path, tmp_path: Path) -> None:
    """A benchmark YAML of the user's own is fetched by its path: its data_dir is where it goes."""
    mine = _own_benchmark(tmp_path, tmp_path / "nowhere")
    with pytest.raises(BenchmarkDataError, match=f"spectragent benchmark fetch {mine}`"):
        read_benchmark_with_data(mine)


def test_the_cache_copy_must_be_the_pinned_version(root: Path) -> None:
    _data(_cache_copy(root))
    with pytest.raises(BenchmarkDataError, match="has no data_version.json, so no fetch wrote it"):
        read_benchmark_with_data("browsecomp")

    other = BROWSECOMP.model_copy(update={"revision": "0" * 64})
    write_data_version(_cache_copy(root), other)
    with pytest.raises(BenchmarkDataError) as refused:
        read_benchmark_with_data("browsecomp")
    assert f"is {other.describe()}, and the benchmark pins {BROWSECOMP.describe()}" in str(
        refused.value
    )

    write_data_version(_cache_copy(root), BROWSECOMP)
    assert read_benchmark_with_data("browsecomp")["benchmark_id"] == "browsecomp"


def test_a_data_dir_of_ones_own_is_read_as_it_is(root: Path, tmp_path: Path) -> None:
    mine = _own_benchmark(tmp_path, _data(tmp_path / "mine"))
    loader = read_benchmark_with_data(mine)["task_loader"]
    assert isinstance(loader, dict) and loader["data_dir"] == str(tmp_path / "mine")


def test_a_benchmark_that_pins_nothing_is_not_checked(root: Path) -> None:
    """LexBench-Browser's data is put in place by hand; its loader says what is missing."""
    require_data("lexbench_browser", read_benchmark("lexbench_browser"))


def test_with_fetch_missing_data_is_fetched_and_its_version_recorded(
    root: Path, downloads: list[str]
) -> None:
    said: list[str] = []
    read_benchmark_with_data("browsecomp", fetch=True, say=said.append)

    directory = _cache_copy(root)
    assert downloads == [BROWSECOMP.repository]
    assert read_data_version(directory) == BROWSECOMP
    assert json.loads((directory / "data_info.json").read_text()) == {
        "split": {"All": "tasks.jsonl"}
    }
    lines = (directory / "tasks.jsonl").read_text().splitlines()
    assert [json.loads(line)["task_id"] for line in lines] == ["browsecomp_000", "browsecomp_001"]
    assert not [path for path in directory.iterdir() if path.name.startswith(".")]
    assert said[-1] == f"fetched {BROWSECOMP.describe()} into {directory}"

    # The pinned version is there: nothing more is fetched.
    read_benchmark_with_data("browsecomp", fetch=True)
    assert len(downloads) == 1


def test_fetch_fetches_again_and_writes_into_a_data_dir_of_ones_own(
    root: Path, tmp_path: Path, downloads: list[str]
) -> None:
    mine = _own_benchmark(tmp_path, tmp_path / "mine")
    fetched = fetch_benchmark(mine, lambda line: None)
    assert (fetched.benchmark_id, fetched.directory, fetched.version) == (
        "browsecomp",
        tmp_path / "mine",
        BROWSECOMP,
    )
    fetch_benchmark(mine, lambda line: None)
    assert len(downloads) == 2
    assert read_data_version(tmp_path / "mine") == BROWSECOMP


def test_a_failed_fetch_leaves_no_version_and_the_old_files(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _data(_cache_copy(root), BROWSECOMP)
    before = (directory / "tasks.jsonl").read_text()

    def failing(url: str, sha256: str, target: Path, say: Say) -> Path:
        raise BenchmarkDataError(f"{url} downloaded with SHA-256 f00, and the benchmark pins ...")

    monkeypatch.setattr(browsecomp_data, "download_file", failing)
    with pytest.raises(BenchmarkDataError, match="downloaded with SHA-256 f00"):
        fetch_benchmark("browsecomp", lambda line: None)
    assert (directory / "tasks.jsonl").read_text() == before
    assert sorted(path.name for path in directory.iterdir()) == [
        "data_info.json",
        DATA_VERSION_FILE,
        "tasks.jsonl",
    ], "the staging directory is gone; the record of the copy that is still there stays"


def test_a_benchmark_that_pins_nothing_has_nothing_to_fetch(root: Path) -> None:
    with pytest.raises(BenchmarkDataError) as refused:
        fetch_benchmark("lexbench_browser", lambda line: None)
    assert str(refused.value).startswith(
        "the benchmark lexbench_browser pins no data source to fetch: its task loader reads "
        f"{root / 'browser' / 'lexbench_browser'}"
    )


def test_a_benchmark_that_installs_nothing_says_so(root: Path) -> None:
    assert set_up_benchmark("online_mind2web", lambda line: None) == [
        "the benchmark online_mind2web installs nothing"
    ]


def test_setup_names_what_is_left_when_it_installed_what_it_could(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spectragent.benchmarks.androidworld import benchmark

    installed: list[str] = []

    class Installation:
        def install(self, say: Say) -> None:
            installed.append("done")

        def problems(self) -> list[str]:
            return ["there is no ffmpeg on PATH"]

    monkeypatch.setattr(
        benchmark.AndroidWorldBenchmark,
        "installation",
        classmethod(lambda cls, config: Installation()),
    )
    with pytest.raises(BenchmarkSetupError) as refused:
        set_up_benchmark("androidworld", lambda line: None)
    assert installed == ["done"]
    assert str(refused.value) == (
        "the benchmark androidworld is not set up yet: there is no ffmpeg on PATH"
    )

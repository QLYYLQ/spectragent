"""``spectragent-export --config <yaml>`` (arch §4.15 "CLI")."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

from spectragent.export.cli import main
from tests._helpers.export_runs import REPO, exporter_entry, write_benchmark
from tests._helpers.export_traces import click_at, finished, ui_tars_step, write_ui_tars_run


def _config(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "export.yaml"
    path.write_text(yaml.safe_dump({"exporter": list(entries)}))
    return path


def test_the_entry_point_is_declared() -> None:
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["spectragent-export"] == "spectragent.export.cli:main"


def test_an_export_prints_what_it_wrote(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_ui_tars_run(
        tmp_path / "src",
        "run-1",
        [ui_tars_step(click_at(10, 10)), ui_tars_step(finished("ok"))],
    )
    out = tmp_path / "out"
    entry = exporter_entry(tmp_path / "src", out, split={"validation_fraction": 0.0})
    assert main(["--config", str(_config(tmp_path, entry))]) == 0
    printed = capsys.readouterr().out
    assert f"dataset: {out}" in printed
    assert "train: 2 sample(s)" in printed
    assert (out / "train.jsonl").is_file() and (out / "manifest.json").is_file()


def test_a_failure_exits_non_zero_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "src").mkdir()
    out = tmp_path / "out"
    assert main(["--config", str(_config(tmp_path, exporter_entry(tmp_path / "src", out)))]) == 1
    assert (
        "export failed: ExportEmptyError: the export produced no sample" in capsys.readouterr().err
    )
    assert not out.exists()


def test_exactly_one_exporter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    one = exporter_entry(tmp_path, tmp_path / "a")
    two = exporter_entry(tmp_path, tmp_path / "b")
    assert main(["--config", str(_config(tmp_path, one, two))]) == 1
    assert "must configure exactly one exporter, it configures 2" in capsys.readouterr().err
    empty = tmp_path / "none.yaml"
    empty.write_text("exporter: []\n")
    assert main(["--config", str(empty)]) == 1
    assert "it configures 0" in capsys.readouterr().err


def test_a_config_that_is_not_a_mapping(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- exporter_id: openai_messages\n")
    assert main(["--config", str(path)]) == 1
    assert "is not a YAML mapping" in capsys.readouterr().err


def test_the_documents_loader_blocks_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``eval_loader:`` / ``task_loader:`` blocks beside the exporter carry the benchmark data.

    The data is reachable only through their ``data_dir`` (the environment's
    data root is empty), so a decontaminated source proves the CLI hands the
    document's loaders to the export.
    """
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path / "empty"))
    directory = write_benchmark(
        tmp_path / "data",
        "online_mind2web",
        [{"task_id": "t-eval", "task_text": "Buy a kettle", "expected_answer": "an answer"}],
        subset="Hard",
    )
    for run_id, task_id in (("eval-task", "t-eval"), ("train-task", "t-train")):
        write_ui_tars_run(
            tmp_path / "src",
            run_id,
            [ui_tars_step(click_at(10, 10))],
            benchmark="online_mind2web",
            task_id=task_id,
        )
    out = tmp_path / "out"
    entry = exporter_entry(
        tmp_path / "src",
        out,
        decontamination={
            "against": [{"benchmark": "online_mind2web", "subset": "Hard"}],
            "match": "task_id",
        },
    )
    block = {"benchmark_name": "online_mind2web", "subset": "Hard", "data_dir": str(directory)}
    path = tmp_path / "export.yaml"
    path.write_text(
        yaml.safe_dump({"exporter": [entry], "eval_loader": [block], "task_loader": [block]})
    )
    assert main(["--config", str(path)]) == 1, "train-task has no reference: the scan fails"
    assert not out.exists()

    write_benchmark(
        tmp_path / "data",
        "online_mind2web",
        [{"task_id": "t-train", "task_text": "x", "expected_answer": "an answer"}],
        subset="All",
    )
    block_all = {**block, "subset": "All"}
    path.write_text(
        yaml.safe_dump(
            {"exporter": [entry], "eval_loader": [block, block_all], "task_loader": [block]}
        )
    )
    assert main(["--config", str(path)]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["sources"]["filtered"] == {
        "eval-task": "decontaminated: task 't-eval' is an evaluation task of online_mind2web/Hard"
    }
    assert manifest["leak_scan"] == {
        "compared": 1,
        "no_eval_loader": {},
        "no_reference_answer": {},
    }

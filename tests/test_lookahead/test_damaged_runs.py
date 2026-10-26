"""One run whose evidence does not read back never stops a command, nor leaves a partial file."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lookahead import extract
from lookahead.cli import run
from lookahead.runs import open_runs
from spectragent.core.llm.messages import BaseMessage
from tests.test_lookahead.conftest import World
from tests.test_lookahead.fake_judge import ScriptedChatModel, answers, loader, prompt_text


@pytest.fixture
def damaged(world: World, tmp_path: Path) -> Path:
    """A copy of the runs in which rec-1's step-1 screenshot is gone and rec-2's frames index is torn."""
    root = tmp_path / "traces"
    shutil.copytree(world.root, root)
    (root / "record" / "rec-1" / "observations" / "001_browser.screenshot.png").unlink()
    return root


def _skipped(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().err


def test_extract_skips_the_damaged_run_and_writes_the_rest(
    damaged: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "rows.jsonl"
    assert run(["extract", "--root", str(damaged), "--out", str(out)]) == 0
    runs = {json.loads(line)["run"] for line in out.read_text(encoding="utf-8").splitlines()}
    assert "record/rec-1" not in runs and "record/rec-2" in runs
    err = _skipped(capsys)
    assert "skipped record/rec-1: not a readable trace" in err
    assert "observations/001_browser.screenshot.png does not read back" in err


def test_stability_and_gold_skip_the_damaged_run(
    damaged: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["stability", "--root", str(damaged), "--out", str(tmp_path / "s")]) == 0
    report = json.loads((tmp_path / "s" / "stability.json").read_text(encoding="utf-8"))
    assert "record/rec-1" in report["skipped_runs"]
    observed = {item["run"] for item in report["observations"]}
    assert "record/rec-1" not in observed and {"record/rec-2", "replays/replay-1"} <= observed
    assert run(["gold-queue", "--root", str(damaged), "--out", str(tmp_path / "g")]) == 0
    sampling = json.loads((tmp_path / "g" / "sampling.json").read_text(encoding="utf-8"))
    assert "record/rec-1" in sampling["skipped_runs"] and "record/rec-1" not in sampling["by_run"]


def test_forks_of_a_damaged_source_are_unpaired_with_the_reason(
    damaged: Path, tmp_path: Path
) -> None:
    def script(messages: list[BaseMessage]) -> str:
        if "Two runs of an agent" in prompt_text(messages):
            return answers(same_intent="yes")
        return answers(reasonable="yes", executed_as_intended="yes")

    out = tmp_path / "forks"
    code = run(
        ["judge-forks", "--root", str(damaged), "--out", str(out), "--judge-config", "j.yaml"],
        load_judge=loader(ScriptedChatModel(script)),
    )
    assert code == 0
    records = {
        record["branch_run"]: record
        for record in map(
            json.loads, (out / "judgments.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    same = records["branches/br-same"]
    assert same["status"] == "unpaired"
    assert same["status_reason"].startswith("its source run does not read back")


def test_a_failure_while_writing_leaves_the_previous_file_whole(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "rows.jsonl"
    out.write_text("previous rows\n", encoding="utf-8")
    calls = {"runs": 0}
    real = extract.step_rows

    def failing(run_, runs):  # a programming error, not a damaged trace: it propagates
        calls["runs"] += 1
        if calls["runs"] == 3:
            raise RuntimeError("boom")
        return real(run_, runs)

    monkeypatch.setattr(extract, "step_rows", failing)
    with pytest.raises(RuntimeError, match="boom"):
        extract.write_rows(open_runs(world.root), out)
    assert out.read_text(encoding="utf-8") == "previous rows\n"
    assert [path.name for path in tmp_path.iterdir()] == ["rows.jsonl"]  # no temporary left

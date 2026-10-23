"""``variants``: alternative tasks from recorded screens, written as ``task-variants-1``."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from lookahead.cli import run
from lookahead.judge import Judge
from lookahead.runs import open_runs
from lookahead.variants import (
    calls_path,
    candidate_steps,
    generate,
    reply_model_for,
    spread,
    writing_system,
)
from spectragent.core.data_loader.task.task_variants import read_task_variants
from tests.test_lookahead.conftest import TASK, World
from tests.test_lookahead.fake_judge import ScriptedTransport, images, prompt_text


def _variants(count: int, *, chinese: bool = True) -> str:
    def task(index: int) -> str:
        return (
            f"在商店找到第{index}种商品的价格。" if chinese else f"Find the price of item {index}."
        )

    return json.dumps(
        {
            "variants": [
                {"task_text": task(index), "answer_location": "the item's page"}
                for index in range(1, count + 1)
            ]
        },
        ensure_ascii=False,
    )


def _count(request: dict) -> int:
    match = re.search(r"Write exactly (\d+) new tasks", prompt_text(request))
    assert match is not None
    return int(match.group(1))


def _answer(request: dict) -> str:
    """As many variants as asked for, in the language the prompt names."""
    if "written in Chinese" in prompt_text(request):
        return _variants(_count(request))
    return _variants(_count(request), chinese=False)


def test_spread_takes_the_middle_of_equal_stretches() -> None:
    assert spread([0, 1, 2, 3, 4, 5, 6, 7], 2) == [2, 6]
    assert spread([0, 1, 2, 3, 4], 1) == [2]
    assert spread([3, 4], 5) == [3, 4]
    assert spread([], 2) == []


def test_checkpoints_are_policy_steps_a_branch_can_start_from(world: World) -> None:
    runs = {run.key: run for run in open_runs(world.root).runs}
    assert candidate_steps(runs["record/rec-1"]) == [0, 1, 2, 3]
    assert candidate_steps(runs["record/rec-2"]) == [0, 1]


def test_reply_validation_counts_distinct_tasks_in_the_source_language() -> None:
    model = reply_model_for(2, TASK)
    assert len(model.model_validate_json(_variants(2)).variants) == 2
    with pytest.raises(ValidationError, match="exactly 2"):
        model.model_validate_json(_variants(3))
    english = json.dumps(
        {
            "variants": [
                {"task_text": "Find the price of item one.", "answer_location": "x"},
                {"task_text": "Find the price of item two.", "answer_location": "x"},
            ]
        }
    )
    with pytest.raises(ValidationError, match="language"):
        model.model_validate_json(english)
    same = json.dumps(
        {
            "variants": [
                {"task_text": TASK, "answer_location": "x"},
                {"task_text": "在商店找到A。", "answer_location": "x"},
            ]
        },
        ensure_ascii=False,
    )
    with pytest.raises(ValidationError, match="repeats the original"):
        model.model_validate_json(same)
    assert writing_system("Find the headline") == "other"
    assert writing_system("在B站搜索") == "chinese"
    assert writing_system("在B站搜索・排序") == "chinese"  # the middle dot is not kana
    assert writing_system("動画を検索") == "japanese"
    assert writing_system("검색") == "korean"


def test_variants_file_reads_back_through_the_branch_loader(world: World, tmp_path: Path) -> None:
    out = tmp_path / "variants.json"
    transport = ScriptedTransport(_answer)
    judge = Judge(transport, log_path=calls_path(out))
    summary = asyncio.run(generate(open_runs(world.root), judge, out, steps_per_run=2, per_step=3))
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["format"] == "task-variants-1"
    variants = document["variants"]
    assert {(item["source_run_id"], item["step"]) for item in variants} == {
        ("rec-1", 1),
        ("rec-1", 3),
        ("rec-2", 0),
        ("rec-2", 1),
    }
    assert {item["variant_id"] for item in variants} == {"1", "2", "3"}
    for item in variants:
        assert set(item) == {"source_run_id", "step", "variant_id", "task_text", "note"}
        assert "answer location" in item["note"]
    assert summary["checkpoints"] == 4 and summary["variants"] == 12
    entries = read_task_variants(out)
    assert [entry.key for entry in entries][:3] == ["rec-1/s1/v1", "rec-1/s1/v2", "rec-1/s1/v3"]
    assert set(summary["skipped_runs"]) == {
        "branches/br-ended",
        "branches/br-offpage",
        "branches/br-same",
        "branches/br-typed",
        "branches/br-unparsed",
        "branches/br-variant",
        "replays/replay-1",
    }
    assert all(images(request) for request in transport.requests)
    assert "Original task: " + TASK in prompt_text(transport.requests[0])
    assert "Chinese" in prompt_text(transport.requests[0])
    log = calls_path(out).read_text(encoding="utf-8").splitlines()
    assert len(log) == len(transport.requests)


def test_a_checkpoint_whose_replies_never_validate_gives_no_variants(
    world: World, tmp_path: Path
) -> None:
    out = tmp_path / "variants.json"
    judge = Judge(ScriptedTransport(lambda request: _variants(1)))
    root = open_runs(world.root / "record", world.root)
    summary = asyncio.run(generate(root, judge, out, steps_per_run=1, per_step=2))
    assert summary["checkpoints_without_variants"] == summary["checkpoints"] == 2
    assert json.loads(out.read_text(encoding="utf-8"))["variants"] == []
    assert judge.totals.calls == 6  # one attempt and two retries each


def test_cli_prints_totals_and_closes_the_transport(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = ScriptedTransport(_answer)
    out = tmp_path / "variants.json"
    code = run(
        [
            "variants",
            "--root",
            str(world.root),
            "--steps-per-run",
            "1",
            "--per-step",
            "2",
            "--out",
            str(out),
            "--concurrency",
            "2",
        ],
        transport_factory=lambda: transport,
    )
    assert code == 0 and transport.closed
    printed = capsys.readouterr().out
    assert "4 variant(s) from 2 checkpoint(s)" in printed and "judge: 2 request(s)" in printed
    assert calls_path(out).is_file()


def test_an_excluded_recording_gets_no_variants(world: World, tmp_path: Path) -> None:
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text(
        json.dumps(
            {
                "format": "run-exclusions-1",
                "runs": [{"run_id": "rec-1", "reason": "never rendered"}],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "variants.json"
    transport = ScriptedTransport(_answer)
    runs = open_runs(world.root, exclude_runs=exclusions)
    summary = asyncio.run(generate(runs, Judge(transport), out, steps_per_run=2, per_step=3))
    assert {item.source_run_id for item in read_task_variants(out)} == {"rec-2"}
    assert (
        summary["excluded_runs"]["record/rec-1"] == "the exclusions file lists it: never rendered"
    )
    assert "record/rec-1" not in summary["skipped_runs"]
    assert all("rec-1" not in str(request) for request in transport.requests)

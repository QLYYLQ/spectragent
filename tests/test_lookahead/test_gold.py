"""``gold-queue``: items for human labels, sampled across sites, action types and runs."""

from __future__ import annotations

import json
from pathlib import Path

from lookahead.cli import run
from lookahead.gold import candidates, gold_queue, main_action, sample
from lookahead.runs import open_runs
from tests.test_lookahead.conftest import World


def test_candidates_are_steps_the_agent_acted_after(world: World) -> None:
    pool = {(run.key, row.step) for run, row in candidates(open_runs(world.root))}
    assert ("record/rec-1", 0) in pool and ("record/rec-1", 2) in pool
    assert ("record/rec-1", 3) not in pool  # it finished the task: nothing follows
    assert ("record/rec-2", 0) in pool  # the next step acted, even though its click failed
    assert ("record/rec-2", 1) not in pool  # the run ended in an error: no next screen
    assert ("branches/br-same", 0) not in pool  # restored, not decided by the agent
    assert ("branches/br-same", 1) not in pool  # its next screen is a terminal observation
    assert not any(key == "replays/replay-1" for key, _ in pool)  # its next step did not act


def test_sampling_spreads_over_sites_actions_and_runs(world: World) -> None:
    pool = candidates(open_runs(world.root))
    chosen = sample(pool, 3, seed=0)
    groups = {(row.site, main_action(row)) for _, row in chosen}
    assert len(groups) == 3
    assert len(sample(pool, 100, seed=0)) == len(pool)
    assert [row.step for _, row in sample(pool, 5, seed=3)] == [
        row.step for _, row in sample(pool, 5, seed=3)
    ]


def test_items_show_both_screens_and_actions_without_a_suggestion(
    world: World, tmp_path: Path
) -> None:
    summary, path = gold_queue(
        open_runs(world.root),
        tmp_path / "gold",
        annotations=tmp_path / "ann",
        queue_id="gold-test",
        count=100,
        seed=0,
    )
    queue = json.loads(path.read_text(encoding="utf-8"))
    assert path == tmp_path / "ann" / "queues" / "gold-test.json"
    assert [kind["value"] for kind in queue["label"]["regions"]["kinds"]] == ["evidence"]
    assert queue["label"]["regions"]["kinds"][0]["text"] == "信息所在位置"
    rules = {choice["value"]: choice["regions"] for choice in queue["label"]["choices"]}
    assert rules == {
        "result_list": "required",
        "confirmation": "required",
        "detail_fields": "required",
        "navigation_target": "required",
        "nothing_new": "forbidden",
        "other": "optional",
    }
    assert all(choice["help"] for choice in queue["label"]["choices"])
    assert queue["guide"] and "跳过" in queue["instructions"]
    items = {item["item_id"]: item for item in queue["items"]}
    assert "record/rec-1@3" not in items  # a step that finished the task
    first = items["record/rec-1@0"]
    assert first["title"] == "shop.example · 任务 rec-1 · 第 0 步"
    images = {image["role"]: image for image in first["images"]}
    assert images["current"]["source"] == {"kind": "observation", "run": "record/rec-1", "step": 0}
    assert images["next"]["source"] == {"kind": "observation", "run": "record/rec-1", "step": 1}
    # the current screen draws this step's actions, the next screen the next step's
    assert images["current"]["marks_from"] == [
        {"run": "record/rec-1", "step": 0, "label": "这一步的动作"}
    ]
    assert images["next"]["marks_from"] == [
        {"run": "record/rec-1", "step": 1, "label": "下一步的动作"}
    ]
    assert images["before_action"]["marks_from"] is None  # the action the frame preceded
    before = images["before_action"]["source"]
    assert before["kind"] == "archive" and before["step"] == 0
    assert all(item["suggestion"] is None for item in queue["items"])
    fields = {field["text"]: field for field in first["fields"]}
    assert fields["这一步的动作（画在“当前屏幕”上）"]["value"].startswith(
        "1. 点击 (20, 10) → 输入框“Search”\n2. 等待 1 秒"
    )
    assert fields["下一步的动作（画在“下一屏”上）"]["value"] == "1. 点击 (100, 10) → 按钮“Search”"
    assert fields["下一步：智能体的回复（原文）"]["value"] == "Thought: run the search."
    assert fields["下一屏网址"] == {
        "text": "下一屏网址",
        "value": "https://shop.example/search",
        "technical": True,
    }
    assert not fields["任务"]["technical"]
    assert "before_action" not in {image["role"] for image in items["record/rec-2@0"]["images"]}
    assert summary["items"] == len(items)
    sampling = json.loads((tmp_path / "gold" / "sampling.json").read_text(encoding="utf-8"))
    assert sampling["by_site"]["shop.example"] >= 1 and sampling["by_site"]["news.example"] == 1


def test_cli_writes_the_queue_under_the_output_by_default(world: World, tmp_path: Path) -> None:
    out = tmp_path / "gold"
    code = run(
        ["gold-queue", "--root", str(world.root), "--n", "3", "--out", str(out), "--queue-id", "g"]
    )
    assert code == 0
    assert len(json.loads((out / "queues" / "g.json").read_text(encoding="utf-8"))["items"]) == 3

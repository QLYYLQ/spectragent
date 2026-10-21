"""``gold-queue``: items for human labels, sampled across sites, action types and runs."""

from __future__ import annotations

import json
from pathlib import Path

from lookahead.cli import run
from lookahead.gold import candidates, gold_queue, main_action, sample
from lookahead.runs import open_runs
from tests.test_lookahead.conftest import World


def test_candidates_are_policy_steps_with_both_screens(world: World) -> None:
    pool = {(run.key, row.step) for run, row in candidates(open_runs(world.root))}
    assert ("record/rec-1", 3) in pool  # its next screen is the terminal observation
    assert ("record/rec-2", 1) not in pool  # the run ended in an error: no next screen
    assert ("branches/br-same", 0) not in pool  # restored, not decided by the agent
    assert ("branches/br-same", 1) in pool


def test_sampling_spreads_over_sites_actions_and_runs(world: World) -> None:
    pool = candidates(open_runs(world.root))
    chosen = sample(pool, 4, seed=0)
    groups = {(row.site, main_action(row)) for _, row in chosen}
    assert len(groups) == 4
    assert len(sample(pool, 100, seed=0)) == len(pool)
    assert [row.step for _, row in sample(pool, 5, seed=3)] == [
        row.step for _, row in sample(pool, 5, seed=3)
    ]


def test_items_show_both_screens_actions_and_change_hints(world: World, tmp_path: Path) -> None:
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
    assert queue["label"]["regions"]["kinds"] == [{"value": "evidence", "text": "Evidence"}]
    assert [choice["value"] for choice in queue["label"]["choices"]] == [
        "result_list",
        "confirmation",
        "detail_fields",
        "navigation_target",
        "nothing_new",
        "other",
    ]
    items = {item["item_id"]: item for item in queue["items"]}
    first = items["record/rec-1@0"]
    roles = {image["role"]: image["source"] for image in first["images"]}
    assert roles["current"] == {"kind": "observation", "run": "record/rec-1", "step": 0}
    assert roles["next"] == {"kind": "observation", "run": "record/rec-1", "step": 1}
    assert roles["before_action"]["kind"] == "archive" and roles["before_action"]["step"] == 0
    suggestion = first["suggestion"]
    assert suggestion["choice"] is None and suggestion["source"] == "pixel-diff"
    assert suggestion["regions"] == [{"image": "next", "kind": "evidence", "box": [64, 0, 128, 32]}]
    fields = {field["text"]: field["value"] for field in first["fields"]}
    assert fields["URL after"] == "https://shop.example/search"
    assert fields["Actions"].startswith("1. browser.click_coordinate")
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

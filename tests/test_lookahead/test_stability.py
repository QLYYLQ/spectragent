"""``stability``: the observation against its delayed and before-action frames, by the grid."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lookahead import imaging
from lookahead.cli import run
from lookahead.runs import open_runs
from lookahead.stability import (
    DEFAULT_MIN_CHANGE_AREA,
    DEFAULT_ORIGINS,
    ObservationStability,
    analyse,
    build_queue,
    playing_media,
    verdict,
)
from spectragent.annotation import ArchiveImage
from spectragent.flywheel.diagnostics import DifferenceMethod
from tests.test_lookahead.conftest import BASE, LOCAL, World
from tests.test_lookahead.traces import gradient, playing_video, with_box


@pytest.mark.parametrize(
    ("delayed", "before", "expected"),
    [
        (None, None, "no_frames"),
        (False, False, "stable_through_think"),
        (None, False, "stable_through_think"),
        (False, None, "stable_short"),
        (False, True, "stable_short"),
        (True, False, "changed_short"),
        (True, None, "changed_short"),
        (None, True, "changed_before_action"),
    ],
)
def test_verdicts(delayed: bool | None, before: bool | None, expected: str) -> None:
    assert verdict(delayed, before) == expected


def test_distance_is_the_restorers_dhash() -> None:
    assert imaging.distance(BASE, BASE) == 0
    assert imaging.distance(BASE, gradient(descending=False)) == 64
    assert imaging.distance(b"not an image", BASE) is None


def test_changed_regions_report_their_cells_and_area() -> None:
    changed = with_box(BASE, (64, 0, 128, 32), (255, 0, 0))
    diff = imaging.difference(BASE, changed)
    assert diff.compared and diff.boxes == [[64, 0, 128, 32]]
    (found,) = diff.regions
    assert (found.cells, found.area) == (2, 2048) and found.fraction == pytest.approx(2048 / 12288)
    assert diff.changed_pixels == 64 * 32 and diff.changed_fraction == pytest.approx(2048 / 12288)
    two = with_box(with_box(BASE, (0, 0, 32, 32), (0, 0, 0)), (64, 64, 96, 96), (0, 0, 0))
    assert imaging.difference(BASE, two).boxes == [[0, 0, 32, 32], [64, 64, 96, 96]]


def test_the_grid_sees_a_local_change_the_dhash_misses() -> None:
    distance = imaging.distance(BASE, LOCAL)
    assert distance is not None and distance <= 5
    diff = imaging.difference(BASE, LOCAL)
    assert diff.significant(0.005) and diff.changed_fraction == pytest.approx(512 / 12288)


def test_images_of_different_sizes_are_not_resized() -> None:
    diff = imaging.difference(BASE, gradient(size=(64, 48)))
    assert not diff.compared and diff.reason is not None and "not resized" in diff.reason


def test_cell_regions_are_clipped_to_the_image() -> None:
    method = DifferenceMethod()
    assert (method.grid_size, method.pixel_threshold) == (32, 16)
    regions = imaging.cell_regions({(0, 0), (1, 0), (3, 2)}, (100, 90), 32)
    assert [found.box for found in regions] == [[0, 0, 64, 32], [96, 64, 100, 90]]
    assert regions[1].area == 4 * 26
    assert imaging.clip([-10, 5, 300, 40.6], (128, 96)) == [0, 5, 128, 41]
    assert imaging.clip([130, 0, 200, 10], (128, 96)) is None


def test_playing_media_boxes_are_clipped_to_the_image() -> None:
    from spectragent.core.capabilities.browser.observations import ScreenshotObs

    shot = ScreenshotObs(
        viewport_width=128,
        viewport_height=96,
        image_width=128,
        image_height=96,
        media=playing_video((100, -20, 200, 50)),
    )
    labels, boxes = playing_media(shot, (128, 96))
    assert labels == ["main#0 video"] and boxes == [[100, 0, 128, 50]]


def _by_step(
    world: World,
    *,
    min_change_area: float = DEFAULT_MIN_CHANGE_AREA,
    origins: frozenset[str] = DEFAULT_ORIGINS,
) -> dict[tuple[str, int], ObservationStability]:
    items = analyse(
        open_runs(world.root),
        method=DifferenceMethod(),
        min_change_area=min_change_area,
        origins=origins,
    )
    return {(item.run, item.step): item for item in items}


def test_each_frame_situation_gets_its_verdict(world: World) -> None:
    items = _by_step(world)
    first = items[("record/rec-1", 0)]
    assert first.verdict == "stable_through_think"
    assert first.delayed is not None and first.before_action is not None
    assert first.delayed.changed is False and first.delayed.regions == []
    second = items[("record/rec-1", 1)]
    assert second.verdict == "changed_short"
    assert second.delayed is not None and second.delayed.changed is True
    assert second.delayed.batch_step == 2
    assert second.delayed.changed_fraction is not None and second.delayed.changed_fraction > 0.3
    third = items[("record/rec-1", 2)]
    assert third.verdict == "changed_before_action"
    assert third.delayed is not None and third.before_action is not None
    assert third.delayed.outcome == "superseded" and third.delayed.changed is None
    assert third.before_action.changed is True
    assert items[("record/rec-1", 3)].verdict == "stable_short"
    assert items[("record/rec-2", 0)].verdict == "no_frames"


def test_a_local_change_is_a_change_whatever_the_dhash_says(world: World) -> None:
    item = _by_step(world)[("replays/replay-1", 0)]
    assert item.verdict == "changed_short"
    assert item.delayed is not None
    assert item.delayed.dhash_distance is not None and item.delayed.dhash_distance <= 5
    assert [found.box for found in item.delayed.regions] == [[32, 32, 96, 64]]
    assert item.delayed.regions[0].significant
    # a minimum area above the region's turns it into noise
    lenient = _by_step(world, min_change_area=0.2)[("replays/replay-1", 0)]
    assert lenient.verdict == "stable_through_think"
    assert lenient.delayed is not None and lenient.delayed.regions[0].significant is False


def test_think_time_comes_from_the_first_action_span(world: World) -> None:
    items = _by_step(world)
    assert items[("record/rec-1", 0)].think_s == pytest.approx(1.0)
    assert items[("record/rec-1", 1)].think_s is None  # spans measured: no env.act call


def test_policy_steps_only_unless_asked(world: World) -> None:
    default = _by_step(world)
    assert {item.origin for item in default.values()} == {"policy"}
    assert ("branches/br-same", 0) not in default
    both = _by_step(world, origins=frozenset({"policy", "restored"}))
    assert both[("branches/br-same", 0)].origin == "restored"


def test_changes_over_playing_media_are_flagged_separately(world: World) -> None:
    items = _by_step(world)
    second = items[("record/rec-1", 1)]
    assert second.media_playing == ["main#0 video"]
    assert second.media_boxes == [[64, 0, 128, 96]]
    assert second.media_change is True
    assert items[("record/rec-1", 2)].media_change is False


def test_previous_action_is_the_last_action_of_the_step_before(world: World) -> None:
    items = _by_step(world)
    assert items[("record/rec-1", 0)].previous_action == "(start)"
    assert items[("record/rec-1", 1)].previous_action == "browser.wait"
    assert items[("record/rec-1", 3)].previous_action == "browser.scroll"


def test_queue_is_stratified_and_suggests_the_regions_over_the_minimum(world: World) -> None:
    items = analyse(open_runs(world.root), method=DifferenceMethod())
    queue = build_queue(items, queue_id="stability-test", size=60, seed=0)
    by_id = {item.item_id: item for item in queue.items}
    assert "record/rec-2@0" not in by_id  # no frames: nothing to compare
    changed = by_id["record/rec-1@1"]
    assert changed.suggestion is not None
    assert changed.suggestion.choice == "animation_or_video"
    assert {found.image for found in changed.suggestion.regions or []} == {"delayed"}
    roles = {image.role: image.source for image in changed.images}
    delayed = roles["delayed"]
    assert isinstance(delayed, ArchiveImage)
    assert delayed.step == 2  # the batch that took it
    assert (delayed.archiver, delayed.name) == ("browser.frames", "frame-2-after.png")
    local = by_id["replays/replay-1@0"].suggestion
    assert local is not None and local.choice == "other_change"
    assert [found.box for found in local.regions or []] == [[32, 32, 96, 64], [32, 32, 96, 64]]
    before = by_id["record/rec-1@2"]
    assert before.suggestion is not None and before.suggestion.choice == "other_change"
    assert "delayed" not in {image.role for image in before.images}  # superseded: no image
    stable = by_id["record/rec-1@0"].suggestion
    assert stable is not None and stable.choice == "stable" and stable.regions == []
    small = build_queue(items, queue_id="stability-test", size=2, seed=0)
    assert len(small.items) == 2
    assert small.items[0].fields[0].text == "机器判断"
    assert len({item.fields[0].value for item in small.items}) == 2  # two different groups
    # a region under the minimum area is noise: neither a change nor a suggested box
    lenient = analyse(open_runs(world.root), method=DifferenceMethod(), min_change_area=0.2)
    quiet = {
        item.item_id: item
        for item in build_queue(lenient, queue_id="q", size=60, seed=0, min_change_area=0.2).items
    }["replays/replay-1@0"].suggestion
    assert quiet is not None and quiet.choice == "stable" and quiet.regions == []


def test_queue_items_read_in_chinese(world: World) -> None:
    runs = open_runs(world.root)
    items = analyse(runs, method=DifferenceMethod())
    queue = build_queue(
        items, queue_id="q", size=60, seed=0, runs={run.key: run for run in runs.runs}
    )
    assert {choice.value: choice.regions for choice in queue.label.choices}["stable"] == "forbidden"
    assert queue.guide and queue.suggestion_display.title.startswith("机器建议")
    item = queue.item("record/rec-1@1")
    assert item is not None and item.title == "shop.example · 任务 rec-1 · 第 1 步"
    assert [image.text for image in item.images] == ["智能体看到的", "约 1 秒后", "第一个动作之前"]
    fields = {field.text: field for field in item.fields}
    assert fields["上一步的最后一个动作"].value == "等待 1 秒"
    assert fields["这一步的动作"].value == "1. 点击 (100, 10) → 按钮“Search”"
    assert str(fields["截图前等页面稳定"].value).startswith("等到上限仍未稳定（等了 3.0 秒）")
    assert fields["网址"].technical and not fields["机器判断"].technical
    assert "dHash" in str(fields["约 1 秒后（细节）"].value)
    assert "dHash" not in str(fields["约 1 秒后"].value)


def test_cli_writes_report_json_and_queue(world: World, tmp_path: Path) -> None:
    out = tmp_path / "stability"
    annotations = tmp_path / "annotations"
    code = run(
        [
            "stability",
            "--root",
            str(world.root),
            "--out",
            str(out),
            "--annotations",
            str(annotations),
            "--queue-id",
            "stability-test",
            "--origins",
            "policy,restored",
            "--min-change-area",
            "0.01",
        ]
    )
    assert code == 0
    report = json.loads((out / "stability.json").read_text(encoding="utf-8"))
    assert report["format"] == "lookahead-stability-3"
    assert report["origins"] == ["policy", "restored"] and report["min_change_area"] == 0.01
    assert report["summary"]["changed_short"] >= 2
    text = (out / "report.md").read_text(encoding="utf-8")
    for heading in (
        "## Settle outcome × verdict",
        "## Last action of the previous step",
        "## Step origin × verdict",
        "## What the dHash alone would say",
    ):
        assert heading in text
    assert "the grid found 2 changed at a dHash distance of 5 or less" in text
    queue = json.loads((annotations / "queues" / "stability-test.json").read_text(encoding="utf-8"))
    assert queue["format"] == "review-queue-1"
    assert queue["label"]["regions"]["kinds"][0]["value"] == "change"


def test_cli_refuses_an_unknown_origin(world: World, tmp_path: Path) -> None:
    code = run(
        [
            "stability",
            "--root",
            str(world.root),
            "--out",
            str(tmp_path / "s"),
            "--origins",
            "terminal",
        ]
    )
    assert code == 2
    assert not (tmp_path / "s" / "stability.json").exists()

"""``stability``: the observation against its delayed and before-action frames."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lookahead import imaging
from lookahead.cli import run
from lookahead.runs import open_runs
from lookahead.stability import ObservationStability, analyse, build_queue, verdict
from spectragent.flywheel.diagnostics import DifferenceMethod
from tests.test_lookahead.conftest import BASE, LATER, SEARCHED, WHITE_LEFT, World
from tests.test_lookahead.traces import gradient, with_box


@pytest.mark.parametrize(
    ("delayed", "before", "expected"),
    [
        (None, None, "no_frames"),
        (0, 0, "stable_through_think"),
        (None, 3, "stable_through_think"),
        (2, None, "stable_short"),
        (2, 20, "stable_short"),
        (20, 0, "changed_short"),
        (20, None, "changed_short"),
        (None, 20, "changed_before_action"),
        (5, 5, "stable_through_think"),
        (6, None, "changed_short"),
    ],
)
def test_verdicts(delayed: int | None, before: int | None, expected: str) -> None:
    assert verdict(delayed, before, 5) == expected


def test_distance_is_the_restorers_dhash() -> None:
    assert imaging.distance(BASE, BASE) == 0
    assert imaging.distance(BASE, gradient(descending=False)) == 64
    assert imaging.distance(b"not an image", BASE) is None


def test_changed_regions_merge_four_neighbour_cells() -> None:
    changed = with_box(BASE, (64, 0, 128, 32), (255, 0, 0))
    diff = imaging.difference(BASE, changed)
    assert diff.compared and diff.boxes == [[64, 0, 128, 32]]
    assert diff.changed_pixels == 64 * 32 and diff.total_pixels == 128 * 96
    two = with_box(with_box(BASE, (0, 0, 32, 32), (0, 0, 0)), (64, 64, 96, 96), (0, 0, 0))
    assert imaging.difference(BASE, two).boxes == [[0, 0, 32, 32], [64, 64, 96, 96]]


def test_images_of_different_sizes_are_not_resized() -> None:
    diff = imaging.difference(BASE, gradient(size=(64, 48)))
    assert not diff.compared and diff.reason is not None and "not resized" in diff.reason


def test_cell_boxes_match_the_flywheel_grid() -> None:
    method = DifferenceMethod()
    assert (method.grid_size, method.pixel_threshold) == (32, 16)
    boxes = imaging.cell_boxes({(0, 0), (1, 0), (3, 2)}, (100, 90), 32)
    assert boxes == [[0, 0, 64, 32], [96, 64, 100, 90]]


def _by_step(world: World) -> dict[tuple[str, int], ObservationStability]:
    items = analyse(open_runs(world.root), limit=5, method=DifferenceMethod())
    return {(item.run, item.step): item for item in items}


def test_each_frame_situation_gets_its_verdict(world: World) -> None:
    items = _by_step(world)
    first = items[("record/rec-1", 0)]
    assert first.verdict == "stable_through_think"
    assert first.delayed is not None and first.before_action is not None
    assert first.delayed.distance == 0 and first.before_action.distance == 0
    second = items[("record/rec-1", 1)]
    assert second.verdict == "changed_short"
    assert second.delayed is not None
    assert second.delayed.distance == imaging.distance(SEARCHED, LATER)
    assert second.delayed.batch_step == 2
    third = items[("record/rec-1", 2)]
    assert third.verdict == "changed_before_action"
    assert third.delayed is not None and third.before_action is not None
    assert third.delayed.outcome == "superseded" and third.delayed.distance is None
    assert third.before_action.distance == imaging.distance(BASE, WHITE_LEFT)
    assert items[("record/rec-1", 3)].verdict == "stable_short"
    assert items[("record/rec-2", 0)].verdict == "no_frames"


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


def test_queue_is_stratified_and_suggests_the_machine_verdict(world: World) -> None:
    items = analyse(open_runs(world.root), limit=5, method=DifferenceMethod())
    queue = build_queue(items, queue_id="stability-test", size=60, seed=0, limit=5)
    assert all(item.suggestion is not None for item in queue.items)
    by_id = {item.item_id: item for item in queue.items}
    assert "record/rec-2@0" not in by_id  # no frames: nothing to compare
    changed = by_id["record/rec-1@1"]
    assert changed.suggestion is not None
    assert changed.suggestion.choice == "animation_or_video"
    assert {region.image for region in changed.suggestion.regions} == {"delayed"}
    roles = {image.role: image.source for image in changed.images}
    assert roles["observation"].kind == "observation"
    delayed = roles["delayed"]
    assert delayed.kind == "archive" and delayed.step == 2  # the batch that took it
    assert delayed.archiver == "browser.frames"  # type: ignore[union-attr]
    before = by_id["record/rec-1@2"]
    assert before.suggestion is not None and before.suggestion.choice == "other_change"
    assert "delayed" not in {image.role for image in before.images}  # superseded: no image
    assert by_id["record/rec-1@0"].suggestion.choice == "stable"  # type: ignore[union-attr]
    small = build_queue(items, queue_id="stability-test", size=2, seed=0, limit=5)
    assert len(small.items) == 2
    assert len({item.fields[1].value for item in small.items}) == 2  # two different groups


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
        ]
    )
    assert code == 0
    report = json.loads((out / "stability.json").read_text(encoding="utf-8"))
    assert report["format"] == "lookahead-stability-1"
    assert report["summary"]["changed_short"] >= 1
    text = (out / "report.md").read_text(encoding="utf-8")
    assert "## Settle outcome × verdict" in text and "## Last action of the previous step" in text
    queue = json.loads((annotations / "queues" / "stability-test.json").read_text(encoding="utf-8"))
    assert (
        queue["format"] == "review-queue-1"
        and queue["label"]["regions"]["kinds"][0]["value"] == "change"
    )

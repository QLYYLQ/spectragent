"""``stability --media-areas``: declared pages compared with their media areas masked out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lookahead.cli import run
from lookahead.runs import open_runs
from lookahead.stability import ObservationStability, analyse, build_queue
from spectragent.core.capabilities.browser.media import MediaState
from spectragent.core.capabilities.browser.replay.media_areas import MediaAreaRule
from spectragent.core.capabilities.browser.replay.page_urls import PageScope
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules
from spectragent.flywheel.diagnostics import DifferenceMethod
from spectragent.schemas.step_record import ActionSpan
from tests.test_lookahead.traces import (
    StepSpec,
    click,
    frame,
    gradient,
    playing_video,
    settled,
    with_box,
    write_run,
)

PAGE = "https://video.test/play/1"
RULES = (MediaAreaRule(id="play-media", scope=PageScope(host="video.test", path="/play/*")),)
RULES_YAML = """media_areas:
- id: play-media
  scope:
    host: video.test
    path: /play/*
"""
#: The player: the top right quarter of the 128x96 screenshot, 25 % of it.
PLAYER = (64, 0, 128, 48)
BASE = gradient()
#: The video moved on: a change inside the player only.
PLAYED = with_box(BASE, (70, 5, 120, 40), (255, 0, 0))
#: The video moved on, and a popup opened outside the player.
PLAYED_AND_POPUP = with_box(PLAYED, (0, 60, 40, 90), (0, 0, 0))
SPAN = ActionSpan(
    action="browser.click_coordinate", started_at=1001.0, ended_at=1001.2, outcome="returned"
)


def _video_run(
    base: Path,
    run_id: str,
    *,
    later: bytes = PLAYED,
    url: str | None = PAGE,
    media: MediaState | None = None,
    viewport: tuple[int, int] = (128, 96),
) -> Path:
    """One observation of a video page, its delayed frame and its before-action frame showing *later*."""
    return write_run(
        base,
        run_id,
        task="Watch the video and note its title.",
        start_url="https://video.test/",
        viewport=viewport,
        steps=[
            StepSpec(
                step=0,
                image=BASE,
                url=url,
                settle=settled(),
                media=media if media is not None else playing_video(PLAYER),
                actions=[click(10, 80, "link", "Title")],
                dom_cycle=1,
                action_spans=[SPAN],
                frames=[
                    frame("after_observation", 1, image=later, requested_at=1001.0),
                    frame(
                        "before_action",
                        1,
                        image=later,
                        action_index=0,
                        action="browser.click_coordinate",
                        requested_at=1004.0,
                    ),
                ],
            ),
            StepSpec(step=1, origin="terminal", image=later, url=url, dom_cycle=2),
        ],
    )


@pytest.fixture(scope="module")
def video_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("media-areas") / "traces"
    video = root / "video"
    _video_run(video, "player")
    _video_run(video, "popup", later=PLAYED_AND_POPUP)
    # a device-scale-2 screenshot: the player's CSS box is half its image box
    _video_run(video, "hidpi", viewport=(64, 48), media=playing_video((32, 0, 64, 24)))
    _video_run(video, "large", media=playing_video((0, 0, 128, 80)))
    _video_run(video, "no-url", url=None)
    _video_run(video, "no-media", media=MediaState(read_before="unknown", read_after="unknown"))
    _video_run(video, "undeclared", url="https://other.test/watch/9")
    return root


def _by_run(root: Path, rules: tuple[MediaAreaRule, ...] | None) -> dict[str, ObservationStability]:
    items = analyse(open_runs(root), method=DifferenceMethod(), media_area_rules=rules)
    return {item.run.removeprefix("video/"): item for item in items}


def test_without_the_option_a_change_in_the_player_is_a_change(video_root: Path) -> None:
    items = _by_run(video_root, None)
    for item in items.values():
        assert item.media_areas is None
        assert item.verdict == item.unmasked_verdict == "changed_short"
    assert items["player"].media_change


def test_a_declared_page_whose_only_change_is_in_the_player_is_unchanged(video_root: Path) -> None:
    item = _by_run(video_root, RULES)["player"]
    areas = item.media_areas
    assert areas is not None and areas.status == "masked" and areas.mask_applied
    assert areas.rule_ids == ["play-media"] and areas.masked_share == 0.25
    assert [(box.kind, box.element, box.box) for box in areas.boxes] == [
        ("media_element", "main#0 video", [64, 0, 128, 48])
    ]
    assert item.verdict == "stable_through_think" == areas.verdict
    assert item.unmasked_verdict == "changed_short", "the whole images' answer is kept"
    assert item.delayed is not None and item.delayed.changed
    assert areas.delayed is not None and areas.delayed.changed is False
    assert areas.delayed.dhash_distance == 0
    assert areas.before_action is not None and areas.before_action.changed is False
    assert not areas.media_change and item.media_change


def test_the_mask_is_scaled_from_the_viewport_to_the_image(video_root: Path) -> None:
    item = _by_run(video_root, RULES)["hidpi"]
    assert item.media_areas is not None
    assert [box.box for box in item.media_areas.boxes] == [[64, 0, 128, 48]]
    assert item.verdict == "stable_through_think"


def test_a_change_outside_the_player_still_counts(video_root: Path) -> None:
    item = _by_run(video_root, RULES)["popup"]
    areas = item.media_areas
    assert areas is not None and areas.mask_applied
    assert item.verdict == "changed_short"
    assert areas.delayed is not None
    assert [found.box for found in areas.delayed.regions] == [[0, 32, 64, 96]]
    assert not areas.media_change, "what still changes is not the playing media"


def test_past_the_share_limit_the_whole_images_decide(video_root: Path) -> None:
    item = _by_run(video_root, RULES)["large"]
    areas = item.media_areas
    assert areas is not None and areas.status == "mask_too_large" and not areas.mask_applied
    assert areas.masked_share == pytest.approx(80 / 96, abs=1e-4)
    assert "the whole images are compared" in areas.detail
    assert areas.delayed is None and areas.verdict is None
    assert item.verdict == item.unmasked_verdict == "changed_short"


def test_without_a_page_url_or_media_facts_nothing_is_masked(video_root: Path) -> None:
    items = _by_run(video_root, RULES)
    no_url = items["no-url"].media_areas
    assert no_url is not None and no_url.status == "no_page_url" and no_url.rule_ids == []
    no_media = items["no-media"].media_areas
    assert no_media is not None and no_media.status == "no_media_facts"
    assert no_media.rule_ids == ["play-media"] and "media state is unknown" in no_media.detail
    undeclared = items["undeclared"].media_areas
    assert undeclared is not None and undeclared.status == "undeclared"
    for key in ("no-url", "no-media", "undeclared"):
        assert not items[key].masked and items[key].verdict == "changed_short"


def test_the_queue_suggests_the_masked_answer_and_says_why(video_root: Path) -> None:
    runs = open_runs(video_root)
    items = analyse(runs, method=DifferenceMethod(), media_area_rules=RULES)
    queue = build_queue(
        items, queue_id="q", size=60, seed=0, runs={run.key: run for run in runs.runs}
    )
    player = queue.item("video/player@0")
    assert player is not None and player.suggestion is not None
    assert player.suggestion.choice == "stable" and player.suggestion.regions == []
    assert "已遮掉媒体区域（规则 play-media）" in (player.suggestion.rationale or "")
    fields = {field.text: field for field in player.fields}
    assert fields["机器判断"].value == "一直没变（到第一个动作之前都没变）"
    assert str(fields["媒体区域"].value).startswith("已遮掉（规则 play-media）")
    assert "不遮时的判断：约 1 秒后已经变了" in str(fields["媒体区域"].value)
    assert fields["遮掉媒体区域后（细节）"].technical
    popup = queue.item("video/popup@0")
    assert popup is not None and popup.suggestion is not None
    assert popup.suggestion.choice == "other_change"
    large = {field.text: field for field in (queue.item("video/large@0") or player).fields}
    assert str(large["媒体区域"].value).startswith("没有遮（规则 play-media）")
    undeclared = queue.item("video/undeclared@0")
    assert undeclared is not None
    assert "媒体区域" not in {field.text for field in undeclared.fields}
    plain = build_queue(
        analyse(runs, method=DifferenceMethod()), queue_id="q", size=60, seed=0
    ).item("video/player@0")
    assert plain is not None and plain.suggestion is not None
    assert plain.suggestion.choice == "animation_or_video"
    assert "媒体区域" not in {field.text for field in plain.fields}


def test_cli_records_the_rule_set_and_reports_what_the_mask_changed(
    video_root: Path, tmp_path: Path
) -> None:
    rules = tmp_path / "replay_rules"
    rules.mkdir()
    (rules / "media_areas.yaml").write_text(RULES_YAML, encoding="utf-8")
    out = tmp_path / "stability"
    code = run(
        [
            "stability",
            "--root",
            str(video_root),
            "--out",
            str(out),
            "--media-areas",
            str(rules),
        ]
    )
    assert code == 0
    report = json.loads((out / "stability.json").read_text(encoding="utf-8"))
    assert report["media_areas"] == {
        "directory": str(rules.resolve()),
        "sha256": SiteRules.load(rules).sha256,
        "rules": [{"id": "play-media", "scope": {"host": "video.test", "path": "/play/*"}}],
    }
    by_run = {item["run"]: item for item in report["observations"]}
    assert by_run["video/player"]["verdict"] == "stable_through_think"
    assert by_run["video/player"]["unmasked_verdict"] == "changed_short"
    assert by_run["video/player"]["media_areas"]["status"] == "masked"
    text = (out / "report.md").read_text(encoding="utf-8")
    assert "## Media areas" in text
    assert "| masked | 3 |" in text
    assert "Of 3 masked observation(s), 2 changed only in the masked media areas" in text
    assert "and 1 still change with the mask" in text
    assert "| play-media | 3 | 2 | 1 |" in text
    assert "Observations whose playing media changed on pages no rule declares: 1; 1 more" in text
    assert "| other.test | /watch/9 | 1 |" in text


@pytest.mark.parametrize("make", ["missing", "empty"])
def test_cli_refuses_a_rule_set_without_media_areas(
    video_root: Path, tmp_path: Path, make: str
) -> None:
    rules = tmp_path / "replay_rules"
    if make == "empty":
        rules.mkdir()
    code = run(
        [
            "stability",
            "--root",
            str(video_root),
            "--out",
            str(tmp_path / "s"),
            "--media-areas",
            str(rules),
        ]
    )
    assert code == 2
    assert not (tmp_path / "s" / "stability.json").exists()

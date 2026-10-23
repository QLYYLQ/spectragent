"""Media-area rules: the video and search pages whose media restoration verification sets aside.

On a video site the playback of a recording and its offline replay rarely
match (``docs/architecture_design.md`` §4.14 "恢复：重定位与校验"). The replay
rule set declares such pages in its ``media_areas`` section; a rule applies when
its scope matches both page URLs. The section is frozen and digested with the
request rules, and request matching never reads it. What a declared page changes
in a measurement is tested with the measurement (``test_browser_restoration.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher
from spectragent.core.capabilities.browser.replay.media_areas import (
    MediaAreaRule,
    media_area_rule_ids,
)
from spectragent.core.capabilities.browser.replay.page_urls import PageScope
from spectragent.core.capabilities.browser.replay.site_rules import (
    SiteRule,
    SiteRuleError,
    SiteRules,
    SiteRulesSnapshot,
)
from tests._helpers.replay_archive import indexed

SHIPPED = Path(__file__).resolve().parents[3] / "configs" / "replay_rules"
PLAY = MediaAreaRule(id="play-media", scope=PageScope(host="video.test", path="/play/*"))


class TestScope:
    def test_a_rule_applies_when_its_scope_matches_both_page_urls(self) -> None:
        assert media_area_rule_ids(
            "https://video.test/play/1", "https://video.test/play/1?t=3", (PLAY,)
        ) == ("play-media",)

    @pytest.mark.parametrize(
        ("recorded", "restored"),
        [
            ("https://video.test/play/1", "https://video.test/home"),
            ("https://video.test/home", "https://video.test/play/1"),
            ("https://other.test/play/1", "https://other.test/play/1"),
            ("https://sub.video.test/play/1", "https://sub.video.test/play/1"),
            ("https://video.test/play/1", None),
            (None, "https://video.test/play/1"),
        ],
    )
    def test_otherwise_none_applies(self, recorded: str | None, restored: str | None) -> None:
        assert media_area_rule_ids(recorded, restored, (PLAY,)) == ()

    def test_every_matching_rule_is_named_sorted(self) -> None:
        site = MediaAreaRule(id="a-site-media", scope=PageScope(host="video.test"))
        assert media_area_rule_ids(
            "https://video.test/play/1", "https://video.test/play/2", (PLAY, site)
        ) == ("a-site-media", "play-media")

    def test_a_rule_names_a_host_and_nothing_else(self) -> None:
        for bad in (
            {"id": "a", "scope": {}},
            {"id": "a", "scope": {"host": ""}},
            {"id": "", "scope": {"host": "a.test"}},
            {"id": "a", "scope": {"host": "a.test"}, "ignore_query": ["t"]},
            {"id": "a", "scope": {"host": "a.test", "method": "GET"}},
        ):
            with pytest.raises(ValidationError):
                MediaAreaRule.model_validate(bad)


MEDIA_AREAS = "media_areas:\n  - id: play-media\n    scope: {host: video.test, path: /play/*}\n"
REQUESTS = "rules: [{id: fallback, scope: {}, exhaustion: repeat_last, unavailable: miss}]\n"


class TestTheRuleSet:
    def test_a_file_may_hold_only_media_area_rules_and_they_are_frozen_with_the_set(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "requests.yaml").write_text(REQUESTS)
        (tmp_path / "media_areas.yaml").write_text(MEDIA_AREAS)
        rules = SiteRules.load(tmp_path)
        assert rules.media_areas == (PLAY,)
        assert len(rules) == 1, "a media-area rule is not a request rule"
        frozen = rules.snapshot()
        (tmp_path / "media_areas.yaml").write_text(MEDIA_AREAS.replace("/play/*", "/watch/*"))
        assert SiteRules.load(tmp_path).sha256 != rules.sha256, "the digest covers them"
        (tmp_path / "media_areas.yaml").unlink()
        restored = SiteRules.from_snapshot(
            SiteRulesSnapshot.model_validate_json(frozen.model_dump_json())
        )
        assert restored.media_areas == rules.media_areas

    def test_identities_are_shared_with_request_and_page_url_rules(self, tmp_path: Path) -> None:
        (tmp_path / "site.yaml").write_text(REQUESTS + MEDIA_AREAS)
        assert SiteRules.load(tmp_path).media_areas == (PLAY,)
        (tmp_path / "site.yaml").write_text(
            REQUESTS + MEDIA_AREAS.replace("play-media", "fallback")
        )
        with pytest.raises(SiteRuleError, match="duplicate.*fallback"):
            SiteRules.load(tmp_path)
        (tmp_path / "site.yaml").write_text(
            MEDIA_AREAS
            + "page_urls: [{id: play-media, scope: {host: a.test}, ignore_query: [t]}]\n"
        )
        with pytest.raises(SiteRuleError, match="duplicate.*play-media"):
            SiteRules.load(tmp_path)

    @pytest.mark.parametrize(
        "body",
        [
            "media_areas: []",
            "rules: []\npage_urls: []\nmedia_areas: []",
            "media_areas: [{id: a, scope: {host: a.test}, unknown: true}]",
            "media_areas: [{id: a, scope: {path: /play}}]",
        ],
    )
    def test_an_empty_or_invalid_section_is_refused_naming_the_file(
        self, tmp_path: Path, body: str
    ) -> None:
        (tmp_path / "media_areas.yaml").write_text(body)
        with pytest.raises(SiteRuleError, match="media_areas.yaml"):
            SiteRules.load(tmp_path)

    def test_request_matching_never_reads_them(self) -> None:
        """A media-area rule for the play page changes nothing about its document request."""
        policy = SiteRule(id="fallback", exhaustion="repeat_last", unavailable="miss")
        with_rule = SiteRules([policy], media_areas=[PLAY])
        without = SiteRules([policy])
        recorded = indexed("https://video.test/play/1?a=1", record_id="doc", order=0, rule=policy)
        for live in ("https://video.test/play/1?a=1", "https://video.test/play/1?a=2"):
            results = [
                Matcher(ArchiveIndex([recorded]), rules=rules).match(LiveRequest("GET", live))
                for rules in (with_rule, without)
            ]
            assert results[0].reason == results[1].reason


# ── the published declarations ────────────────────────────────────────


def shipped() -> tuple[MediaAreaRule, ...]:
    return SiteRules.load(SHIPPED).media_areas


@pytest.mark.parametrize(
    ("url", "rule_id"),
    [
        (
            "https://v.qq.com/x/cover/mzc00200agxtrid/z00460c2bi3.html",
            "tencent-video-play-media",
        ),
        (
            "https://v.qq.com/x/search/?q=%E5%92%92%E6%9C%AF%E5%9B%9E%E6%88%98",
            "tencent-video-search-media",
        ),
        ("https://www.iqiyi.com/v_2ffkws0bgr0.html", "iqiyi-play-media"),
        ("https://www.iqiyi.com/search?q=%E5%90%91", "iqiyi-search-media"),
        ("https://www.ign.com/articles/elden-ring-review", "ign-review-media"),
        ("https://www.ign.com/search?q=Elden%20Ring", "ign-search-media"),
        ("https://www.bilibili.com/video/BV18Z4y1C7kk/", "bilibili-video-media"),
        ("https://search.bilibili.com/all?keyword=x&vt=1", "bilibili-search-media"),
    ],
)
def test_the_published_rules_declare_the_video_and_search_pages(url: str, rule_id: str) -> None:
    assert media_area_rule_ids(url, url, shipped()) == (rule_id,)


@pytest.mark.parametrize(
    "url",
    [
        # Front pages, channels and the other pages of the same sites are not declared.
        "https://v.qq.com/",
        "https://v.qq.com/channel/tv",
        "https://www.iqiyi.com/",
        "https://www.ign.com/",
        "https://www.ign.com/games/resident-evil-4",
        "https://www.ign.com/articles/best-games-2026",
        "https://www.bilibili.com/",
        "https://space.bilibili.com/927587",
        # Nor is any other site, video or not.
        "https://www.mgtv.com/b/790878/23902732.html",
        "https://search.10jqka.com.cn/search/result?w=x",
    ],
)
def test_the_published_rules_declare_nothing_else(url: str) -> None:
    assert media_area_rule_ids(url, url, shipped()) == ()

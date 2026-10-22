"""The published Tonghuashun search rule recovers panel ids without merging different searches.

Panel ids below are synthetic.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher, MatchResult
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules
from tests._helpers.replay_archive import indexed

JSON = "application/json"
SEARCH = "https://search.10jqka.com.cn/gateway/mobilesearch/comprehensive/search"
PANEL = {
    "offset": 0,
    "size": 20,
    "app_id": "wencai_pc",
    "query": "贵州茅台",
    "channels": ["news_filter", "web", "announcement", "report", "interact"],
    "qid": "a" * 32,
    "scroll_mode": "web",
    "platform": "pc",
    "slots": [],
}


def rules() -> SiteRules:
    return SiteRules.load(Path(__file__).resolve().parents[3] / "configs" / "replay_rules")


def match_one(recorded: dict[str, Any], live: dict[str, Any]) -> MatchResult:
    policies = rules()
    record = indexed(
        SEARCH,
        record_id="recorded-panel",
        order=0,
        method="POST",
        body=json.dumps(recorded).encode(),
        content_type=JSON,
        rule=policies.require_for_request("POST", SEARCH),
    )
    return Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("POST", SEARCH, {"Content-Type": JSON}, json.dumps(live).encode())
    )


def test_a_new_panel_id_finds_the_recorded_results() -> None:
    result = match_one(PANEL, {**PANEL, "qid": "z" * 32})
    assert (result.reason, result.level, result.rule_id) == (
        None,
        "normalized",
        "tonghuashun-search-results",
    )


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("query", "贵州茅台现在股价"),
        ("offset", 20),
        ("size", 3),
        ("channels", ["announcement"]),
        ("app_id", "wencai_app"),
        ("platform", "mobile"),
        ("slots", ["ai"]),
    ],
)
def test_query_paging_and_channels_still_select_the_results(field: str, other: object) -> None:
    assert match_one(PANEL, {**PANEL, "qid": "z" * 32, field: other}).reason == "no_match"


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", SEARCH),
        ("POST", "https://search.10jqka.com.cn:444/gateway/mobilesearch/comprehensive/search"),
        ("POST", "https://search.10jqka.com.cn/gateway/mobilesearch/comprehensive/suggest"),
        ("POST", "https://www.10jqka.com.cn/gateway/mobilesearch/comprehensive/search"),
        ("POST", "https://cbasspider.10jqka.com.cn:8443/spider/api/v1/report/track_info"),
    ],
)
def test_other_requests_keep_their_query_id(method: str, url: str) -> None:
    assert not rules().require_for_request(method, url).ignores_body_path("qid")

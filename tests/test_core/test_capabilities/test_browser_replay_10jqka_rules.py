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
ASSISTANT = "https://search.10jqka.com.cn/gateway/aime"
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


def match_one(recorded: dict[str, Any], live: dict[str, Any], url: str = SEARCH) -> MatchResult:
    policies = rules()
    record = indexed(
        url,
        record_id="recorded",
        order=0,
        method="POST",
        body=json.dumps(recorded).encode(),
        content_type=JSON,
        rule=policies.require_for_request("POST", url),
    )
    return Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("POST", url, {"Content-Type": JSON}, json.dumps(live).encode())
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


# ── the AI assistant ────────────────────────────────────────────────────

QUESTION = {
    "version": "3.4.1",
    "session_id": "a" * 32,
    "source": "Ths_iwencai_Xuangu",
    "input_type": "typewrite",
    "question": "贵州茅台现价",
    "agent_id": "hZDwPjEfAQ",
    "dialog_model": "CUSTOMER_AGENT",
    "entity_info": {"device_type": "mac", "comefrom": "WebiwcResearchPlaza"},
    "events": [
        {"event_type": "user_input", "event_name": "normal_agent"},
        {
            "event_type": "user_input",
            "event_name": "investment_research",
            "content": {"attachments": [{"type": "text", "theme_id": "TZ-11893"}]},
        },
    ],
}
HISTORY = {
    "session_id": "a" * 32,
    "source": "Ths_iwencai_Xuangu",
    "agent_id": "hZDwPjEfAQ",
    "theme_id": "TZ-11893",
    "title": "贵州茅台现价",
}
SUGGESTIONS = {"question": "", "source": "Ths_iwencai_Xuangu", "session_id": "a" * 32}


@pytest.mark.parametrize(
    ("path", "body", "rule_id", "selectors"),
    [
        (
            "/stream-query",
            QUESTION,
            "tonghuashun-assistant-question",
            {
                "question": "贵州茅台市值",
                "agent_id": "another",
                "events": [{"event_type": "user_input", "event_name": "normal_agent"}],
                "dialog_model": "OTHER",
            },
        ),
        (
            "/robotdata/user_session/add",
            HISTORY,
            "tonghuashun-assistant-history",
            {"title": "贵州茅台市值", "theme_id": "TZ-1", "agent_id": "another"},
        ),
        (
            "/lenovo",
            SUGGESTIONS,
            "tonghuashun-assistant-suggestions",
            {"question": "贵州茅台", "source": "another"},
        ),
    ],
)
def test_a_new_chat_id_finds_the_recorded_answer_but_other_fields_still_select(
    path: str, body: dict[str, Any], rule_id: str, selectors: dict[str, object]
) -> None:
    url = ASSISTANT + path
    live = {**body, "session_id": "f" * 32}
    found = match_one(body, live, url)
    assert (found.level, found.rule_id) == ("normalized", rule_id)
    for field, other in selectors.items():
        assert match_one(body, {**live, field: other}, url).reason == "no_match", field


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", ASSISTANT + "/stream-query"),
        ("POST", ASSISTANT + "/robotdata/user_session/delete"),
        ("POST", ASSISTANT + "/user-info"),
        ("POST", "https://www.iwencai.com/gateway/aime/stream-query"),
    ],
)
def test_other_requests_keep_their_chat_id(method: str, url: str) -> None:
    assert not rules().require_for_request(method, url).ignores_body_path("session_id")

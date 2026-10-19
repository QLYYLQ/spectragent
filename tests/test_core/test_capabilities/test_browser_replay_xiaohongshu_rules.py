"""Published search rules recover request IDs without merging different searches."""

import json
from pathlib import Path

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules
from tests._helpers.replay_archive import indexed


def rules() -> SiteRules:
    return SiteRules.load(Path(__file__).resolve().parents[3] / "configs" / "replay_rules")


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("keyword", "北京 咖啡店"),
        ("page", 2),
        ("page_size", 10),
        ("sort", "time_descending"),
        ("note_type", 1),
        ("geo", "another-location"),
        ("image_formats", ["jpg"]),
        ("ext_flags", ["another-filter"]),
    ],
)
def test_notes_ids_can_change_but_content_selectors_cannot(field: str, other: object) -> None:
    policies = rules()
    url = "https://so.xiaohongshu.com/api/sns/web/v2/search/notes"
    original = {
        "keyword": "上海 咖啡店",
        "page": 1,
        "page_size": 20,
        "sort": "general",
        "note_type": 0,
        "geo": "",
        "image_formats": ["jpg", "webp", "avif"],
        "ext_flags": [],
        "search_id": "recorded-search",
        "session_id": "recorded-search-session",
    }
    record = indexed(
        url,
        record_id="recorded-notes",
        order=0,
        method="POST",
        body=json.dumps(original).encode(),
        content_type="application/json",
        rule=policies.require_for_request("POST", url),
    )
    changed_ids = {**original, "search_id": "live-search", "session_id": "live-search-session"}

    def match(body: dict[str, object]):
        return Matcher(ArchiveIndex([record]), rules=policies).match(
            LiveRequest(
                "POST", url, {"Content-Type": "application/json"}, json.dumps(body).encode()
            )
        )

    recovered = match(changed_ids)
    assert recovered.record is record and recovered.level == "normalized"
    assert match({**changed_ids, field: other}).reason == "no_match"


@pytest.mark.parametrize(("field", "other"), [("keyword", "北京 咖啡店"), ("biz_type", "other")])
def test_onebox_preserves_query_and_business_type(field: str, other: str) -> None:
    policies = rules()
    url = "https://edith.xiaohongshu.com/api/sns/web/v1/search/onebox"
    original = {
        "keyword": "上海 咖啡店",
        "biz_type": "web_search_user",
        "search_id": "recorded-search",
        "request_id": "recorded-request",
    }
    record = indexed(
        url,
        record_id="recorded-onebox",
        order=0,
        method="POST",
        body=json.dumps(original).encode(),
        content_type="application/json",
        rule=policies.require_for_request("POST", url),
    )
    live = {**original, "search_id": "live-search", "request_id": "live-request"}
    for body, expected in ((live, None), ({**live, field: other}, "no_match")):
        result = Matcher(ArchiveIndex([record]), rules=policies).match(
            LiveRequest(
                "POST", url, {"Content-Type": "application/json"}, json.dumps(body).encode()
            )
        )
        assert result.reason == expected
        if expected is None:
            assert result.record is record and result.level == "normalized"


def test_filter_request_id_does_not_merge_keywords() -> None:
    policies = rules()
    base = "https://edith.xiaohongshu.com/api/sns/web/v1/search/filter"
    record = indexed(
        base + "?keyword=coffee&search_id=recorded",
        record_id="recorded-filter",
        order=0,
        rule=policies.require_for_request("GET", base),
    )
    recovered = Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("GET", base + "?keyword=coffee&search_id=live")
    )
    assert recovered.record is record and recovered.level == "normalized"
    different = Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("GET", base + "?keyword=tea&search_id=live")
    )
    assert different.reason == "no_match"


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "https://so.xiaohongshu.com/api/sns/web/v2/search/notes"),
        ("POST", "https://so.xiaohongshu.com:444/api/sns/web/v2/search/notes"),
        ("POST", "https://so.xiaohongshu.com/api/sns/web/v2/search/notes/other"),
        ("POST", "https://edith.xiaohongshu.com/api/sns/web/v2/user/me"),
    ],
)
def test_other_requests_keep_session_and_search_identifiers(method: str, url: str) -> None:
    policy = rules().require_for_request(method, url)
    assert not policy.ignores_body_path("session_id")
    assert not policy.ignores_body_path("search_id")
    assert not policy.ignores_query("search_id")

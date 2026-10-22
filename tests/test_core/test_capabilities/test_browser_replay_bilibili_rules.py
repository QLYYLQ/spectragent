"""Published Bilibili rules recover re-signed content requests without merging different content.

Under the shared replay clock a replayed page signs its WBI requests at another second, so
``wts`` and the signature ``w_rid`` change on every one, along with the per-search ``qv_id``,
the player's per-play ``session`` and the ``dm_img_inter`` telemetry. Field names and shapes
follow the recorded requests named in ``configs/replay_rules/bilibili.com.yaml``; all
identifiers, signatures and telemetry values below are synthetic.
"""

from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher, MatchResult
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules
from tests._helpers.replay_archive import indexed

API = "https://api.bilibili.com"
LOCALE = '{"c_locale":{"language":"zh","script":"Hans"},"always_translate":false}'
DEVICE = '{"platform":"web","device":"pc","spmid":"333.1007","mobi_app":"web_cn"}'
SIGNED = {"w_rid": "0" * 32, "wts": "1700000000"}
#: What the replayed page sends instead: another second, hence another signature.
RESIGNED = {"w_rid": "f" * 32, "wts": "1700000037"}
TELEMETRY = '{"ds":[],"wh":[4003,4401,1],"of":[12,24,12]}'
NEW_TELEMETRY = '{"ds":[],"wh":[4172,4369,22],"of":[189,378,189]}'

#: Per rule: the endpoint, a recorded request's fields, the fields a replay changes, and
#: the fields that select content (each changed alone must still miss).
ENDPOINTS: dict[str, tuple[str, str, dict[str, str], dict[str, str], dict[str, str]]] = {
    "bilibili-search-results": (
        "GET",
        "/x/web-interface/wbi/search/type",
        {
            "search_type": "video",
            "page": "1",
            "page_size": "42",
            "order": "",
            "keyword": "kettle",
            "pubtime_begin_s": "0",
            "qv_id": "a" * 32,
            **SIGNED,
        },
        {"qv_id": "b" * 32, **RESIGNED},
        {
            "keyword": "teapot",
            "page": "2",
            "order": "click",
            "search_type": "bili_user",
            "pubtime_begin_s": "1700000000",
        },
    ),
    "bilibili-search-all": (
        "GET",
        "/x/web-interface/wbi/search/all/v2",
        {"page": "1", "keyword": "kettle", "qv_id": "a" * 32, **SIGNED},
        {"qv_id": "b" * 32, **RESIGNED},
        {"keyword": "teapot", "page": "2"},
    ),
    "bilibili-search-placeholder": (
        "GET",
        "/x/web-interface/wbi/search/default",
        {"web_location": "333.1007", "x-bili-locale-json": LOCALE, **SIGNED},
        RESIGNED,
        {"web_location": "333.337", "x-bili-locale-json": LOCALE.replace("zh", "en")},
    ),
    "bilibili-search-trending": (
        "GET",
        "/x/web-interface/wbi/search/square",
        {"limit": "10", "platform": "web", **SIGNED},
        RESIGNED,
        {"limit": "50", "platform": "h5"},
    ),
    "bilibili-search-suggestions": (
        "GET",
        "/x/web-interface/suggest",
        {"term": "kett", "highlight": "0", **SIGNED},
        RESIGNED,
        {"term": "kettl"},
    ),
    "bilibili-page-slots": (
        "GET",
        "/x/web-show/wbi/res/locs",
        {"pf": "0", "ids": "2837,2836", "x-bili-device-req-json": DEVICE, **SIGNED},
        RESIGNED,
        {"ids": "2837", "pf": "1", "x-bili-device-req-json": DEVICE.replace("pc", "h5")},
    ),
    "bilibili-page-slot": (
        "GET",
        "/x/web-show/res/locs",
        {"pf": "0", "ids": "3449", **SIGNED},
        RESIGNED,
        {"ids": "5742"},
    ),
    "bilibili-video-detail": (
        "GET",
        "/x/web-interface/wbi/view/detail",
        {"bvid": "BV1aaaaaaaaa", "need_view": "1", "dm_img_inter": TELEMETRY, **SIGNED},
        {"dm_img_inter": NEW_TELEMETRY, **RESIGNED},
        {"bvid": "BV1bbbbbbbbb", "need_view": "0", "dm_img_list": "[]"},
    ),
    "bilibili-player-info": (
        "GET",
        "/x/player/wbi/v2",
        {"aid": "1001", "cid": "2001", "dm_img_inter": TELEMETRY, **SIGNED},
        {"dm_img_inter": NEW_TELEMETRY, **RESIGNED},
        {"aid": "1002", "cid": "2002"},
    ),
    "bilibili-play-url": (
        "GET",
        "/x/player/wbi/playurl",
        {
            "avid": "1001",
            "bvid": "BV1aaaaaaaaa",
            "cid": "2001",
            "qn": "32",
            "fnval": "2000",
            "gaia_source": "view-card",
            "session": "1" * 32,
            "dm_img_inter": TELEMETRY,
            **SIGNED,
        },
        {"session": "2" * 32, "dm_img_inter": NEW_TELEMETRY, **RESIGNED},
        {
            "avid": "1002",
            "bvid": "BV1bbbbbbbbb",
            "cid": "2002",
            "qn": "80",
            "gaia_source": "pre-load",
        },
    ),
    "bilibili-danmaku-segments": (
        "GET",
        "/x/v2/dm/wbi/web/seg.so",
        {"type": "1", "oid": "2001", "pid": "1001", "segment_index": "1", **SIGNED},
        RESIGNED,
        {"oid": "2002", "pid": "1002", "segment_index": "2", "type": "2"},
    ),
    "bilibili-danmaku-settings": (
        "GET",
        "/x/v2/dm/web/view",
        {"type": "1", "oid": "2001", "pid": "1001", "duration": "120", **SIGNED},
        RESIGNED,
        {"oid": "2002", "pid": "1002", "duration": "121"},
    ),
    "bilibili-subtitles": (
        "GET",
        "/x/v2/subtitle/web/view",
        {"oid": "2001", "pid": "1001", "type": "1", "x-bili-locale-json": LOCALE, **SIGNED},
        RESIGNED,
        {"oid": "2002", "pid": "1002", "x-bili-locale-json": LOCALE.replace("zh", "en")},
    ),
    "bilibili-summary-button": (
        "GET",
        "/x/web-interface/view/conclusion/judge",
        {"bvid": "BV1aaaaaaaaa", "cid": "2001", "up_mid": "3001", **SIGNED},
        RESIGNED,
        {"bvid": "BV1bbbbbbbbb", "cid": "2002", "up_mid": "3002"},
    ),
    "bilibili-space-acc-info-signature": (
        "GET",
        "/x/space/wbi/acc/info",
        {
            "mid": "1001",
            "token": "",
            "platform": "web",
            "web_location": "1550101",
            "dm_img_list": "[]",
            "dm_img_str": "c3ludGhldGljLWds",
            "dm_cover_img_str": "c3ludGhldGljLXJlbmRlcmVy",
            "dm_img_inter": TELEMETRY,
            **SIGNED,
        },
        {"dm_img_inter": NEW_TELEMETRY, **RESIGNED},
        {
            "mid": "1002",
            "token": "another-token",
            "platform": "h5",
            "web_location": "333.1387",
            "dm_img_list": '[{"x":1,"y":2,"z":0,"timestamp":1,"type":0}]',
            "dm_img_str": "YW5vdGhlci1nbA",
            "dm_cover_img_str": "YW5vdGhlci1yZW5kZXJlcg",
        },
    ),
    "bilibili-space-relation": (
        "GET",
        "/x/space/wbi/acc/relation",
        {"mid": "1001", "web_location": "333.1387", **SIGNED},
        RESIGNED,
        {"mid": "1002"},
    ),
    "bilibili-visitor-ticket": (
        "POST",
        "/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
        {"key_id": "ec02", "hexsign": "a" * 64, "context[ts]": "1700000000", "csrf": ""},
        {"hexsign": "b" * 64, "context[ts]": "1700000037"},
        {"key_id": "ec03", "csrf": "token"},
    ),
}


def rules() -> SiteRules:
    return SiteRules.load(Path(__file__).resolve().parents[3] / "configs" / "replay_rules")


def url(path: str, params: dict[str, str]) -> str:
    return f"{API}{path}?{urlencode(params, quote_via=quote)}"


def match_one(policies: SiteRules, method: str, recorded: str, live: str) -> MatchResult:
    record = indexed(
        recorded,
        record_id="recorded",
        order=0,
        method=method,
        rule=policies.require_for_request(method, recorded),
    )
    return Matcher(ArchiveIndex([record]), rules=policies).match(LiveRequest(method, live))


@pytest.mark.parametrize("rule_id", sorted(ENDPOINTS))
def test_a_resigned_request_finds_the_recorded_one(rule_id: str) -> None:
    method, path, recorded, changed, _selectors = ENDPOINTS[rule_id]
    result = match_one(rules(), method, url(path, recorded), url(path, {**recorded, **changed}))
    assert (result.reason, result.level, result.rule_id) == (None, "normalized", rule_id)
    assert result.record is not None and result.record.record_id == "recorded"


@pytest.mark.parametrize(
    ("rule_id", "field"),
    [(rule_id, field) for rule_id, spec in sorted(ENDPOINTS.items()) for field in spec[4]],
)
def test_content_fields_still_select(rule_id: str, field: str) -> None:
    method, path, recorded, changed, selectors = ENDPOINTS[rule_id]
    live = url(path, {**recorded, **changed, field: selectors[field]})
    result = match_one(rules(), method, url(path, recorded), live)
    assert result.reason == "no_match", f"{rule_id} must not ignore {field}"


def test_the_ticket_rule_names_the_bracketed_parameter_literally() -> None:
    policy = rules().require_for_request(
        "POST", f"{API}/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
    )
    assert policy.ignores_query("context[ts]")
    assert not policy.ignores_query("contexts") and not policy.ignores_query("contextt")


def test_profile_responses_are_served_in_recording_order_then_repeated() -> None:
    policies = rules()
    method, path, recorded, changed, _ = ENDPOINTS["bilibili-space-acc-info-signature"]
    first = indexed(
        url(path, recorded),
        record_id="first-visit",
        order=0,
        rule=policies.require_for_request(method, url(path, recorded)),
    )
    later = {**recorded, "dm_img_inter": '{"ds":[],"wh":[4099,4433,33],"of":[274,548,274]}'}
    second = indexed(
        url(path, later),
        record_id="second-visit",
        order=1,
        rule=policies.require_for_request(method, url(path, later)),
    )
    matcher = Matcher(ArchiveIndex([first, second]), rules=policies)
    served = [
        matcher.match(LiveRequest(method, url(path, {**recorded, **changed}))) for _ in range(3)
    ]
    assert [result.record for result in served] == [first, second, second]
    assert [result.exhausted for result in served] == [False, False, True]


@pytest.mark.parametrize(
    ("method", "address"),
    [
        ("POST", f"{API}/x/space/wbi/acc/info"),
        ("GET", "https://api.bilibili.com:444/x/space/wbi/acc/info"),
        ("GET", "https://www.api.bilibili.com/x/space/wbi/acc/info"),
        ("GET", f"{API}/x/space/wbi/arc/search"),
        ("GET", f"{API}/x/space/wbi/article"),
        ("GET", f"{API}/x/v2/dm/wbi/web/history/seg.so"),
        ("POST", f"{API}/x/click-interface/web/heartbeat"),
        ("GET", f"{API}/x/web-interface/wbi/index/top/feed/rcmd"),
        ("POST", f"{API}/x/web-interface/wbi/search/type"),
    ],
)
def test_other_requests_keep_clock_signature_and_telemetry(method: str, address: str) -> None:
    policy = rules().require_for_request(method, address)
    for field in ("wts", "w_rid", "dm_img_inter", "qv_id", "session"):
        assert not policy.ignores_query(field), (address, field)

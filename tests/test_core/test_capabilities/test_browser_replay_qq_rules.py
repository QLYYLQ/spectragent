"""Published Tencent Video rules recover fresh visitor ids without merging different requests.

Visitor ids, cookies and callback names below are synthetic. Field names and the
shape of each body follow the recorded requests named in ``configs/replay_rules/qq.com.yaml``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.canonical import (
    BODY_SHA256_PARAM,
    POST_DATA_PARAM,
    body_params,
)
from spectragent.core.capabilities.browser.replay.jsonp import rewrite_callback
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher, MatchResult
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules
from tests._helpers.replay_archive import indexed

JSON = "application/json"
FEATURE_SWITCHES = "https://config.ab.qq.com/tab/GetTabRemoteConfig"
EXPERIMENT_GROUPS = "https://data.ab.qq.com/wabt/get_gray_policy"
BUCKET = "https://access.video.qq.com/common_config/get_bi"
SEARCH = (
    "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch"
    "?vversion_platform=2"
)
PLAY_INFO = "https://vd6.l.qq.com/vinfo_proxy"


def rules() -> SiteRules:
    return SiteRules.load(Path(__file__).resolve().parents[3] / "configs" / "replay_rules")


def match_one(
    policies: SiteRules,
    url: str,
    recorded: dict[str, Any],
    live: dict[str, Any],
    *,
    content_type: str = JSON,
) -> MatchResult:
    record = indexed(
        url,
        record_id="recorded",
        order=0,
        method="POST",
        body=json.dumps(recorded).encode(),
        content_type=content_type,
        rule=policies.require_for_request("POST", url),
    )
    return Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("POST", url, {"Content-Type": content_type}, json.dumps(live).encode())
    )


# ── feature switches ────────────────────────────────────────────────────

SWITCHES = {
    "app_id": "5800",
    "app_key": "<redacted>",
    "config_keys": ["page_slice_search_hover_sdk_option", "txv_nav_history_pop_launch"],
    "guid": "1111aaaa2222bbbb",
}


def test_feature_switches_match_across_a_new_visitor_id() -> None:
    result = match_one(
        rules(), FEATURE_SWITCHES, SWITCHES, {**SWITCHES, "guid": "9999ffff8888eeee"}
    )
    assert (result.reason, result.level, result.rule_id) == (
        None,
        "normalized",
        "tencent-video-feature-switches",
    )


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("config_keys", ["txv_play_public_config", "player_comment_show"]),
        ("config_keys", ["page_slice_search_hover_sdk_option"]),
        ("app_id", "5801"),
    ],
)
def test_feature_switches_still_select_by_keys_and_app(field: str, other: object) -> None:
    live = {**SWITCHES, "guid": "9999ffff8888eeee", field: other}
    assert match_one(rules(), FEATURE_SWITCHES, SWITCHES, live).reason == "no_match"


def protobuf_switches(guid: str, cached_version: int) -> bytes:
    """The SDK's protobuf request: app id, guid, cached config version, SDK fields."""
    version = bytearray()
    while True:
        byte, cached_version = cached_version & 0x7F, cached_version >> 7
        version.append(byte | (0x80 if cached_version else 0))
        if not cached_version:
            break
    return (
        b"\n\x045800\x1a"
        + bytes([len(guid)])
        + guid.encode()
        + b"("
        + bytes(version)
        + b"2\x050.0.08fJ\x04gzip"
    )


def test_the_protobuf_variant_is_still_matched_on_its_whole_body() -> None:
    """The cached version in the body chooses a full config or a not-modified answer."""
    policies = rules()
    url = FEATURE_SWITCHES
    content_type = "application/proto"
    record = indexed(
        url,
        record_id="recorded-proto",
        order=0,
        method="POST",
        body=protobuf_switches("1111aaaa2222bbbb", 0),
        content_type=content_type,
        rule=policies.require_for_request("POST", url),
    )
    assert [name for name, _ in record.keys.params] == [f"body:{POST_DATA_PARAM}"]
    matcher = Matcher(ArchiveIndex([record]), rules=policies)
    for body in (
        protobuf_switches("9999ffff8888eeee", 0),
        protobuf_switches("1111aaaa2222bbbb", 1792658376),
    ):
        live = LiveRequest("POST", url, {"Content-Type": content_type}, body)
        assert matcher.match(live).reason == "no_match"


# ── experiment groups ───────────────────────────────────────────────────


def test_experiment_groups_match_across_a_new_visitor_id_and_keep_the_app() -> None:
    policies = rules()
    recorded = {"guid": "1111aaaa2222bbbb", "appid": "5800"}
    live = {"guid": "0123456789abcdef0123456789abcdef", "appid": "5800"}
    found = match_one(policies, EXPERIMENT_GROUPS, recorded, live)
    assert (found.level, found.rule_id) == ("normalized", "tencent-video-experiment-groups")
    assert match_one(policies, EXPERIMENT_GROUPS, recorded, {**live, "appid": "5801"}).reason == (
        "no_match"
    )


def test_experiment_groups_are_served_in_recorded_order_then_repeated() -> None:
    policies = rules()
    url = EXPERIMENT_GROUPS
    rule = policies.require_for_request("POST", url)
    first, second = (
        indexed(
            url,
            record_id=f"visit-{order}",
            order=order,
            method="POST",
            body=json.dumps({"guid": guid, "appid": "5800"}).encode(),
            content_type=JSON,
            rule=rule,
        )
        for order, guid in ((0, "aaaa000011112222"), (1, "bbbb333344445555"))
    )
    matcher = Matcher(ArchiveIndex([first, second]), rules=policies)
    live = LiveRequest(
        "POST", url, {"Content-Type": JSON}, b'{"guid":"cccc666677778888","appid":"5800"}'
    )
    served = [matcher.match(live) for _ in range(3)]
    assert [result.record for result in served] == [first, second, second]
    assert [result.exhausted for result in served] == [False, False, True]


# ── visitor bucket JSONP ────────────────────────────────────────────────


def bucket(callback: str, stamp: str, vappid: str = "09693353") -> str:
    return f"{BUCKET}?vappid={vappid}&vsecret=0123abcd&callback={callback}&_={stamp}"


def test_bucket_jsonp_matches_a_new_callback_and_cache_buster_and_calls_the_new_name() -> None:
    policies = rules()
    recorded_url = bucket("jQuery19100000000000000001_1700000000001", "1700000000002")
    live_url = bucket("jQuery19109999999999999999_1700000005001", "1700000005002")
    record = indexed(
        recorded_url,
        record_id="recorded-bucket",
        order=0,
        rule=policies.require_for_request("GET", recorded_url),
    )
    result = Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("GET", live_url, resource_type="Script")
    )
    assert (result.record, result.rule_id) == (record, "tencent-video-bucket-jsonp")
    recorded_body = b'jQuery19100000000000000001_1700000000001({"data":{"ret":0},"ret":0});'
    rewritten = rewrite_callback(
        recorded_body,
        recorded_url=recorded_url,
        request_url=live_url,
        resource_type="Script",
        content_type="application/javascript",
    )
    assert rewritten is not None
    assert rewritten[0].startswith(b"jQuery19109999999999999999_1700000005001(")


def test_bucket_jsonp_keeps_its_app_id() -> None:
    policies = rules()
    recorded_url = bucket("jQuery1_1", "2")
    record = indexed(
        recorded_url,
        record_id="recorded-bucket",
        order=0,
        rule=policies.require_for_request("GET", recorded_url),
    )
    result = Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("GET", bucket("jQuery3_3", "4", vappid="11111111"))
    )
    assert result.reason == "no_match"


# ── video search ────────────────────────────────────────────────────────

SEARCH_BODY = {
    "version": "26022601",
    "clientType": 1,
    "filterValue": "",
    "uuid": "AAAAAAAA-1111-4222-8333-BBBBBBBBBBBB",
    "query": "权力的游戏",
    "retry": 0,
    "pagenum": 0,
    "isPrefetch": True,
    "pagesize": 30,
    "queryFrom": 0,
    "searchDatakey": "",
    "transInfo": "",
    "isneedQc": True,
    "preQid": "",
    "adClientInfo": "",
    "extraInfo": {"isNewMarkLabel": "1", "multi_terminal_pc": "1", "themeType": "1"},
    "featureList": ["DEFAULT_FEFEATURE", "PC_WANT_EPISODE"],
}


def test_search_matches_a_new_search_box_id() -> None:
    live = {**SEARCH_BODY, "uuid": "CCCCCCCC-5555-4666-8777-DDDDDDDDDDDD"}
    result = match_one(rules(), SEARCH, SEARCH_BODY, live)
    assert (result.level, result.rule_id) == ("normalized", "tencent-video-search")


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("query", "权力的游戏第一季"),
        ("pagenum", 1),
        ("pagesize", 10),
        ("filterValue", "firstTabid=150"),
        ("isPrefetch", False),
        ("featureList", ["DEFAULT_FEFEATURE"]),
    ],
)
def test_search_still_selects_by_query_paging_and_filters(field: str, other: object) -> None:
    live = {**SEARCH_BODY, "uuid": "CCCCCCCC-5555-4666-8777-DDDDDDDDDDDD", field: other}
    assert match_one(rules(), SEARCH, SEARCH_BODY, live).reason == "no_match"


# ── search suggestions ──────────────────────────────────────────────────

SUGGEST = (
    "https://actapi.video.qq.com/trpc.videosearch.smartboxServer.SugRecallHttp/GetSugHttp"
    "?vplatform=2"
)
SUGGEST_BODY = {
    "auth_info": {"app_id": "3172", "app_key": "<redacted>"},
    "query": "权力的游戏",
    "scene_id": 5,
    "page_num": 0,
    "page_size": 10,
    "sug_id": "1700000000000aaaaaaaaaaa",
}


def test_suggestions_match_a_new_request_id_and_keep_the_typed_text() -> None:
    policies = rules()
    live = {**SUGGEST_BODY, "sug_id": "1700000009999bbbbbbbbbbb"}
    found = match_one(policies, SUGGEST, SUGGEST_BODY, live)
    assert (found.level, found.rule_id) == ("normalized", "tencent-video-search-suggestions")
    for field, other in (("query", "权力的游戏第一季"), ("page_num", 1), ("scene_id", 6)):
        assert match_one(policies, SUGGEST, SUGGEST_BODY, {**live, field: other}).reason == (
            "no_match"
        )


# ── play page layout ────────────────────────────────────────────────────

LAYOUT = (
    "https://pbaccess.video.qq.com/trpc.vector_layout.page_view.PageService/getPage"
    "?vdevice_guid={guid}&video_appid=3000010&vversion_name=8.5.96&vversion_platform=2"
)


def layout_body(guid: str, **params: Any) -> dict[str, Any]:
    return {
        "page_params": {"page_type": "detail_operation", "cid": "mzc00200aaaaaaa"},
        "page_bypass_params": {
            "params": {"page_id": "detail_recommend_pull", "caller_id": "3000010", **params},
            "scene": "desk_detail",
            "abtest_bypass_id": guid,
        },
        "page_context": None,
    }


def match_layout(recorded: dict[str, Any], live: dict[str, Any], live_guid: str) -> MatchResult:
    policies = rules()
    recorded_url = LAYOUT.format(guid="1111aaaa2222bbbb")
    record = indexed(
        recorded_url,
        record_id="recorded-layout",
        order=0,
        method="POST",
        body=json.dumps(recorded).encode(),
        content_type=JSON,
        rule=policies.require_for_request("POST", recorded_url),
    )
    return Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest(
            "POST",
            LAYOUT.format(guid=live_guid),
            {"Content-Type": JSON},
            json.dumps(live).encode(),
        )
    )


def test_the_layout_matches_a_new_visitor_id_in_query_and_body() -> None:
    result = match_layout(
        layout_body("1111aaaa2222bbbb"), layout_body("9999ffff8888eeee"), "9999ffff8888eeee"
    )
    assert (result.level, result.rule_id) == ("normalized", "tencent-video-page-layout")


@pytest.mark.parametrize(
    "live",
    [
        layout_body("9999ffff8888eeee", page_id="100101"),
        {**layout_body("9999ffff8888eeee"), "page_params": {"cid": "mzc00200bbbbbbb"}},
        {**layout_body("9999ffff8888eeee"), "page_context": {"page_index": "2"}},
    ],
)
def test_the_layout_still_selects_its_page_and_position(live: dict[str, Any]) -> None:
    assert match_layout(layout_body("1111aaaa2222bbbb"), live, "9999ffff8888eeee").reason == (
        "no_match"
    )


# ── membership checks ───────────────────────────────────────────────────


def membership(stamp: str, **fields: str) -> str:
    params = {
        "name": "spp_pre_authentication",
        "cmd": "24792",
        "callback": "_",
        "platform": "2",
        "cid": "mzc00200aaaaaaa",
        "vid": "k0000000000",
        "_": stamp,
        **fields,
    }
    return "https://vip.video.qq.com/fcgi-bin/comm_cgi?" + "&".join(
        f"{key}={value}" for key, value in params.items()
    )


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({}, None),
        ({"name": "spp_pc_pay_tips"}, "no_match"),
        ({"cmd": "24793"}, "no_match"),
        ({"cid": "mzc00200bbbbbbb"}, "no_match"),
        ({"vid": "k1111111111"}, "no_match"),
    ],
)
def test_membership_checks_ignore_only_the_cache_buster(
    fields: dict[str, str], reason: str | None
) -> None:
    policies = rules()
    recorded_url = membership("1700000000001")
    record = indexed(
        recorded_url,
        record_id="recorded-check",
        order=0,
        rule=policies.require_for_request("GET", recorded_url),
    )
    result = Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("GET", membership("1700000009999", **fields))
    )
    assert result.reason == reason


# ── play page video info ────────────────────────────────────────────────


def play_info(cookie: str, **fields: Any) -> dict[str, Any]:
    """A play info request; the real one carries a few kilobytes of player config."""
    return {
        "cid": "mzc00200aaaaaaa",
        "vid": "",
        "vqqcookie": cookie,
        "playParams": {},
        "vinfoParams": {
            "adVersion": "4.4.2",
            "dynamicConfig": {f"switch{index}": "x" * 40 for index in range(80)},
        },
        **fields,
    }


RECORDED_COOKIE = "video_guid=1111aaaa2222bbbb; pgv_pvid=1000000001; " + "x=" + "1" * 600
LIVE_COOKIE = "video_guid=9999ffff8888eeee; pgv_pvid=2000000002; " + "x=" + "2" * 900


def test_play_info_bodies_are_past_the_body_cap_and_still_match_across_cookies() -> None:
    policies = rules()
    rule = policies.require_for_request("POST", PLAY_INFO)
    recorded, live = play_info(RECORDED_COOKIE), play_info(LIVE_COOKIE)
    for body in (recorded, live):
        keyed = body_params(json.dumps(body).encode(), JSON, rule)
        assert [name for name, _ in keyed] == [BODY_SHA256_PARAM], (
            "the remaining fields alone exceed the cap, so the key is their sha256"
        )
    result = match_one(policies, PLAY_INFO, recorded, live)
    assert (result.level, result.rule_id) == ("normalized", "tencent-video-play-info")


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("cid", "mzc00200bbbbbbb"),
        ("vid", "k0000000000"),
        ("playParams", {"localHistoryVid": "k0000000000", "localHistoryPos": 198}),
    ],
)
def test_play_info_still_selects_the_video_and_its_resume_point(field: str, other: object) -> None:
    live = play_info(LIVE_COOKIE, **{field: other})
    assert match_one(rules(), PLAY_INFO, play_info(RECORDED_COOKIE), live).reason == "no_match"


# ── scope ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", FEATURE_SWITCHES),
        ("POST", "https://config.ab.qq.com:444/tab/GetTabRemoteConfig"),
        ("POST", "https://config.ab.qq.com/tab/GetTabConfig"),
        ("POST", "https://data.ab.qq.com/wabt/report"),
        ("POST", "https://h.trace.qq.com/kv"),
        (
            "POST",
            "https://pbaccess.video.qq.com/trpc.universal_backend_service.union_extra_data"
            ".UnionExtraData/FillUnionInfo",
        ),
        ("POST", "https://vd6.l.qq.com/proxyhttp"),
        ("POST", "https://vi.l.qq.com/vinfo_proxy"),
        ("GET", PLAY_INFO),
        ("GET", "https://access.video.qq.com/common_config/get_config"),
        (
            "POST",
            "https://pbaccess.video.qq.com/trpc.universal_backend_service.page_server_rpc"
            ".PageServer/GetPageData",
        ),
        ("POST", "https://actapi.video.qq.com/trpc.videosearch.smartboxServer.Other/GetSugHttp"),
        ("GET", "https://vip.video.qq.com/fcgi-bin/other_cgi"),
    ],
)
def test_other_requests_keep_visitor_ids_cookies_and_callbacks(method: str, url: str) -> None:
    policy = rules().require_for_request(method, url)
    for field in (
        "guid",
        "uuid",
        "vqqcookie",
        "sug_id",
        "page_bypass_params.abtest_bypass_id",
    ):
        assert not policy.ignores_body_path(field)
    for field in ("callback", "_", "vdevice_guid"):
        assert not policy.ignores_query(field)

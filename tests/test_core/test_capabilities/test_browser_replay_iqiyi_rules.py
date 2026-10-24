"""Published iQIYI rules recover re-signed requests without merging different videos.

Under the shared replay clock the replayed page also makes a new visitor device id, which
every home page, search and player API carries under one of several names. Ids, trace
values and signatures below are synthetic.
"""

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher, MatchResult
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules
from tests._helpers.replay_archive import indexed

REDIRECT = "https://www.iqiyi.com/tvg/to_page_url"
BASE_INFO = "https://mesh.if.iqiyi.com/tvg/v2/lw/base_info"

CLICK = {
    "album_id": "",
    "app_version": "17.103.26647",
    "ext_params": "lt=2&e2=00000000000000000000000000000001&qyTrace=recordedTrace001",
    "on_record": "1",
    "timestamp": "1700000000111",
    "tv_id": "MTAwMDAwMDAwMDAwMDAwMA==",
    "sign": "0" * 32,
}
#: A later click on the same result: a new trace id and time, hence a new signature.
RECLICK = {
    **CLICK,
    "ext_params": "lt=2&e2=00000000000000000000000000000001&qyTrace=liveTrace0000002",
    "timestamp": "1700000009999",
    "sign": "F" * 32,
}
DETAILS = {
    "entity_id": "1000000000000000",
    "device_id": "0123456789abcdef0123456789abcdef",
    "auth_cookie": "",
    "user_id": "0",
    "vip_type": "-1",
    "vip_status": "0",
    "conduit_id": "",
    "pcv": "17.103.26647",
    "app_version": "17.103.26647",
    "ext": "",
    "app_mode": "standard",
    "scale": "100",
    "timestamp": "1700000000222",
    "src": "pca_tvg",
    "os": "",
    "sign": "0" * 32,
}


def rules() -> SiteRules:
    return SiteRules.load(Path(__file__).resolve().parents[3] / "configs" / "replay_rules")


def url(base: str, params: dict[str, str]) -> str:
    return f"{base}?{urlencode(params, quote_via=quote)}"


def match_one(base: str, recorded: dict[str, str], live: dict[str, str]) -> MatchResult:
    policies = rules()
    address = url(base, recorded)
    record = indexed(
        address,
        record_id="recorded",
        order=0,
        status=302 if base == REDIRECT else 200,
        rule=policies.require_for_request("GET", address),
    )
    return Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("GET", url(base, live), is_document=base == REDIRECT)
    )


def test_a_reclicked_result_finds_the_recorded_redirect() -> None:
    result = match_one(REDIRECT, CLICK, RECLICK)
    assert (result.reason, result.level, result.rule_id) == (
        None,
        "normalized",
        "iqiyi-video-page-redirect",
    )


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("tv_id", "MjAwMDAwMDAwMDAwMDAwMA=="),
        ("album_id", "MjUyNDQ5MTAx"),
        ("app_version", "17.104.0"),
        ("on_record", "0"),
    ],
)
def test_the_redirect_still_selects_its_video(field: str, other: str) -> None:
    assert match_one(REDIRECT, CLICK, {**RECLICK, field: other}).reason == "no_match"


def test_redirects_are_served_in_recorded_order_then_repeated() -> None:
    policies = rules()
    first_url = url(REDIRECT, CLICK)
    second_url = url(REDIRECT, {**CLICK, "ext_params": "lt=2&e2=2&qyTrace=t2", "sign": "1" * 32})
    first, second = (
        indexed(
            address,
            record_id=name,
            order=order,
            status=302,
            rule=policies.require_for_request("GET", address),
        )
        for order, (name, address) in enumerate((("first", first_url), ("second", second_url)))
    )
    matcher = Matcher(ArchiveIndex([first, second]), rules=policies)
    served = [matcher.match(LiveRequest("GET", url(REDIRECT, RECLICK))) for _ in range(3)]
    assert [result.record for result in served] == [first, second, second]
    assert [result.exhausted for result in served] == [False, False, True]


def test_video_details_match_across_a_new_time_and_signature() -> None:
    live = {**DETAILS, "timestamp": "1700000007777", "sign": "E" * 32}
    result = match_one(BASE_INFO, DETAILS, live)
    assert (result.level, result.rule_id) == ("normalized", "iqiyi-video-base-info")


def test_video_details_match_across_a_new_device_id() -> None:
    live = {**DETAILS, "device_id": "fedcba9876543210fedcba9876543210", "sign": "E" * 32}
    assert match_one(BASE_INFO, DETAILS, live).rule_id == "iqiyi-video-base-info"


def test_video_details_match_without_the_ad_network_token() -> None:
    recorded = {**DETAILS, "adn_token": "{}"}
    live = {**DETAILS, "adn_token": "", "timestamp": "1700000007777", "sign": "E" * 32}
    result = match_one(BASE_INFO, recorded, live)
    assert (result.reason, result.rule_id) == (None, "iqiyi-video-base-info")


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("entity_id", "2000000000000000"),
        ("user_id", "12345"),
        ("vip_type", "1"),
        ("app_version", "17.104.0"),
    ],
)
def test_video_details_still_select_by_video_and_user(field: str, other: str) -> None:
    live = {**DETAILS, "timestamp": "1700000007777", "sign": "E" * 32, field: other}
    assert match_one(BASE_INFO, DETAILS, live).reason == "no_match"


@pytest.mark.parametrize(
    ("method", "address"),
    [
        ("POST", REDIRECT),
        ("GET", "https://www.iqiyi.com:444/tvg/to_page_url"),
        ("GET", "https://www.iqiyi.com/prelw/tvg/v2/lw/base_info"),
        ("GET", "https://www.iqiyi.com/prelw/tvg/lw/video_metadata"),
        ("GET", "https://mesh.if.iqiyi.com/tvg/lw/ask_after_play"),
        ("GET", "https://mesh.if.iqiyi.com/tvg/ad/lw/feature_feed_ad"),
        ("GET", "https://mesh.if.iqiyi.com/tvg/v2/selector"),
        ("GET", "https://pcw-api.iqiyi.com/tvg/to_page_url"),
        ("GET", "https://cache.video.iqiyi.com/dash"),
    ],
)
def test_other_requests_keep_time_signature_and_trace(method: str, address: str) -> None:
    policy = rules().require_for_request(method, address)
    for field in ("timestamp", "sign", "ext_params"):
        assert not policy.ignores_query(field)


# ── the visitor device id and module cache busters ──────────────────────

MESH = "https://mesh.if.iqiyi.com"
PROXY = "https://www.iqiyi.com/prelw"
RECORDED_DEVICE = "8dae18aefd9ff8d085b8cf8d6ab84300"
LIVE_DEVICE = "85b8cf8d6ab84300fda099bf4fac9d2f"

#: Per rule: the endpoint, a recorded request's fields, what a replay changes, and fields
#: that select content (each changed alone must still miss).
DEVICE_ENDPOINTS: dict[str, tuple[str, dict[str, str], dict[str, str], dict[str, str]]] = {
    "iqiyi-experiment-buckets": (
        f"{MESH}/portal/lw/abtest",
        {"device": RECORDED_DEVICE, "v": "17.103.26647"},
        {"device": LIVE_DEVICE},
        {"v": "17.104.0"},
    ),
    "iqiyi-device-prompt": (
        f"{MESH}/portal/lw/v2/popup/device/check",
        {"deviceId": RECORDED_DEVICE, "v": "17.103.26647"},
        {"deviceId": LIVE_DEVICE},
        {"v": "17.104.0"},
    ),
    "iqiyi-popup-recommendation": (
        f"{MESH}/portal/lw/v2/popup/recommend",
        {"deviceId": RECORDED_DEVICE, "uid": "0"},
        {"deviceId": LIVE_DEVICE},
        {"uid": "12345"},
    ),
    "iqiyi-membership-prompts": (
        f"{MESH}/portal/lw/vip/interact",
        {
            "interfaceCode": "a6b0057cbf43c2b6",
            "deviceID": RECORDED_DEVICE,
            "qyid": RECORDED_DEVICE,
            "timestamp": "1700000000111",
        },
        {"deviceID": LIVE_DEVICE, "qyid": LIVE_DEVICE, "timestamp": "1700000009999"},
        {"interfaceCode": "94cc824e3a0d0f0c"},
    ),
    "iqiyi-membership-prompts-proxy": (
        f"{PROXY}/portal/lw/vip/interact",
        {"interfaceCode": "a6b0057cbf43c2b6", "deviceID": RECORDED_DEVICE, "qyid": RECORDED_DEVICE},
        {"deviceID": LIVE_DEVICE, "qyid": LIVE_DEVICE},
        {"interfaceCode": "94cc824e3a0d0f0c"},
    ),
    "iqiyi-channel-bar": (
        f"{MESH}/portal/lw/v5/channel/navigateBar",
        {"uid": "0", "device": RECORDED_DEVICE, "v": "17.103.26647"},
        {"device": LIVE_DEVICE},
        {"uid": "12345"},
    ),
    "iqiyi-channel-bar-proxy": (
        f"{PROXY}/portal/lw/v5/channel/navigateBar",
        {"uid": "0", "device": RECORDED_DEVICE},
        {"device": LIVE_DEVICE},
        {"uid": "12345"},
    ),
    "iqiyi-channel-badges": (
        f"{MESH}/portal/lw/v5/channel/navigateBar/bubble",
        {"device": RECORDED_DEVICE, "uid": "0"},
        {"device": LIVE_DEVICE},
        {"uid": "12345"},
    ),
    "iqiyi-home-feed": (
        f"{MESH}/portal/lw/v5/channel/recommend",
        {"uid": "0", "device": RECORDED_DEVICE, "width": "1280"},
        {"device": LIVE_DEVICE},
        {"width": "1920"},
    ),
    "iqiyi-home-feed-more": (
        f"{MESH}/portal/lw/v5/channel/recoms",
        {"channelName": "recommend", "device": RECORDED_DEVICE, "page": "1"},
        {"device": LIVE_DEVICE},
        {"channelName": "tv", "page": "2"},
    ),
    "iqiyi-search-placeholder": (
        f"{MESH}/portal/lw/search/keywords/default",
        {"device_id": RECORDED_DEVICE, "channel_id": "", "v": "17.103.26647"},
        {"device_id": LIVE_DEVICE},
        {"channel_id": "2"},
    ),
    "iqiyi-search-placeholder-proxy": (
        f"{PROXY}/portal/lw/search/keywords/default",
        {"device_id": RECORDED_DEVICE, "channel_id": ""},
        {"device_id": LIVE_DEVICE},
        {"channel_id": "2"},
    ),
    "iqiyi-search-hot-list": (
        f"{MESH}/portal/lw/search/keywords/hotList",
        {"device_id": RECORDED_DEVICE, "appMode": ""},
        {"device_id": LIVE_DEVICE},
        {"appMode": "kids"},
    ),
    "iqiyi-search-hot-list-proxy": (
        f"{PROXY}/portal/lw/search/keywords/hotList",
        {"device_id": RECORDED_DEVICE, "appMode": ""},
        {"device_id": LIVE_DEVICE},
        {"appMode": "kids"},
    ),
    "iqiyi-search-suggestions": (
        f"{MESH}/portal/lw/search/searchKeyWord",
        {"key": "流浪", "deviceId": RECORDED_DEVICE},
        {"deviceId": LIVE_DEVICE},
        {"key": "流浪地球"},
    ),
    "iqiyi-search-results": (
        f"{MESH}/portal/lw/search/homePageV3",
        {"key": "流浪地球2", "source": "input", "pageNum": "1", "u": RECORDED_DEVICE},
        {"u": LIVE_DEVICE},
        {"key": "流浪地球", "source": "suggest", "pageNum": "2"},
    ),
    "iqiyi-player-overlays": (
        f"{MESH}/player/lw/display/control/v2",
        {"tvId": "1000", "deviceId": RECORDED_DEVICE, "timestamp": "1700000000111"},
        {"deviceId": LIVE_DEVICE, "timestamp": "1700000009999"},
        {"tvId": "2000"},
    ),
    "iqiyi-app-panel": (
        f"{MESH}/tvg/app_help_info",
        {
            "device_id": RECORDED_DEVICE,
            "timestamp": "1700000000",
            "app_version": "17.103.26647",
            "src": "pca",
            "sign": "0" * 32,
        },
        {"device_id": LIVE_DEVICE, "timestamp": "1700000009", "sign": "F" * 32},
        {"src": "pcw", "app_version": "17.104.0"},
    ),
    "iqiyi-game-layer": (
        f"{MESH}/tvg/lw/layer_game",
        {"device_id": RECORDED_DEVICE, "timestamp": "1700000000", "sign": "0" * 32, "src": "pca"},
        {"device_id": LIVE_DEVICE, "timestamp": "1700000009", "sign": "F" * 32},
        {"src": "pcw"},
    ),
    "iqiyi-poster-badges": (
        f"{MESH}/player/lw/video/mark_type/info",
        {"module": "2", "version": "17.103.26647", "timestamp": "1700000000", "sign": "0" * 32},
        {"timestamp": "1700000009", "sign": "f" * 32},
        {"module": "3", "version": "17.104.0"},
    ),
    "iqiyi-search-module-config": (
        "https://static.iqiyi.com/ext/config/xfind/v1.json",
        {"t": "1700000000111"},
        {"t": "hzo5Hgo3"},
        {},
    ),
    "iqiyi-player-module-config": (
        "https://static.iqiyi.com/ext/config/xplayer/v4.json",
        {"t": "1700000000111"},
        {"t": "7zk77iyA"},
        {},
    ),
}


def match_get(base: str, recorded: dict[str, str], live: dict[str, str]) -> MatchResult:
    policies = rules()
    address = url(base, recorded)
    record = indexed(
        address, record_id="recorded", order=0, rule=policies.require_for_request("GET", address)
    )
    return Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("GET", url(base, live))
    )


@pytest.mark.parametrize("rule_id", sorted(DEVICE_ENDPOINTS))
def test_a_new_device_id_or_cache_buster_finds_the_recorded_request(rule_id: str) -> None:
    base, recorded, changed, _selectors = DEVICE_ENDPOINTS[rule_id]
    result = match_get(base, recorded, {**recorded, **changed})
    assert (result.reason, result.level, result.rule_id) == (None, "normalized", rule_id)


@pytest.mark.parametrize(
    ("rule_id", "field"),
    [(rule_id, field) for rule_id, spec in sorted(DEVICE_ENDPOINTS.items()) for field in spec[3]],
)
def test_content_fields_still_select(rule_id: str, field: str) -> None:
    base, recorded, changed, selectors = DEVICE_ENDPOINTS[rule_id]
    live = {**recorded, **changed, field: selectors[field]}
    assert match_get(base, recorded, live).reason == "no_match", f"{rule_id} must keep {field}"


GRADING = f"{MESH}/player/client/device/asset/grading"
MODULES = {
    "cid": RECORDED_DEVICE,
    "mode": "simple",
    "projects": [{"p": "pcafx", "v": 1}, {"p": "xfind", "v": 1}, {"p": "xplayer", "v": 1}],
}


def match_grading(recorded: dict[str, Any], live: dict[str, Any]) -> MatchResult:
    policies = rules()
    record = indexed(
        GRADING,
        record_id="recorded",
        order=0,
        method="POST",
        body=json.dumps(recorded).encode(),
        content_type="application/json",
        rule=policies.require_for_request("POST", GRADING),
    )
    return Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest(
            "POST", GRADING, {"Content-Type": "application/json"}, json.dumps(live).encode()
        )
    )


def test_module_grading_matches_across_a_new_device_id() -> None:
    result = match_grading(MODULES, {**MODULES, "cid": LIVE_DEVICE})
    assert (result.reason, result.rule_id) == (None, "iqiyi-module-grading")


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "full"},
        {"projects": [{"p": "pcafx", "v": 2}, {"p": "xfind", "v": 1}, {"p": "xplayer", "v": 1}]},
        {"projects": [{"p": "pcafx", "v": 1}]},
    ],
)
def test_module_grading_still_selects_by_mode_and_projects(change: dict[str, Any]) -> None:
    live = {**MODULES, "cid": LIVE_DEVICE, **change}
    assert match_grading(MODULES, live).reason == "no_match"


@pytest.mark.parametrize(
    "address",
    [
        f"{MESH}/portal/lw/v7/channel/vip",
        f"{MESH}/portal/lw/search/homePageV2",
        f"{PROXY}/portal/lw/search/homePageV3",
        f"{PROXY}/player/lw/display/control/v2",
        "https://static.iqiyi.com/ext/config/other/v1.json",
    ],
)
def test_other_requests_keep_the_device_id_and_cache_buster(address: str) -> None:
    policy = rules().require_for_request("GET", address)
    for field in ("device", "deviceId", "device_id", "deviceID", "qyid", "u", "t"):
        assert not policy.ignores_query(field), (address, field)


# ── play page requests signed per page load, channel pages' ad-network token ──

VIDEO = "1000000000000000"
SIGNED = {"timestamp": "1700000000111", "sign": "0" * 32}
RESIGNED = {"timestamp": "1700000009999", "sign": "F" * 32}

#: Same shape as DEVICE_ENDPOINTS: endpoint, recorded fields, what a replay changes, and fields
#: that select content (each changed alone must still miss).
PLAY_PAGE_ENDPOINTS: dict[str, tuple[str, dict[str, str], dict[str, str], dict[str, str]]] = {
    "iqiyi-tv-channel": (
        f"{MESH}/portal/lw/v7/channel/tv",
        {"uid": "0", "device": RECORDED_DEVICE, "page": "1", "adnToken": "{}"},
        {"adnToken": ""},
        {"page": "2", "uid": "12345", "device": LIVE_DEVICE},
    ),
    "iqiyi-vip-channel": (
        f"{MESH}/portal/lw/v7/channel/vip",
        {"uid": "0", "device": RECORDED_DEVICE, "page": "1", "adnToken": "{}"},
        {"adnToken": ""},
        {"page": "2", "uid": "12345"},
    ),
    "iqiyi-play-history": (
        f"{MESH}/tvg/my/lw/v2/play_history",
        {
            "device_id": RECORDED_DEVICE,
            "page_size": "100",
            "page_num": "1",
            "only_long": "1",
            "user_id": "",
            **SIGNED,
        },
        {"device_id": LIVE_DEVICE, **RESIGNED},
        {"page_num": "2", "page_size": "10", "only_long": "2", "user_id": "12345"},
    ),
    "iqiyi-video-metadata": (
        f"{MESH}/tvg/lw/video_metadata",
        {"videoIds": VIDEO, "deviceId": RECORDED_DEVICE, "appVersion": "17.103.26647", **SIGNED},
        {"deviceId": LIVE_DEVICE, **RESIGNED},
        {"videoIds": "2000000000000000", "appVersion": "17.104.0"},
    ),
    "iqiyi-play-control": (
        f"{MESH}/tvg/lw/async_pr_control",
        {"device_id": RECORDED_DEVICE, "app_version": "17.103.26647", "src": "pca", **SIGNED},
        {"device_id": LIVE_DEVICE, **RESIGNED},
        {"app_version": "17.104.0", "src": "pcw"},
    ),
    "iqiyi-player-tips": (
        f"{MESH}/player/lw/play/tips",
        {"device_id": RECORDED_DEVICE, "app_version": "17.103.26647", **SIGNED},
        {"device_id": LIVE_DEVICE, **RESIGNED},
        {"app_version": "17.104.0"},
    ),
    "iqiyi-player-loading-image": (
        f"{MESH}/player/lw/play/img/loading",
        {"version": "17.103.26647", "timestamp": "1700000000", "sign": "0" * 32},
        {"timestamp": "1700000009", "sign": "f" * 32},
        {"version": "17.104.0"},
    ),
    "iqiyi-after-play-control": (
        f"{MESH}/tvg/lw/after_play_control",
        {"videoId": VIDEO, "deviceId": RECORDED_DEVICE, "appVersion": "17.103.26647", **SIGNED},
        {"deviceId": LIVE_DEVICE, **RESIGNED},
        {"videoId": "2000000000000000", "appVersion": "17.104.0"},
    ),
    "iqiyi-watch-report": (
        f"{MESH}/tvg/realtime_action",
        {"type": "2", "content": VIDEO, "rpage": "pcw_playpage", "play_time": "5000", **SIGNED},
        RESIGNED,
        {"content": "2000000000000000", "rpage": "sukan_ply", "play_time": "10000", "type": "1"},
    ),
    **{
        rule_id: (
            f"{MESH}/player/w/video/{path}",
            {
                "id": VIDEO,
                "version": "17.103.26647",
                "rpage": "pcw_playpage",
                "block": "",
                "s2": "3",
            }
            | {"timestamp": "1700000000111", "sign": "0" * 32},
            {"s2": "", "timestamp": "1700000009999", "sign": "f" * 32},
            {"id": "2000000000000000", "rpage": "sukan_ply", "block": "b1", "version": "17.104.0"},
        )
        for rule_id, path in (
            ("iqiyi-video-like-switch", "like/control"),
            ("iqiyi-video-heat-switch", "vv_heat/control"),
            ("iqiyi-video-comment-switch", "comment/control"),
            ("iqiyi-video-danmaku-switch", "danmu/info"),
        )
    },
    "iqiyi-play-page-module-config": (
        "https://static.iqiyi.com/ext/config/tvg/v1.json",
        {"t": "wKtKOM2B"},
        {"t": "F4QkQxsD"},
        {},
    ),
    "iqiyi-interactive-layer": (
        "https://iface2.iqiyi.com/ivos/interact/video/data",
        {
            "tv_id": VIDEO,
            "play_mode": "1",
            "platform_id": "1079",
            "app_v": "17.103.26647",
            "qyid": RECORDED_DEVICE,
            "req_sn": "1700000000111",
        },
        {"qyid": LIVE_DEVICE, "req_sn": "1700000009999"},
        {"tv_id": "2000000000000000", "play_mode": "2", "platform_id": "1075", "app_v": "17.104.0"},
    ),
    "iqiyi-follow-state": (
        "https://subscription.iqiyi.com/services/subscribe/countAndState.htm",
        {"subType": "2", "subKeys": "3000000000000000", "agentType": "1", "dfp": "a0" * 30},
        {"dfp": ""},
        {"subKeys": "4000000000000000", "subType": "1", "agentType": "2"},
    ),
    "iqiyi-detail-channel-bar": (
        f"{MESH}/portal/lw/v5/detail/navigateBar",
        {"uid": "0", "vip": "0", "auth": "", "device": RECORDED_DEVICE, "v": "17.103.26647"},
        {"device": LIVE_DEVICE},
        {"uid": "12345", "vip": "1", "v": "17.104.0"},
    ),
    "iqiyi-episode-recommendation": (
        f"{MESH}/tvg/lw/after_episode_feature",
        {
            "deviceId": RECORDED_DEVICE,
            "userId": "0",
            "src": "pcw_tvg_hot",
            "channelId": "1",
            "episodeId": VIDEO,
            "filterIds": VIDEO,
            **SIGNED,
        },
        RESIGNED,
        {
            "episodeId": "2000000000000000",
            "channelId": "2",
            "filterIds": "252449101",
            "userId": "1",
        },
    ),
    "iqiyi-recommended-videos": (
        f"{MESH}/tvg/tvg_v2/rcd_videos",
        {
            "episodeId": VIDEO,
            "albumId": "252449101",
            "retNum": "10",
            "offset": "10",
            "previousPageSize": "13",
            "lastFeedAdIndex": "7",
            "templateId": "album_template",
            "adParam": "",
            **SIGNED,
        },
        RESIGNED,
        {
            "episodeId": "2000000000000000",
            "albumId": "252449102",
            "offset": "20",
            "previousPageSize": "23",
            "templateId": "film_template",
            "adParam": '[{"azt":"612"}]',
        },
    ),
    "iqiyi-comment-counts": (
        "https://sns-comment.iqiyi.com/v3/comment/get_bulk_count_data.action",
        {"content_ids": VIDEO, "business_type": "17", "agent_type": "30", "timestamp": "1"},
        {"timestamp": "2"},
        {"content_ids": "2000000000000000", "business_type": "18"},
    ),
}


@pytest.mark.parametrize("rule_id", sorted(PLAY_PAGE_ENDPOINTS))
def test_a_play_page_request_signed_again_finds_the_recorded_one(rule_id: str) -> None:
    base, recorded, changed, _selectors = PLAY_PAGE_ENDPOINTS[rule_id]
    result = match_get(base, recorded, {**recorded, **changed})
    assert (result.reason, result.level, result.rule_id) == (None, "normalized", rule_id)


@pytest.mark.parametrize(
    ("rule_id", "field"),
    [
        (rule_id, field)
        for rule_id, spec in sorted(PLAY_PAGE_ENDPOINTS.items())
        for field in spec[3]
    ],
)
def test_play_page_content_fields_still_select(rule_id: str, field: str) -> None:
    base, recorded, changed, selectors = PLAY_PAGE_ENDPOINTS[rule_id]
    live = {**recorded, **changed, field: selectors[field]}
    assert match_get(base, recorded, live).reason == "no_match", f"{rule_id} must keep {field}"


@pytest.mark.parametrize("rule_id", ["iqiyi-tv-channel", "iqiyi-vip-channel"])
def test_a_channel_page_without_the_token_finds_the_recorded_one(rule_id: str) -> None:
    """Offline the page leaves adnToken out instead of sending it empty."""
    base, recorded, _changed, _selectors = PLAY_PAGE_ENDPOINTS[rule_id]
    live = {name: value for name, value in recorded.items() if name != "adnToken"}
    assert match_get(base, recorded, live).rule_id == rule_id
    assert match_get(base, recorded, live).reason is None


@pytest.mark.parametrize(
    ("address", "fields"),
    [
        (f"{MESH}/portal/lw/v7/channel/recommend", ("adnToken",)),
        (f"{MESH}/portal/lw/v5/channel/recommend", ("adnToken", "ad", "page")),
        (f"{MESH}/tvg/ad/lw/feature_feed_ad", ("adn_token", "timestamp", "sign")),
        (f"{MESH}/tvg/lw/ask_after_play", ("timestamp", "sign")),
        (f"{PROXY}/player/w/video/like/control", ("s2", "timestamp", "sign")),
        (f"{MESH}/player/w/video/other/control", ("s2", "timestamp", "sign")),
        ("https://iface2.iqiyi.com/ivos/interact/video/summary", ("req_sn",)),
        ("https://subscription.iqiyi.com/services/subscribe/add.htm", ("dfp",)),
    ],
)
def test_neighbouring_requests_keep_their_fields(address: str, fields: tuple[str, ...]) -> None:
    policy = rules().require_for_request("GET", address)
    for field in fields:
        assert not policy.ignores_query(field), (address, field)


# ── player stream addresses ─────────────────────────────────────────────

DASH = "https://cache.video.iqiyi.com/dash"
STREAMS = {
    "tvid": "1000000000000000",
    "bid": "600",
    "src": "01080031010000000000",
    "k_uid": RECORDED_DEVICE,
    "authKey": "0" * 32,
    "dfp": "a0" * 30,
    "tm": "1700000000111",
    "bop": json.dumps({"version": "10.0", "dfp": "a0" * 30, "b_ft1": 60}, separators=(",", ":")),
    "ut": "0",
    "vf": "1" * 32,
}
#: The replayed player: a new time and signature, a new device id, and no fingerprint yet.
RESTREAMS = {
    **STREAMS,
    "k_uid": LIVE_DEVICE,
    "authKey": "f" * 32,
    "dfp": "",
    "tm": "1700000009999",
    "bop": json.dumps({"version": "10.0", "dfp": "", "b_ft1": 60}, separators=(",", ":")),
    "vf": "e" * 32,
}


def test_stream_addresses_match_across_a_new_time_signature_device_and_fingerprint() -> None:
    result = match_one(DASH, STREAMS, RESTREAMS)
    assert (result.reason, result.rule_id) == (None, "iqiyi-player-streams")


@pytest.mark.parametrize(
    "change",
    [
        {"tvid": "2000000000000000"},
        {"bid": "500"},
        {"src": "01010031010000000000"},
        {"ut": "1"},
        {"bop": json.dumps({"version": "11.0", "dfp": "", "b_ft1": 60}, separators=(",", ":"))},
        {"bop": json.dumps({"version": "10.0", "dfp": "", "b_ft1": 30}, separators=(",", ":"))},
    ],
)
def test_stream_addresses_still_select_the_video_and_its_bitrate(change: dict[str, str]) -> None:
    assert match_one(DASH, STREAMS, {**RESTREAMS, **change}).reason == "no_match"


# ── video segments and the bandwidth probe ──────────────────────────────

SEGMENT = "https://bj3cnc.71edge.com/videos/vts/20261016/52/cd/0000000000000000000000000000aaaa.ts"
#: The CDN address the segment scheduler answered with, plus the byte range the player chose.
SEGMENT_FIELDS = {
    "key": "00976ee4c3686de999015b32f04e66bf2",
    "dis_k": "82112f60c178dc40b35811347a191f22",
    "qd_tvid": "1000000000000000",
    "bid": "600",
    "start": "2871136",
    "end": "3697772",
    "sd": "17160",
    "contentlength": "826636",
    "random": "1700000000698",
}


def test_a_segment_matches_across_a_new_cache_buster() -> None:
    result = match_one(SEGMENT, SEGMENT_FIELDS, {**SEGMENT_FIELDS, "random": "1700000009999"})
    assert (result.reason, result.rule_id) == (None, "iqiyi-video-segments")


@pytest.mark.parametrize(
    "change",
    [{"start": "3697772"}, {"end": "4000000"}, {"contentlength": "1"}, {"key": "1" * 33}],
)
def test_a_segment_still_selects_its_bytes(change: dict[str, str]) -> None:
    live = {**SEGMENT_FIELDS, "random": "1700000009999", **change}
    assert match_one(SEGMENT, SEGMENT_FIELDS, live).reason == "no_match"


def test_another_segment_or_host_family_still_misses() -> None:
    other = SEGMENT.replace("aaaa.ts", "bbbb.ts")
    assert match_one(SEGMENT, SEGMENT_FIELDS, SEGMENT_FIELDS).reason is None
    policies = rules()
    assert policies.require_for_request("GET", other).id == "iqiyi-video-segments"
    for address in (
        "https://bj3cnc.71edge.com/videos/other/20260819/d8/f5/0.f4v",
        "https://bj3cnc.71edge.com.example/videos/vts/x.ts",
        "https://pcw-data.video.iqiyi.com/videos/vts/20261016/52/cd/0.ts",
    ):
        assert not policies.require_for_request("GET", address).ignores_query("random"), address


def test_the_bandwidth_probe_matches_across_a_new_cache_buster() -> None:
    probe = "https://data.video.iqiyi.com/v.f4v"
    result = match_one(probe, {"random": "1700000000698"}, {"random": "1700000009999"})
    assert (result.reason, result.rule_id) == (None, "iqiyi-bandwidth-probe")

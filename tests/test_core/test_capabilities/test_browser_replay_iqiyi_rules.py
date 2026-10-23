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
        ("GET", "https://mesh.if.iqiyi.com/tvg/my/lw/v2/play_history"),
        ("GET", "https://mesh.if.iqiyi.com/tvg/lw/video_metadata"),
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

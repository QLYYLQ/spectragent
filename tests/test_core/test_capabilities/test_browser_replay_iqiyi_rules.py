"""Published iQIYI rules recover re-signed requests without merging different videos.

Ids, trace values and signatures below are synthetic.
"""

from pathlib import Path
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


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("entity_id", "2000000000000000"),
        ("device_id", "fedcba9876543210fedcba9876543210"),
        ("user_id", "12345"),
        ("vip_type", "1"),
        ("app_version", "17.104.0"),
    ],
)
def test_video_details_still_select_by_video_device_and_user(field: str, other: str) -> None:
    live = {**DETAILS, "timestamp": "1700000007777", "sign": "E" * 32, field: other}
    assert match_one(BASE_INFO, DETAILS, live).reason == "no_match"


@pytest.mark.parametrize(
    ("method", "address"),
    [
        ("POST", REDIRECT),
        ("GET", "https://www.iqiyi.com:444/tvg/to_page_url"),
        ("GET", "https://www.iqiyi.com/prelw/tvg/v2/lw/base_info"),
        ("GET", "https://mesh.if.iqiyi.com/tvg/my/lw/v2/play_history"),
        ("GET", "https://mesh.if.iqiyi.com/tvg/lw/layer_game"),
        ("GET", "https://pcw-api.iqiyi.com/tvg/to_page_url"),
        ("GET", "https://cache.video.iqiyi.com/dash"),
    ],
)
def test_other_requests_keep_time_signature_and_trace(method: str, address: str) -> None:
    policy = rules().require_for_request(method, address)
    for field in ("timestamp", "sign", "ext_params"):
        assert not policy.ignores_query(field)

"""Published Bilibili rules recover re-signed profile requests without merging profiles.

All identifiers, signatures and telemetry values below are synthetic.
"""

from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules
from tests._helpers.replay_archive import indexed

PROFILE = "https://api.bilibili.com/x/space/wbi/acc/info"
RECORDED = {
    "mid": "1001",
    "token": "",
    "platform": "web",
    "web_location": "1550101",
    "dm_img_list": "[]",
    "dm_img_str": "c3ludGhldGljLWds",
    "dm_cover_img_str": "c3ludGhldGljLXJlbmRlcmVy",
    "dm_img_inter": '{"ds":[],"wh":[4003,4401,1],"of":[12,24,12]}',
    "w_rid": "0" * 32,
    "wts": "1700000000",
}
#: What a later page load of the same profile sends: new telemetry, hence a new signature.
RELOADED = {
    **RECORDED,
    "dm_img_inter": '{"ds":[],"wh":[4172,4369,22],"of":[189,378,189]}',
    "w_rid": "f" * 32,
}


def rules() -> SiteRules:
    return SiteRules.load(Path(__file__).resolve().parents[3] / "configs" / "replay_rules")


def url(base: str, params: dict[str, str]) -> str:
    return f"{base}?{urlencode(params, quote_via=quote)}"


def recorded(policies: SiteRules, base: str, params: dict[str, str], record_id: str, order: int):
    address = url(base, params)
    return indexed(
        address,
        record_id=record_id,
        order=order,
        rule=policies.require_for_request("GET", address),
    )


def test_reloaded_profile_request_finds_the_recorded_profile() -> None:
    policies = rules()
    record = recorded(policies, PROFILE, RECORDED, "recorded-profile", 0)
    result = Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("GET", url(PROFILE, RELOADED))
    )
    assert result.record is record
    assert result.level == "normalized"
    assert result.rule_id == "bilibili-space-acc-info-signature"


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("mid", "1002"),
        ("wts", "1700086400"),
        ("token", "another-token"),
        ("platform", "h5"),
        ("web_location", "333.1387"),
        ("dm_img_list", '[{"x":1,"y":2,"z":0,"timestamp":1,"type":0}]'),
        ("dm_img_str", "YW5vdGhlci1nbA"),
        ("dm_cover_img_str", "YW5vdGhlci1yZW5kZXJlcg"),
    ],
)
def test_profile_and_other_request_fields_still_select(field: str, other: str) -> None:
    policies = rules()
    record = recorded(policies, PROFILE, RECORDED, "recorded-profile", 0)
    result = Matcher(ArchiveIndex([record]), rules=policies).match(
        LiveRequest("GET", url(PROFILE, {**RELOADED, field: other}))
    )
    assert result.reason == "no_match"


def test_profile_responses_are_served_in_recording_order_then_repeated() -> None:
    policies = rules()
    first = recorded(policies, PROFILE, RECORDED, "first-visit", 0)
    second = recorded(
        policies,
        PROFILE,
        {**RECORDED, "dm_img_inter": '{"ds":[],"wh":[4099,4433,33],"of":[274,548,274]}'},
        "second-visit",
        1,
    )
    matcher = Matcher(ArchiveIndex([first, second]), rules=policies)
    served = [matcher.match(LiveRequest("GET", url(PROFILE, RELOADED))) for _ in range(3)]
    assert [result.record for result in served] == [first, second, second]
    assert [result.exhausted for result in served] == [False, False, True]


@pytest.mark.parametrize(
    ("method", "address"),
    [
        ("POST", PROFILE),
        ("GET", "https://api.bilibili.com:444/x/space/wbi/acc/info"),
        ("GET", "https://www.api.bilibili.com/x/space/wbi/acc/info"),
        ("GET", "https://api.bilibili.com/x/space/wbi/acc/relation"),
        ("GET", "https://api.bilibili.com/x/space/wbi/arc/search"),
        ("GET", "https://api.bilibili.com/x/space/wbi/article"),
        ("GET", "https://api.bilibili.com/x/web-interface/wbi/search/default"),
    ],
)
def test_other_requests_keep_signature_and_telemetry(method: str, address: str) -> None:
    policy = rules().require_for_request(method, address)
    assert not policy.ignores_query("w_rid")
    assert not policy.ignores_query("dm_img_inter")

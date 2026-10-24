"""Published East Money rules recover clock-stamped requests without merging different content.

The captcha script asks for its challenge with jQuery's ``cache: false``, so the request carries
``_``, a counter started at the page clock, and ``request``, an encrypted string whose only
varying part is a random number. The home page's quote widgets are JSONP through jQuery, whose
callback name ``cb`` and ``_`` both count from the page clock. The shared replay clock changes
them on every replay. Field names and shapes follow the recorded requests named in
``configs/replay_rules/eastmoney.com.yaml``; ids, tokens and encrypted values below are synthetic.
"""

from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher, MatchResult
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules
from tests._helpers.replay_archive import indexed

CAPTCHA = "https://smartvcode2.eastmoney.com/Titan/api/captcha"
QUOTES = "https://push2.eastmoney.com/api/qt"
JQUERY = "jQuery3710011704753153026104_"

#: Per rule: the endpoint, a recorded request's fields, the fields a replay changes, and the
#: fields that select content (each changed alone must still miss).
ENDPOINTS: dict[str, tuple[str, dict[str, str], dict[str, str], dict[str, str]]] = {
    "eastmoney-captcha-challenge": (
        f"{CAPTCHA}/get",
        {
            "callback": "cb",
            "ctxid": "a" * 32,
            "request": "c3ludGhldGljLWVuY3J5cHRlZC1yZXF1ZXN0",
            "_": "1700000000000",
        },
        {"request": "YW5vdGhlci1yYW5kb20tbnVtYmVy", "_": "1700000027000"},
        {"ctxid": "b" * 32, "callback": "cb2"},
    ),
    "eastmoney-home-quote-list": (
        f"{QUOTES}/ulist/get",
        {
            "fltt": "1",
            "invt": "2",
            "cb": f"{JQUERY}1700000000830",
            "fields": "f13,f12,f14,f1,f2,f4,f3,f152",
            "secids": "100.HSI,100.N225",
            "ut": "0" * 32,
            "pn": "1",
            "np": "1",
            "pz": "20",
            "_": "1700000000831",
        },
        {"cb": f"{JQUERY}1700000027830", "_": "1700000027831"},
        {"secids": "100.HSI", "fields": "f12,f14", "pz": "10", "ut": "1" * 32, "fltt": "2"},
    ),
    "eastmoney-home-stock-connect": (
        f"{QUOTES}/kamt/get",
        {
            "fields1": "f1,f2,f3,f4,f55",
            "fields2": "f51,f52,f53",
            "ut": "0" * 32,
            "cb": f"{JQUERY}1700000000832",
            "_": "1700000000833",
        },
        {"cb": f"{JQUERY}1700000027832", "_": "1700000027833"},
        {"fields1": "f1", "fields2": "f51", "ut": "1" * 32},
    ),
    "eastmoney-home-board-list": (
        f"{QUOTES}/clist/get",
        {
            "fltt": "1",
            "invt": "2",
            "cb": f"{JQUERY}1700000000836",
            "fields": "f12,f13,f14,f1,f2,f4,f11,f152",
            "fs": "m:0+t:6,m:0+t:80",
            "fid": "f11",
            "po": "1",
            "pn": "1",
            "pz": "3",
            "_": "1700000000837",
        },
        {"cb": f"{JQUERY}1700000027836", "_": "1700000027837"},
        {"fs": "m:90+t:3", "fid": "f3", "po": "0", "pn": "2", "pz": "10"},
    ),
}


def rules() -> SiteRules:
    return SiteRules.load(Path(__file__).resolve().parents[3] / "configs" / "replay_rules")


def url(base: str, params: dict[str, str]) -> str:
    return f"{base}?{urlencode(params, quote_via=quote)}"


def match_one(recorded: str, live: str) -> MatchResult:
    policies = rules()
    record = indexed(
        recorded,
        record_id="recorded",
        order=0,
        method="GET",
        rule=policies.require_for_request("GET", recorded),
    )
    return Matcher(ArchiveIndex([record]), rules=policies).match(LiveRequest("GET", live))


@pytest.mark.parametrize("rule_id", sorted(ENDPOINTS))
def test_a_request_made_at_another_time_finds_the_recorded_one(rule_id: str) -> None:
    base, recorded, changed, _selectors = ENDPOINTS[rule_id]
    result = match_one(url(base, recorded), url(base, {**recorded, **changed}))
    assert (result.reason, result.level, result.rule_id) == (None, "normalized", rule_id)


@pytest.mark.parametrize(
    ("rule_id", "field"),
    [(rule_id, field) for rule_id, spec in sorted(ENDPOINTS.items()) for field in spec[3]],
)
def test_content_fields_still_select(rule_id: str, field: str) -> None:
    base, recorded, changed, selectors = ENDPOINTS[rule_id]
    live = url(base, {**recorded, **changed, field: selectors[field]})
    assert match_one(url(base, recorded), live).reason == "no_match", f"{rule_id} must keep {field}"


@pytest.mark.parametrize(
    "address",
    [
        f"{CAPTCHA}/Validate",
        f"{CAPTCHA}/reset",
        "https://smartvcode2.eastmoney.com:8443/Titan/api/captcha/get",
        f"{QUOTES}/stock/get",
        f"{QUOTES}/slist/get",
        f"{QUOTES}/stock/trends2/get",
        "https://push2his.eastmoney.com/api/qt/ulist/get",
        "https://searchadapter.eastmoney.com/api/NoticeBoard/get",
    ],
)
def test_other_requests_keep_their_clock_fields(address: str) -> None:
    policy = rules().require_for_request("GET", address)
    for field in ("_", "cb", "request"):
        assert not policy.ignores_query(field), (address, field)

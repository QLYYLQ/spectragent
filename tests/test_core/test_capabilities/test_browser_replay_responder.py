"""What a replay sends back, one shape per test (§4.14 "按记录的形态应答").

``responder.py`` is pure: a record (or a range cover, or a preflight, or a
miss decision) in, one ``Fetch.fulfillRequest`` response out. The live
counterpart is ``test_browser_replay_live.py``.
"""

from __future__ import annotations

import base64
import json

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import RangeCover
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest
from spectragent.core.capabilities.browser.replay.miss_policy import MissDecision
from spectragent.core.capabilities.browser.replay.responder import (
    UNKNOWN_STATUS_PHRASE,
    Fulfilment,
    Refusal,
    exceeds_cap,
    format_http_date,
    is_preflight,
    miss_response,
    parse_http_date,
    reason_phrase,
    serve_range,
    serve_record,
    synthetic_preflight,
)
from spectragent.core.capabilities.browser.replay.site_rules import EchoRule, SiteRule
from tests._helpers.replay_archive import indexed

URL = "http://shop.test/api/item?id=1"
#: The record's request start (the WARC-Date source) and its recorded ``Date``.
RECORDED = 1_700_000_000.0
NOW = RECORDED + 30 * 86400.0
CAP = 1024


def _get(url: str = URL, **headers: str) -> LiveRequest:
    return LiveRequest(method="GET", url=url, headers=headers)


def _record(status: int = 200, headers: tuple[tuple[str, str], ...] = (), **fields):
    return indexed(
        URL,
        record_id="rec-1",
        order=0,
        status=status,
        response_headers=headers,
        started_at=RECORDED,
        status_text=fields.pop("status_text", "OK"),
        **fields,
    )


def _serve(record, body: bytes = b"", *, request: LiveRequest | None = None, **kw):
    options = {
        "request": request or _get(),
        "raw_body": None,
        "rule": None,
        "shift_dates": "none",
        "now": NOW,
        "cap": CAP,
    }
    options.update(kw)
    return serve_record(record, body, **options)


def _served(result: Fulfilment | Refusal) -> Fulfilment:
    assert isinstance(result, Fulfilment), result
    return result


def _all(fulfilment: Fulfilment, name: str) -> list[str]:
    return [value for key, value in fulfilment.headers if key.lower() == name.lower()]


# ── recorded responses ────────────────────────────────────────────────


def test_a_recorded_redirect_is_served_with_its_location() -> None:
    """The recorded 3xx goes out as it is; Chrome follows ``Location`` itself."""
    record = _record(302, (("Location", "http://shop.test/landing"),), status_text="Found")
    served = _served(_serve(record))
    assert served.status == 302
    assert served.header("location") == "http://shop.test/landing"
    assert served.body == b""
    assert served.header("content-length") == "0"


def test_transfer_headers_are_dropped_and_length_is_recomputed() -> None:
    record = _record(
        200,
        (
            (":status", "200"),
            ("Content-Type", "text/html"),
            ("Content-Encoding", "gzip"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Length", "999"),
        ),
    )
    served = _served(_serve(record, b"<p>decoded</p>"))
    names = [name.lower() for name, _ in served.headers]
    assert ":status" not in names
    assert "content-encoding" not in names and "transfer-encoding" not in names
    assert _all(served, "content-length") == [str(len(b"<p>decoded</p>"))]
    assert served.header("content-type") == "text/html"


def test_each_set_cookie_is_its_own_entry() -> None:
    record = _record(
        200,
        (
            ("Set-Cookie", "a=1; Path=/\nb=2; Path=/"),
            ("Set-Cookie", "c=3; Secure"),
            ("Date", format_http_date(RECORDED)),
        ),
    )
    served = _served(_serve(record, b"x"))
    assert _all(served, "set-cookie") == ["a=1; Path=/", "b=2; Path=/", "c=3; Secure"]


def test_no_content_length_on_a_bodiless_status() -> None:
    served = _served(_serve(_record(204, (("Date", format_http_date(RECORDED)),))))
    assert served.header("content-length") is None


# ── dates ─────────────────────────────────────────────────────────────


def _dated() -> tuple[tuple[str, str], ...]:
    return (
        ("Date", format_http_date(RECORDED)),
        ("Expires", format_http_date(RECORDED + 3600)),
        ("Last-Modified", format_http_date(RECORDED - 86400)),
        ("Set-Cookie", f"sid=1; Expires={format_http_date(RECORDED + 86400)}; Path=/"),
    )


def test_shift_none_serves_every_date_as_recorded() -> None:
    served = _served(_serve(_record(200, _dated()), b"x", shift_dates="none"))
    for name, value in _dated():
        assert value in _all(served, name)


def test_shift_all_moves_every_date_by_one_delta() -> None:
    """Δ = now − recorded ``Date``: ``Date`` becomes now and the rest keep their offsets."""
    served = _served(_serve(_record(200, _dated()), b"x", shift_dates="all"))
    assert parse_http_date(served.header("date") or "") == NOW
    assert parse_http_date(served.header("expires") or "") == NOW + 3600
    assert parse_http_date(served.header("last-modified") or "") == NOW - 86400
    cookie = _all(served, "set-cookie")[0]
    assert f"Expires={format_http_date(NOW + 86400)}" in cookie
    assert cookie.startswith("sid=1;") and cookie.endswith("; Path=/")


def test_without_a_recorded_date_the_delta_comes_from_the_request_start() -> None:
    headers = (("Expires", format_http_date(RECORDED + 60)),)
    served = _served(_serve(_record(200, headers), b"x", shift_dates="all"))
    assert parse_http_date(served.header("expires") or "") == NOW + 60
    assert parse_http_date(served.header("date") or "") == NOW


@pytest.mark.parametrize(("mode", "expected"), [("none", RECORDED), ("all", NOW)])
def test_a_missing_date_is_synthesised(mode: str, expected: float) -> None:
    """Chrome rebases cookie expiry by the served ``Date``; without one it uses the real clock."""
    served = _served(_serve(_record(200, ()), b"x", shift_dates=mode))
    assert _all(served, "date") and parse_http_date(served.header("date") or "") == expected


def test_a_value_that_is_not_a_date_is_served_as_recorded() -> None:
    headers = (
        ("Date", format_http_date(RECORDED)),
        ("Expires", "0"),
        ("Set-Cookie", "x=1; Expires=never; Path=/"),
    )
    served = _served(_serve(_record(200, headers), b"x", shift_dates="all"))
    assert served.header("expires") == "0"
    assert _all(served, "set-cookie") == ["x=1; Expires=never; Path=/"]


# ── the cap, echo, 304 ────────────────────────────────────────────────


def test_a_body_over_the_cap_is_refused_as_too_large() -> None:
    assert _serve(_record(200, ()), b"x" * (CAP + 1)) == Refusal("too_large")
    assert isinstance(_serve(_record(200, ()), b"x" * CAP), Fulfilment)
    assert exceeds_cap(CAP + 1, CAP) and not exceeds_cap(CAP, CAP) and not exceeds_cap(None, CAP)


def test_response_echo_copies_the_request_id() -> None:
    rule = SiteRule(
        id="request-echo",
        exhaustion="repeat_last",
        unavailable="miss",
        response_echo=(
            EchoRule(url="http://shop.test/api/*", request_path="id", response_path="echo"),
        ),
    )
    request = LiveRequest(
        method="POST", url=URL, headers={"Content-Type": "application/json"}, body=b"{}"
    )
    served = _served(
        _serve(
            _record(200, (("Content-Type", "application/json"),)),
            json.dumps({"echo": "recorded-7", "data": 1}).encode(),
            request=request,
            raw_body=json.dumps({"id": "live-42"}).encode(),
            rule=rule,
        )
    )
    assert json.loads(served.body) == {"echo": "live-42", "data": 1}
    assert served.header("content-length") == str(len(served.body))


# ── JSONP callback names (jsonp.py; the shapes are in test_browser_replay_jsonp.py) ──

JSONP_RECORDED = "http://shop.test/api/hot?callback=jQuery1_1791958613693&_=5"
JSONP_ASKED = "http://shop.test/api/hot?callback=jQuery1_17919586136951&_=6"
JSONP_BODY = b'jQuery1_1791958613693({"hot":["a","b"]});'
#: Headers vouching for the recorded bytes, and one that does not.
DIGESTS = (
    ("Content-MD5", "Q2hlY2sgSW50ZWdyaXR5IQ=="),
    ("Digest", "sha-256=X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE="),
    ("Content-Digest", "sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:"),
    ("Repr-Digest", "sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:"),
    ("ETag", '"v1"'),
)


def _jsonp_record(headers: tuple[tuple[str, str], ...] = ()):
    return indexed(
        JSONP_RECORDED,
        record_id="jsonp-1",
        order=0,
        status=200,
        response_headers=(("Content-Type", "text/plain;charset=UTF-8"), *headers),
        started_at=RECORDED,
        status_text="OK",
    )


def _script(url: str = JSONP_ASKED, resource_type: str = "Script") -> LiveRequest:
    return LiveRequest(method="GET", url=url, resource_type=resource_type)


def test_a_jsonp_answer_calls_the_name_this_request_asks_for() -> None:
    served = _served(_serve(_jsonp_record(), JSONP_BODY, request=_script()))
    assert served.body == b'jQuery1_17919586136951({"hot":["a","b"]});'
    assert served.header("content-length") == str(len(served.body)) == str(len(JSONP_BODY) + 1)
    assert served.callback_rewrite is not None
    assert served.callback_rewrite.model_dump() == {
        "parameter": "callback",
        "recorded": "jQuery1_1791958613693",
        "requested": "jQuery1_17919586136951",
    }


def test_a_rewritten_body_drops_the_recorded_digests_and_keeps_the_etag() -> None:
    record = _jsonp_record(DIGESTS)
    rewritten = _served(_serve(record, JSONP_BODY, request=_script()))
    names = {name.lower() for name, _ in rewritten.headers}
    assert not names & {"content-md5", "digest", "content-digest", "repr-digest"}
    assert rewritten.header("etag") == '"v1"'
    as_recorded = _served(_serve(record, JSONP_BODY, request=_script(JSONP_RECORDED)))
    assert as_recorded.body == JSONP_BODY and as_recorded.callback_rewrite is None
    for name, value in DIGESTS:
        assert as_recorded.header(name) == value, "the recorded bytes keep their digests"


def test_a_jsonp_body_is_not_rewritten_for_a_document() -> None:
    served = _served(_serve(_jsonp_record(), JSONP_BODY, request=_script(resource_type="Document")))
    assert served.body == JSONP_BODY and served.callback_rewrite is None


def test_the_cap_is_checked_against_the_rewritten_bytes() -> None:
    record = _jsonp_record()
    padded = JSONP_BODY + b" " * (CAP - len(JSONP_BODY))
    assert len(padded) == CAP
    assert _served(_serve(record, padded, request=_script(JSONP_RECORDED))).body == padded
    assert _serve(record, padded, request=_script()) == Refusal("too_large")


def test_304_never_answers_an_unconditional_request() -> None:
    not_modified = _record(304, (("ETag", '"v1"'),), status_text="Not Modified")
    assert _serve(not_modified) == Refusal("no_match")
    conditional = _get(**{"If-None-Match": '"v1"'})
    assert conditional.conditional and not _get().conditional
    served = _served(_serve(not_modified, request=conditional))
    assert served.status == 304 and served.header("etag") == '"v1"'


# ── ranges ────────────────────────────────────────────────────────────


def _video(length: int = 100):
    return indexed(
        "http://shop.test/clip.mp4",
        record_id="vid",
        order=0,
        status=200,
        response_headers=(("Content-Type", "video/mp4"), ("Content-Length", str(length))),
        payload_length=length,
        started_at=RECORDED,
    )


def test_a_range_request_gets_206_never_200() -> None:
    record = _video()
    cover = RangeCover(start=10, end=99, total=100, parts=((record, 10, 90),))
    served = serve_range(cover, bytes(range(10, 100)), shift_dates="none", now=NOW)
    assert served.status == 206
    assert served.header("content-range") == "bytes 10-99/100"
    assert served.header("accept-ranges") == "bytes"
    assert _all(served, "content-length") == ["90"]
    assert served.header("content-type") == "video/mp4"


def test_a_range_answer_is_clipped_to_the_cap() -> None:
    record = _video()
    cover = RangeCover(start=0, end=99, total=100, parts=((record, 0, 100),)).clipped(16)
    served = serve_range(cover, bytes(16), shift_dates="none", now=NOW)
    assert served.header("content-range") == "bytes 0-15/100"
    assert len(served.body) == 16


def test_an_unknown_total_is_a_star() -> None:
    record = _video()
    cover = RangeCover(start=0, end=9, total=None, parts=((record, 0, 10),))
    served = serve_range(cover, bytes(10), shift_dates="none", now=NOW)
    assert served.header("content-range") == "bytes 0-9/*"


# ── preflights ────────────────────────────────────────────────────────


def _preflight(**headers: str) -> LiveRequest:
    return LiveRequest(method="OPTIONS", url="http://api.other.test/v1", headers=headers)


def test_a_preflight_is_known_by_method_and_header() -> None:
    assert is_preflight(_preflight(**{"Access-Control-Request-Method": "PUT"}))
    assert not is_preflight(_preflight())
    assert not is_preflight(_get(**{"Access-Control-Request-Method": "PUT"}))


def test_an_unarchived_preflight_gets_a_204_allowing_what_it_asks() -> None:
    served = synthetic_preflight(
        _preflight(
            Origin="http://shop.test",
            **{
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type,x-custom",
            },
        )
    )
    assert served.status == 204 and served.body == b""
    assert served.header("access-control-allow-origin") == "http://shop.test"
    assert served.header("access-control-allow-credentials") == "true"
    assert served.header("access-control-allow-methods") == "PUT"
    assert served.header("access-control-allow-headers") == "content-type,x-custom"
    assert served.header("vary") == "Origin"
    assert "*" not in [value for _, value in served.headers]


# ── misses, and the wire shape ────────────────────────────────────────


def test_a_fulfilled_miss_is_the_policy_answer_with_no_body() -> None:
    decision = MissDecision(
        request_class="subresource", answer="fulfill", status=404, headers=(("x-replay-miss", "1"),)
    )
    served = miss_response(decision)
    assert served.status == 404 and served.body == b""
    assert served.header("x-replay-miss") == "1" and served.header("content-length") == "0"
    with pytest.raises(ValueError, match="fail"):
        miss_response(MissDecision(request_class="document", answer="fail", error_reason="Failed"))


def test_command_params() -> None:
    served = Fulfilment(
        status=200, headers=(("A", "1"), ("A", "2")), body=b"\x00hi", status_text="OK"
    )
    params = served.command_params("req-1")
    assert params == {
        "requestId": "req-1",
        "responseCode": 200,
        "responseHeaders": [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}],
        "body": base64.b64encode(b"\x00hi").decode("ascii"),
        "responsePhrase": "OK",
    }


@pytest.mark.parametrize(
    ("status", "recorded", "sent"),
    [
        (200, "OK", "OK"),
        (404, "Nope", "Nope"),
        # HTTP/2 carries no phrase: the standard one, never an empty phrase Chrome refuses.
        (200, "", "OK"),
        (404, "", "Not Found"),
        (425, "", "Too Early"),
        # ctrip's document: a code with no standard phrase, recorded over HTTP/2.
        (432, "", UNKNOWN_STATUS_PHRASE),
        (599, "", UNKNOWN_STATUS_PHRASE),
        # A recorded phrase a status line does not carry everywhere is not sent as it is.
        (432, "Nicht gut é", UNKNOWN_STATUS_PHRASE),
        (500, "bad\tphrase", "Internal Server Error"),
    ],
)
def test_every_fulfilment_carries_a_phrase_chrome_accepts(
    status: int, recorded: str, sent: str
) -> None:
    assert reason_phrase(status, recorded) == sent
    params = Fulfilment(status=status, headers=(), status_text=recorded).command_params("r")
    assert params["responsePhrase"] == sent

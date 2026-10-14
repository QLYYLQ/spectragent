"""JSONP callback names in a served response (§4.14 "JSONP 回调名").

``jsonp.py`` is pure: a recorded body, the recorded request's URL and the
request being answered in; the body calling this request's name out — or
``None``, and the body is served as recorded. One test per wrapper shape, one
per reason the body is left alone. The responder's side (``Content-Length``,
digests) is in ``test_browser_replay_responder.py``, the log entry in
``test_browser_replay_server.py`` and the real browser in
``test_browser_replay_jsonp_live.py``.
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser.replay.jsonp import (
    CallbackRewrite,
    callback_parameter,
    is_callback_name,
    leading_call,
    may_be_script,
    rewrite_callback,
)

RECORDED = "jQuery1710062_1791958613693"
ASKED = "jQuery1710062_1791958613695"
RECORDED_URL = f"https://api.test/hot?size=10&callback={RECORDED}&_=1791958613690"
REQUEST_URL = f"https://api.test/hot?size=10&callback={ASKED}&_=1791958613705"
DATA = b'{"items":["\xe4\xb8\x80","two"]}'


def _rewrite(
    body: bytes,
    *,
    recorded_url: str = RECORDED_URL,
    request_url: str = REQUEST_URL,
    resource_type: str | None = "Script",
    content_type: str | None = "application/javascript; charset=utf-8",
) -> tuple[bytes, CallbackRewrite] | None:
    return rewrite_callback(
        body,
        recorded_url=recorded_url,
        request_url=request_url,
        resource_type=resource_type,
        content_type=content_type,
    )


def _with(name: str, template: bytes) -> bytes:
    return template.replace(b"NAME", name.encode("ascii"))


# ── the wrapper shapes ──────────────────────────────────────────────────

SHAPES = {
    "bare call": b"NAME(" + DATA + b")",
    "with a semicolon": b"NAME(" + DATA + b");",
    "space before the parenthesis": b"NAME (" + DATA + b")",
    "leading whitespace": b" \r\n\tNAME(" + DATA + b")",
    "byte order mark": b"\xef\xbb\xbfNAME(" + DATA + b")",
    "empty block comment": b"/**/NAME(" + DATA + b");",
    "block comment and whitespace": b"/* jsonp */\n NAME(" + DATA + b")",
    "line comment": b"// API callback\nNAME(" + DATA + b");",
    "leading semicolon": b";NAME(" + DATA + b")",
    "express typeof guard": b"/**/ typeof NAME === 'function' && NAME(" + DATA + b");",
    "typeof guard, == and double quotes": b'typeof NAME=="function"&&NAME(' + DATA + b")",
    "name guard": b"NAME && NAME(" + DATA + b")",
    "try block": b"try{NAME(" + DATA + b")}catch(e){}",
    "try block with a typeof guard": (
        b"try { typeof NAME === 'function' && NAME(" + DATA + b"); } catch (e) {}"
    ),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_each_wrapper_calls_the_name_this_request_asks_for(shape: str) -> None:
    template = SHAPES[shape]
    result = _rewrite(_with(RECORDED, template))
    assert result is not None, shape
    body, rewrite = result
    assert body == _with(ASKED, template), "every occurrence of the wrapper, nothing else"
    assert rewrite == CallbackRewrite(parameter="callback", recorded=RECORDED, requested=ASKED)


def test_a_dotted_name_is_one_name() -> None:
    recorded_url = "https://api.test/d?callback=angular.callbacks._0"
    request_url = "https://api.test/d?callback=angular.callbacks._3"
    result = _rewrite(
        b"angular.callbacks._0(" + DATA + b")", recorded_url=recorded_url, request_url=request_url
    )
    assert result is not None
    assert result[0] == b"angular.callbacks._3(" + DATA + b")"


def test_a_longer_and_a_shorter_name_keep_every_other_byte() -> None:
    body = _with(RECORDED, SHAPES["express typeof guard"])
    for asked in ("cb", "jsonp_" + "9" * 60):
        request_url = f"https://api.test/hot?size=10&callback={asked}"
        result = _rewrite(body, request_url=request_url)
        assert result is not None
        assert result[0] == _with(asked, SHAPES["express typeof guard"])
        assert len(result[0]) == len(body) + 2 * (len(asked) - len(RECORDED))


def test_any_parameter_name_that_carries_the_called_name() -> None:
    recorded_url = "https://ptlogin.test/check?format=jsonp&method=loginHandler7&appid=10"
    request_url = "https://ptlogin.test/check?format=jsonp&method=loginHandler9&appid=10"
    result = _rewrite(b"loginHandler7({})", recorded_url=recorded_url, request_url=request_url)
    assert result is not None
    assert result[1].parameter == "method"
    assert result[0] == b"loginHandler9({})"


def test_a_body_in_another_charset_keeps_its_bytes() -> None:
    gbk = "热门搜索".encode("gbk")
    result = _rewrite(
        _with(RECORDED, b'NAME({"t":"' + gbk + b'"})'), content_type="text/html; charset=GBK"
    )
    assert result is not None
    assert result[0] == _with(ASKED, b'NAME({"t":"' + gbk + b'"})')


# ── left as recorded ────────────────────────────────────────────────────


def test_the_same_name_is_not_rewritten() -> None:
    assert _rewrite(_with(RECORDED, SHAPES["bare call"]), request_url=RECORDED_URL) is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"callback(" + DATA + b")", id="a fixed name the server always writes"),
        pytest.param(
            b"/**/ typeof callback === 'function' && callback(" + DATA + b")",
            id="a guarded fixed name",
        ),
        pytest.param(b"LETV.Plugin.AD.resoJsonp(" + DATA + b")", id="a fixed dotted name"),
        pytest.param(b"var data = NAME(" + DATA + b")", id="the name called later, not first"),
        pytest.param(b"other(NAME(" + DATA + b"))", id="the name as an argument"),
        pytest.param(b"NAME.call(null, " + DATA + b")", id="a member of the name"),
        pytest.param(b"NAME = " + DATA, id="an assignment"),
        pytest.param(
            b"typeof NAME === 'function' && other(" + DATA + b")", id="guard and call differ"
        ),
        pytest.param(b"NAME && other(" + DATA + b")", id="name guard and call differ"),
        pytest.param(b"typeof NAME === 'object' && NAME(" + DATA + b")", id="not a function guard"),
        pytest.param(b"/* never closed NAME(", id="an unclosed comment"),
        pytest.param(b"// NAME(" + DATA, id="the call inside a comment"),
        pytest.param(b"NAMEX(" + DATA + b")", id="a longer name"),
        pytest.param(DATA, id="a JSON body"),
        pytest.param(b'{"callback":"NAME"}', id="JSON naming the callback"),
        pytest.param(b"<html><script>NAME(1)</script></html>", id="an HTML document"),
        pytest.param(b"", id="an empty body"),
    ],
)
def test_a_body_that_is_not_a_wrapper_of_the_recorded_name(body: bytes) -> None:
    assert _rewrite(_with(RECORDED, body)) is None


@pytest.mark.parametrize(
    "content_type",
    ["image/gif", "image/png", "video/mp4", "audio/mpeg", "font/woff2", "text/css", "text/csv"],
)
def test_a_body_that_is_not_a_script_by_its_type(content_type: str) -> None:
    # A GIF's first bytes read "GIF89a(" when its width is 40 px.
    recorded_url = "https://img.test/p.gif?v=GIF89a"
    request_url = "https://img.test/p.gif?v=GIF89b"
    body = b"GIF89a(\x00\x01\x00\x80\x00\x00"
    assert _rewrite(body, recorded_url=recorded_url, request_url=request_url, content_type=None)
    assert (
        _rewrite(
            body,
            recorded_url=recorded_url,
            request_url=request_url,
            content_type=content_type,
        )
        is None
    )


@pytest.mark.parametrize(
    "resource_type", ["Document", "Image", "Stylesheet", "Media", "Font", None]
)
def test_a_request_that_does_not_run_or_read_a_script(resource_type: str | None) -> None:
    assert _rewrite(_with(RECORDED, SHAPES["bare call"]), resource_type=resource_type) is None


@pytest.mark.parametrize(
    ("recorded_url", "request_url"),
    [
        pytest.param(
            f"https://api.test/d?callback={RECORDED}",
            "https://api.test/d?size=10",
            id="this request has no such parameter",
        ),
        pytest.param(
            f"https://api.test/d?callback={RECORDED}",
            f"https://api.test/d?callback={ASKED}&callback=other",
            id="this request gives it twice",
        ),
        pytest.param(
            f"https://api.test/d?callback={RECORDED}&jsonp={RECORDED}",
            f"https://api.test/d?callback={ASKED}&jsonp={ASKED}",
            id="two recorded parameters carry the name",
        ),
        pytest.param(
            f"https://api.test/d?callback={RECORDED}&callback={RECORDED}",
            f"https://api.test/d?callback={ASKED}",
            id="the recorded parameter is given twice",
        ),
        pytest.param(
            f"https://api.test/{RECORDED}/data",
            f"https://api.test/{ASKED}/data",
            id="a callback in the path",
        ),
        pytest.param(
            f"https://api.test/d?callback={RECORDED}",
            "https://api.test/d?callback=cb);fetch('//evil.test')//",
            id="a value that is not a name",
        ),
        pytest.param(
            f"https://api.test/d?callback={RECORDED}",
            "https://api.test/d?callback=",
            id="an empty value",
        ),
        pytest.param(
            f"https://api.test/d?callback={RECORDED}",
            "https://api.test/d?callback=%E5%9B%9E%E8%B0%83",
            id="a value that is not ASCII",
        ),
    ],
)
def test_the_parameter_must_be_provable(recorded_url: str, request_url: str) -> None:
    body = _with(RECORDED, SHAPES["bare call"])
    assert _rewrite(body, recorded_url=recorded_url, request_url=request_url) is None


# ── the pieces ──────────────────────────────────────────────────────────


def test_leading_call_spans_every_occurrence_of_the_wrapper() -> None:
    body = b"/**/ typeof cb7 === 'function' && cb7({})"
    call = leading_call(body)
    assert call is not None
    assert call.name == "cb7"
    assert [body[start:end] for start, end in call.spans] == [b"cb7", b"cb7"]


def test_callback_parameter_names_the_one_carrier() -> None:
    assert callback_parameter("https://a.test/x?a=1&cb=foo", "foo") == "cb"
    assert callback_parameter("https://a.test/x?a=foo&cb=foo", "foo") is None
    assert callback_parameter("https://a.test/x?a=1", "foo") is None


@pytest.mark.parametrize(
    "query",
    [
        "cb=foo&cb=other",
        "cb=other&cb=foo",
        "cb=foo&cb=",
        "cb=&cb=foo",
        "cb=foo&%63b=other",
        "%63b=other&cb=foo",
    ],
)
def test_a_repeated_recorded_parameter_is_ambiguous_even_when_values_differ(query: str) -> None:
    recorded_url = f"https://a.test/x?{query}"
    assert callback_parameter(recorded_url, "foo") is None
    assert (
        _rewrite(
            b"foo({})",
            recorded_url=recorded_url,
            request_url="https://a.test/x?cb=bar",
        )
        is None
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("jQuery123_456", True),
        ("$", True),
        ("_cb", True),
        ("a.b._0", True),
        ("erebor_86738B42C835484FBE32248C1E89AF9E", True),
        ("1abc", False),
        ("a..b", False),
        ("a.", False),
        ("a-b", False),
        ("a b", False),
        ("", False),
        ("回调", False),
    ],
)
def test_is_callback_name(value: str, expected: bool) -> None:
    assert is_callback_name(value) is expected


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        (None, True),
        ("", True),
        ("application/javascript", True),
        ("application/x-javascript;charset=UTF-8", True),
        ("text/javascript", True),
        ("text/ecmascript", True),
        ("text/jscript", True),
        ("application/json; charset=utf-8", True),
        ("application/ld+json", True),
        ("text/plain;charset=UTF-8", True),
        ("text/html; charset=GBK", True),
        ("application/octet-stream", True),
        ("image/gif", False),
        ("text/css", False),
        ("application/pdf", False),
    ],
)
def test_may_be_script_by_the_response_type(content_type: str | None, expected: bool) -> None:
    assert may_be_script("Script", content_type) is expected
    assert may_be_script("XHR", content_type) is expected
    assert may_be_script("Document", content_type) is False

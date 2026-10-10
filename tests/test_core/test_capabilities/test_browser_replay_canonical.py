"""The one canonicaliser replay keys requests with (§4.14 "匹配", ``canonical.py``).

URL rules, each class of volatile parameter, each body type, order
insensitivity, the 4096-character cap, GraphQL (single, persisted, batch,
GET), the exact key — and the redaction parity the whole scheme rests on: a
body recorded by M29's own ``browser.network`` session and the same body
arriving live, redacted with the same function, get the same keys.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.archive.redaction import redact_post_body
from spectragent.core.capabilities.browser.replay.archive_index import load_archive
from spectragent.core.capabilities.browser.replay.canonical import (
    BODY_SHA256_PARAM,
    MAX_BODY_KEY_CHARS,
    METHOD_PARAM,
    POST_DATA_PARAM,
    body_params,
    canonical_query,
    exact_body_digest,
    request_keys,
    volatile_param,
)
from spectragent.core.capabilities.browser.replay.site_rules import SiteRule
from tests._helpers.replay_archive import RecordingScript

FORM = "application/x-www-form-urlencoded"
JSON = "application/json"


def normalized(
    url: str, method: str = "GET", body: bytes | None = None, ct: str | None = None
) -> str:
    return request_keys(method, url, body, ct).normalized


def multipart(boundary: str, *parts: tuple[str, bytes, str | None]) -> bytes:
    out = b""
    for name, value, filename in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += (
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n".encode() + value + b"\r\n"
        )
    return out + f"--{boundary}--\r\n".encode()


class TestURL:
    def test_scheme_and_host_are_lower_cased_the_path_is_not(self) -> None:
        assert normalized("HTTP://Shop.TEST/Item") == normalized("http://shop.test/Item")
        assert normalized("http://shop.test/Item") != normalized("http://shop.test/item")

    def test_default_ports_are_dropped_and_others_kept(self) -> None:
        assert normalized("http://shop.test:80/a") == normalized("http://shop.test/a")
        assert normalized("https://shop.test:443/a") == normalized("https://shop.test/a")
        assert normalized("http://shop.test:8080/a") != normalized("http://shop.test/a")
        assert ":8080/" in normalized("http://shop.test:8080/a")
        assert normalized("https://shop.test:80/a") != normalized("https://shop.test/a")

    def test_the_fragment_is_dropped(self) -> None:
        assert normalized("http://shop.test/a?x=1#top") == normalized("http://shop.test/a?x=1")
        assert request_keys("GET", "http://shop.test/a#top", None, None).exact[1] == (
            "http://shop.test/a"
        )

    def test_the_query_is_sorted_and_decoded(self) -> None:
        assert normalized("http://shop.test/a?b=2&a=1") == normalized("http://shop.test/a?a=1&b=2")
        assert normalized("http://shop.test/a?q=a%20b") == normalized("http://shop.test/a?q=a+b")
        assert normalized("http://shop.test/a?q=1") != normalized("http://shop.test/a?q=2")

    def test_an_empty_path_is_the_root_and_ipv6_keeps_its_brackets(self) -> None:
        assert normalized("http://shop.test") == normalized("http://shop.test/")
        assert normalized("http://[::1]:8080/x").startswith("http://[::1]:8080/x?")

    def test_a_url_urlsplit_refuses_is_its_own_key(self) -> None:
        broken = "http://[::1/x?a=1"
        keys = request_keys("GET", broken, None, None)
        assert (keys.exact, keys.host, keys.params) == (("GET", broken, None), "", ())
        assert keys.normalized == request_keys("GET", broken, None, None).normalized
        assert keys.normalized != request_keys("GET", "http://[::1/y?a=1", None, None).normalized

    def test_the_method_is_part_of_the_key(self) -> None:
        get = normalized("http://shop.test/a")
        assert f"{METHOD_PARAM}=GET" in get
        assert get != normalized("http://shop.test/a", "POST")
        assert normalized("http://shop.test/a", "get") == get


class TestVolatileParameters:
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("utm_source", "newsletter"),
            ("UTM_Campaign", "x"),
            ("fbclid", "IwAR0abc"),
            ("_ga", "2.1234.5678"),
            ("_", "1700000000123"),
            ("cb", "12.5"),
            ("uncache", "-3"),
            ("nocache", "42"),
            ("cachebuster", "7"),
            ("callback", "jsonp1234"),
            ("cb", "jQuery331012_1700000000000"),
            ("cachebust", "1700000000000"),
            ("ts", "1700000000000000"),
            ("bust_ts", "1700000000000"),
        ],
    )
    def test_dropped(self, name: str, value: str) -> None:
        assert volatile_param(name, value) == "drop"
        assert normalized(f"http://shop.test/a?{name}={value}&k=v") == normalized(
            "http://shop.test/a?k=v"
        )

    @pytest.mark.parametrize(
        ("name", "value"),
        [("timestamp", "99"), ("_", "abc"), ("r", "0.5"), ("width", "800"), ("height", "600")],
    )
    def test_blanked_keeping_the_name(self, name: str, value: str) -> None:
        assert volatile_param(name, value) == "blank"
        assert normalized(f"http://shop.test/a?{name}={value}") == normalized(
            f"http://shop.test/a?{name}=other"
        )
        assert normalized(f"http://shop.test/a?{name}={value}") != normalized("http://shop.test/a")

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("cb", "abc"),  # not numeric
            ("cache_mode", "full"),
            ("ts", "17"),  # too short for a timestamp
            ("ts", "17000000000000000"),  # too long
            ("ts", "2700000000000"),  # no leading 1: not an epoch in ms / µs (pywb)
            ("products", "4006381333931"),  # an EAN-13 in a name containing "ts"
            ("id", "1700000000000"),  # no bust / ts in the name
            ("callback", "handle"),
            ("page", "2"),
        ],
    )
    def test_kept(self, name: str, value: str) -> None:
        assert volatile_param(name, value) is None
        assert normalized(f"http://shop.test/a?{name}={value}") != normalized("http://shop.test/a")

    def test_a_product_code_is_not_a_timestamp(self) -> None:
        """Two stock requests for two products keep two keys at L1, cache-buster or not.

        pywb drops a ``*bust*`` / ``*ts*`` parameter only when its value is
        ``1`` and 12–15 digits — an epoch in milliseconds or microseconds; any
        13–16 digits would drop a barcode from ``products`` and serve one
        product's stock for another as a clean ``normalized`` hit.
        """
        base = "https://shop.test/api/stock"
        first = normalized(f"{base}?products=4006381333931&_=1712345678901")
        second = normalized(f"{base}?products=5000112637922&_=1712345678999")
        assert first != second
        assert "products=4006381333931" in first
        assert first == normalized(f"{base}?products=4006381333931&_=1712345670000")

    def test_the_exact_key_keeps_everything(self) -> None:
        keys = request_keys("GET", "http://shop.test/a?utm_source=x", None, None)
        assert keys.exact[1] == "http://shop.test/a?utm_source=x"


class TestBodies:
    def test_urlencoded_is_order_insensitive_and_decoded(self) -> None:
        a = request_keys("POST", "http://shop.test/f", b"b=2&a=hello+world", FORM)
        b = request_keys("POST", "http://shop.test/f", b"a=hello%20world&b=2", FORM)
        assert a.normalized == b.normalized and a.body_key == b.body_key
        assert a.exact != b.exact
        assert request_keys("POST", "http://shop.test/f", b"a=1&b=3", FORM).body_key != a.body_key

    def test_body_and_query_parameters_merge_into_one_sorted_list(self) -> None:
        keys = request_keys("POST", "http://shop.test/f?z=9", b"a=1", FORM)
        assert keys.normalized == (f"http://shop.test/f?{METHOD_PARAM}=POST&a=1&z=9")
        assert keys.body_key == "a=1"

    def test_multipart_ignores_the_boundary(self) -> None:
        one = multipart("AaB03x", ("name", b"bob", None), ("note", b"hi", None))
        two = multipart("XyZ987", ("note", b"hi", None), ("name", b"bob", None))
        ct1 = "multipart/form-data; boundary=AaB03x"
        ct2 = "multipart/form-data; boundary=XyZ987"
        a = request_keys("POST", "http://shop.test/u", one, ct1)
        b = request_keys("POST", "http://shop.test/u", two, ct2)
        assert a.normalized == b.normalized
        assert "AaB03x" not in a.normalized and "XyZ987" not in (b.body_key or "")
        # The exact level hashes the bytes without the boundary.
        same_order = multipart("XyZ987", ("name", b"bob", None), ("note", b"hi", None))
        assert a.exact[2] == request_keys("POST", "http://shop.test/u", same_order, ct2).exact[2]

    def test_a_file_part_is_its_sha256(self) -> None:
        data = b"\x89PNG binary"
        body = multipart("b1", ("upload", data, "a.png"))
        params = dict(body_params(body, "multipart/form-data; boundary=b1"))
        assert params["upload"] == "sha256:" + hashlib.sha256(data).hexdigest()

    def test_json_is_flattened_by_full_path(self) -> None:
        """Leaf names alone collide: ``{"a":{"id":1},"b":{"id":2}}`` vs the swap."""
        first = json.dumps({"a": {"id": 1}, "b": {"id": 2}}).encode()
        swapped = json.dumps({"a": {"id": 2}, "b": {"id": 1}}).encode()
        assert dict(body_params(first, JSON)) == {"a.id": "1", "b.id": "2"}
        assert body_params(first, JSON) != body_params(swapped, JSON)
        nested = json.dumps({"c": [{"b": True}, None, 1.5, "s"], "e": {}, "f": []}).encode()
        assert dict(body_params(nested, JSON)) == {
            "c.0.b": "true",
            "c.1": "null",
            "c.2": "1.5",
            "c.3": "s",
            "e": "{}",
            "f": "[]",
        }

    def test_json_key_order_does_not_matter(self) -> None:
        a = json.dumps({"x": 1, "y": {"p": 2, "q": 3}}).encode()
        b = json.dumps({"y": {"q": 3, "p": 2}, "x": 1}).encode()
        assert body_params(a, JSON) == body_params(b, JSON)
        assert body_params(a, "application/vnd.api+json") == body_params(a, JSON)

    def test_text_plain_and_untyped_bodies_are_tried_as_json_then_as_a_form(self) -> None:
        doc = b'{"event": "click"}'
        assert body_params(doc, "text/plain;charset=UTF-8") == body_params(doc, JSON)
        assert dict(body_params(b"a=1&b=2", None)) == {"a": "1", "b": "2"}

    def test_newline_delimited_json_is_read_line_by_line(self) -> None:
        body = b'{"a": 1}\n{"a": 2}\n'
        assert dict(body_params(body, JSON)) == {"0.a": "1", "1.a": "2"}

    def test_an_unparsable_body_is_its_sha256(self) -> None:
        for body, content_type in (
            (b"\x00\x01binary", "application/octet-stream"),
            (b"{not json", JSON),
            (b"\xff\xfe", FORM),
            (b"--x\r\nno separator", "multipart/form-data; boundary=x"),
            (b"anything", "multipart/form-data"),  # no boundary
        ):
            assert body_params(body, content_type) == (
                (POST_DATA_PARAM, hashlib.sha256(body).hexdigest()),
            ), content_type

    def test_every_method_with_a_body_is_keyed(self) -> None:
        for method in ("PUT", "PATCH", "DELETE"):
            a = request_keys(method, "http://shop.test/r", b"a=1", FORM)
            b = request_keys(method, "http://shop.test/r", b"a=2", FORM)
            assert a.normalized != b.normalized, method

    def test_no_body_and_an_empty_body_are_the_same(self) -> None:
        assert request_keys("POST", "http://shop.test/r", b"", FORM) == request_keys(
            "POST", "http://shop.test/r", None, FORM
        )
        assert request_keys("POST", "http://shop.test/r", None, None).body_key is None


class TestTheBodyCap:
    def test_up_to_the_cap_the_parameters_are_the_key(self) -> None:
        value = "v" * (MAX_BODY_KEY_CHARS - len("a="))
        params = body_params(f"a={value}".encode(), FORM)
        assert params == (("a", value),)

    def test_past_the_cap_the_body_is_its_sha256_and_long_bodies_do_not_collide(self) -> None:
        stem = "v" * MAX_BODY_KEY_CHARS
        one = body_params(f"a={stem}1".encode(), FORM)
        two = body_params(f"a={stem}2".encode(), FORM)
        assert [name for name, _ in one] == [BODY_SHA256_PARAM]
        assert one != two, "upstream truncates at 4096 characters and these would collide"


class TestGraphQL:
    QUERY = "query Search($q: String!) {\n  search(q: $q) { id }\n}"

    def op(self, **fields: object) -> dict[str, object]:
        return {"operationName": "Search", "query": self.QUERY, "variables": {"q": "x"}, **fields}

    def test_a_single_operation_is_name_query_hash_and_variables(self) -> None:
        params = dict(body_params(json.dumps(self.op()).encode(), JSON))
        assert params["operationName"] == "Search"
        assert params["variables.q"] == "x"
        expected = hashlib.sha256(" ".join(self.QUERY.split()).encode()).hexdigest()
        assert params["query_sha256"] == expected
        assert "query" not in params

    def test_whitespace_in_the_query_does_not_matter_variables_do(self) -> None:
        compact = self.op(query="query Search($q: String!) { search(q: $q) { id } }")
        assert body_params(json.dumps(self.op()).encode(), JSON) == body_params(
            json.dumps(compact).encode(), JSON
        )
        other = self.op(variables={"q": "y"})
        assert body_params(json.dumps(self.op()).encode(), JSON) != body_params(
            json.dumps(other).encode(), JSON
        )

    def test_a_volatile_variable_is_removed_by_the_site_rule(self) -> None:
        rule = SiteRule(ignore_body_paths=["variables.cursor"])
        a = self.op(variables={"q": "x", "cursor": "abc"})
        b = self.op(variables={"q": "x", "cursor": "def"})
        assert body_params(json.dumps(a).encode(), JSON) != body_params(
            json.dumps(b).encode(), JSON
        )
        assert body_params(json.dumps(a).encode(), JSON, rule) == body_params(
            json.dumps(b).encode(), JSON, rule
        )

    def test_a_persisted_query_is_keyed_by_its_own_hash(self) -> None:
        persisted = {
            "operationName": "Search",
            "variables": {"q": "x"},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "abc123"}},
        }
        params = dict(body_params(json.dumps(persisted).encode(), JSON))
        assert params["persisted_sha256"] == "abc123"
        assert "query_sha256" not in params

    def test_a_batch_is_keyed_item_by_item_in_order(self) -> None:
        first, second = self.op(), self.op(operationName="Other", variables={"q": "z"})
        batch = dict(body_params(json.dumps([first, second]).encode(), JSON))
        assert (batch["0.operationName"], batch["1.operationName"]) == ("Search", "Other")
        swapped = body_params(json.dumps([second, first]).encode(), JSON)
        assert body_params(json.dumps([first, second]).encode(), JSON) != swapped
        rule = SiteRule(ignore_body_paths=["variables.q"])
        stripped = dict(body_params(json.dumps([first, second]).encode(), JSON, rule))
        assert "0.variables.q" not in stripped and "1.variables.q" not in stripped

    def test_a_get_request_carries_its_operation_in_the_url(self) -> None:
        url = (
            "http://shop.test/graphql?operationName=Search&query="
            + "query%20Search%20%7B%20search%20%7D&variables=%7B%22q%22%3A%22x%22%7D&utm_source=x"
        )
        query = dict(canonical_query(url.split("?", 1)[1]))
        assert query["operationName"] == "Search"
        assert query["variables.q"] == "x"
        assert "query_sha256" in query and "query" not in query and "utm_source" not in query
        spaced = url.replace("query%20Search%20%7B", "query%20%20Search%20%20%7B")
        assert normalized(url) == normalized(spaced)

    def test_a_search_parameter_called_query_is_not_graphql(self) -> None:
        assert dict(canonical_query("query=red+shoes")) == {"query": "red shoes"}
        assert dict(body_params(b'{"query": "red shoes"}', JSON)) == {"query": "red shoes"}


class TestTheExactKey:
    def test_method_url_and_body_digest(self) -> None:
        keys = request_keys("post", "http://shop.test/f?x=1#frag", b"a=1", FORM)
        assert keys.exact == ("POST", "http://shop.test/f?x=1", hashlib.sha256(b"a=1").hexdigest())
        assert request_keys("GET", "http://shop.test/f", None, None).exact[2] is None

    def test_the_boundary_is_removed_only_for_multipart(self) -> None:
        body = b"--b1 data b1"
        assert (
            exact_body_digest(body, "text/plain; boundary=b1") == hashlib.sha256(body).hexdigest()
        )
        assert exact_body_digest(body, "multipart/form-data; boundary=b1") == (
            hashlib.sha256(body.replace(b"b1", b"")).hexdigest()
        )


class TestRedactionParity:
    """Both sides redact with M29's function before any key, so secrets never break a match."""

    SECRET = "planted-Secret-77"

    async def test_record_and_replay_keys_agree_after_redaction(self, tmp_path: Path) -> None:
        bodies = {
            "http://shop.test/login": (
                f"user=alice&password={self.SECRET}&remember=1".encode(),
                FORM,
            ),
            "http://shop.test/api/token": (
                json.dumps({"user": "alice", "auth": {"token": self.SECRET}, "n": 1}).encode(),
                JSON,
            ),
            "http://shop.test/upload": (
                multipart(
                    "RecB0undary",
                    ("user", b"alice", None),
                    ("password", self.SECRET.encode(), None),
                ),
                "multipart/form-data; boundary=RecB0undary",
            ),
        }
        recording = RecordingScript(tmp_path)
        for url, (body, content_type) in bodies.items():
            await recording.exchange(
                url, method="POST", post=body, request_headers={"Content-Type": content_type}
            )
        await recording.commit(0)
        reader = open_archive(recording.finish())
        recorded = {record.url: record for record in load_archive(reader).index.records}

        everything = b"".join(
            reader.read_extra(entry.path)
            for _, entry in reader.iter_archive("browser.network", include_trailing=True)
        )
        assert self.SECRET.encode() not in everything, "the archive holds only redacted values"

        for url, (body, content_type) in bodies.items():
            live_body, live_type = body, content_type
            if url.endswith("upload"):  # a live upload has its own random boundary
                live_body = body.replace(b"RecB0undary", b"LiveB0undary9")
                live_type = content_type.replace("RecB0undary", "LiveB0undary9")
            redacted, _ = redact_post_body(live_body, live_type)
            live = request_keys("POST", url, redacted, live_type)
            record = recorded[url]
            assert record.keys.exact == live.exact, url
            assert record.keys.normalized == live.normalized, url
            assert record.keys.body_key == live.body_key, url
            # Unredacted, the live request would not have matched at all.
            assert request_keys("POST", url, live_body, live_type).exact != record.keys.exact, url

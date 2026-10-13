"""The browser's redaction rules for environment evidence (§4.14 privacy, §5.6).

What is withheld before any ``ArchiveFile`` exists: stripped request headers,
credential-shaped POST fields, and sensitive form values in raw DOM captures
(the attribute, the user-agent shadow text, the DOMSnapshot value tables, the
accessibility value). Cookie values are not among them since ``browser-2``
(``storage.json`` stores them as they are). Plus the tripwire: the rule
sources are pinned by digest, so a rule cannot change without
``BROWSER_REDACTION_POLICY`` changing with it.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any

import pytest

from spectragent.core.archive.privacy import PRIVACY_POLICY, REDACTED
from spectragent.core.capabilities.browser.archive import redaction
from spectragent.core.capabilities.browser.archive.redaction import (
    BROWSER_REDACTION_POLICY,
    DEFAULT_STRIPPED_REQUEST_HEADERS,
    browser_ledger,
    redact_post_body,
    redact_raw_capture,
    strip_headers,
    stripped_header_set,
)
from spectragent.core.capabilities.browser.dom import sensitivity
from spectragent.core.capabilities.browser.dom.models import TargetCapture

SECRET = "hunter2-Qz9"


class TestStrippedHeaders:
    def test_defaults_and_extras_are_case_insensitive(self) -> None:
        strip = stripped_header_set(["X-Api-Key", " "])
        assert strip == {"cookie", "authorization", "proxy-authorization", "x-api-key"}
        assert DEFAULT_STRIPPED_REQUEST_HEADERS == (
            "cookie",
            "authorization",
            "proxy-authorization",
        )

    def test_a_mapping_keeps_every_other_header(self) -> None:
        headers = {"Cookie": "sid=1", "AUTHORIZATION": "Bearer x", "Accept": "*/*"}
        kept, entries = strip_headers(headers, stripped_header_set(), location="/request/headers")
        assert kept == {"Accept": "*/*"}
        assert [(e.field, e.rule, e.location) for e in entries] == [
            ("Cookie", "request_header", "/request/headers"),
            ("AUTHORIZATION", "request_header", "/request/headers"),
        ]
        assert headers["Cookie"] == "sid=1", "the input is not mutated"

    def test_a_fetch_header_entry_list(self) -> None:
        headers = [{"name": "proxy-authorization", "value": "Basic x"}, {"name": "A", "value": "1"}]
        kept, entries = strip_headers(headers, stripped_header_set(), location="/h")
        assert kept == [{"name": "A", "value": "1"}]
        assert [e.field for e in entries] == ["proxy-authorization"]

    def test_an_unknown_shape_is_not_stored(self) -> None:
        assert strip_headers("Cookie: x", stripped_header_set(), location="/h") == ({}, [])

    def test_blanking_keeps_the_names_for_the_ledger_and_drops_the_values(self) -> None:
        """What travels through the archive scope before strip_headers runs (§4.14)."""
        strip = stripped_header_set(["X-Api-Key"])
        headers = {"Cookie": "sid=1", "x-api-key": "k", "Accept": "*/*"}
        blanked = redaction.blank_stripped_headers(headers, strip)
        assert blanked == {"Cookie": "", "x-api-key": "", "Accept": "*/*"}
        assert headers["Cookie"] == "sid=1", "the input is not mutated"
        kept, entries = strip_headers(blanked, strip, location="/request/headers")
        assert kept == {"Accept": "*/*"} and [e.field for e in entries] == ["Cookie", "x-api-key"]
        entries_shape = [
            {"name": "Authorization", "value": "Bearer x"},
            {"name": "A", "value": "1"},
        ]
        assert redaction.blank_stripped_headers(entries_shape, strip) == [
            {"name": "Authorization", "value": ""},
            {"name": "A", "value": "1"},
        ]
        assert redaction.blank_stripped_headers("Cookie: x", strip) == {}


class TestPostBodies:
    def test_urlencoded_keeps_every_other_byte(self) -> None:
        body = f"username=alice&password={SECRET}&remember=on&api%5Fkey=k1".encode()
        redacted, entries = redact_post_body(
            body, "application/x-www-form-urlencoded; charset=UTF-8"
        )
        assert redacted == b"username=alice&password=<redacted>&remember=on&api%5Fkey=<redacted>"
        assert [(e.field, e.rule) for e in entries] == [
            ("password", "field_name"),
            ("api_key", "field_name"),
        ]

    def test_multipart_replaces_only_the_part_body(self) -> None:
        boundary = "----af"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="user"\r\n\r\nalice\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="pwd"\r\n\r\n{SECRET}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="doc"; filename="a.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\nfile body\r\n--{boundary}--\r\n"
        ).encode()
        redacted, entries = redact_post_body(body, f"multipart/form-data; boundary={boundary}")
        assert SECRET.encode() not in redacted
        assert b"alice" in redacted and b"file body" in redacted
        assert redacted == body.replace(SECRET.encode(), b"<redacted>")
        assert [e.field for e in entries] == ["pwd"]

    def test_json_uses_the_shared_value_rule(self) -> None:
        body = json.dumps({"query": "shoes", "token": SECRET, "nested": {"cvv": "123"}}).encode()
        redacted, entries = redact_post_body(body, "application/json")
        assert json.loads(redacted) == {
            "query": "shoes",
            "token": REDACTED,
            "nested": {"cvv": REDACTED},
        }
        assert sorted(e.location for e in entries) == ["/nested/cvv", "/token"]

    def test_json_without_a_match_keeps_its_bytes(self) -> None:
        body = b'{ "query" : "shoes" }'
        assert redact_post_body(body, "application/graphql+json") == (body, [])

    def test_batched_graphql_is_walked(self) -> None:
        body = json.dumps([{"variables": {"password": SECRET}}, {"query": "q"}]).encode()
        redacted, entries = redact_post_body(body, "application/json")
        assert SECRET.encode() not in redacted
        assert [e.location for e in entries] == ["/0/variables/password"]

    def test_text_plain_beacons(self) -> None:
        assert redact_post_body(b"event=view&page=beacon", "text/plain;charset=UTF-8") == (
            b"event=view&page=beacon",
            [],
        )
        redacted, _ = redact_post_body(f"event=login&otp={SECRET}".encode(), None)
        assert redacted == b"event=login&otp=<redacted>"
        redacted, _ = redact_post_body(json.dumps({"secret": SECRET}).encode(), "text/plain")
        assert SECRET.encode() not in redacted

    def test_binary_and_prose_pass_unchanged(self) -> None:
        assert redact_post_body(b"\x00\x01password=x", "application/octet-stream")[1] == []
        assert redact_post_body(b"the password is x", "text/plain") == (b"the password is x", [])

    def test_deterministic(self) -> None:
        body = json.dumps({"b": 1, "password": SECRET, "a": [1, 2]}).encode()
        assert redact_post_body(body, "application/json") == redact_post_body(
            body, "application/json"
        )


def _snapshot_with_password() -> dict[str, Any]:
    """A DOMSnapshot: html > [input(text, value shoes), input(password) > #shadow > #text]."""
    strings = [
        "HTML",  # 0
        "INPUT",  # 1
        "type",  # 2
        "text",  # 3
        "value",  # 4
        "shoes",  # 5
        "password",  # 6
        SECRET,  # 7
        "#text",  # 8
        "#document-fragment",  # 9
        "visible shoes",  # 10
    ]
    return {
        "strings": strings,
        "documents": [
            {
                "documentURL": 10,
                "nodes": {
                    "parentIndex": [-1, 0, 0, 2, 3],
                    "nodeType": [1, 1, 1, 11, 3],
                    "nodeName": [0, 1, 1, 9, 8],
                    "nodeValue": [-1, -1, -1, -1, 7],
                    "backendNodeId": [10, 11, 12, 13, 14],
                    "attributes": [[], [2, 3, 4, 5], [2, 6, 4, 7], [], []],
                    "inputValue": {"index": [1, 2], "value": [5, 7]},
                    "textValue": {"index": [], "value": []},
                },
                "layout": {
                    "nodeIndex": [4],
                    "text": [7],
                    "styles": [[5]],
                    "bounds": [[0, 0, 1, 1]],
                },
            }
        ],
    }


def _dom_with_password() -> dict[str, Any]:
    return {
        "root": {
            "nodeType": 9,
            "nodeName": "#document",
            "backendNodeId": 1,
            "children": [
                {
                    "nodeType": 1,
                    "nodeName": "INPUT",
                    "backendNodeId": 11,
                    "attributes": ["type", "text", "value", "shoes"],
                },
                {
                    "nodeType": 1,
                    "nodeName": "INPUT",
                    "backendNodeId": 12,
                    "attributes": ["type", "password", "value", SECRET],
                    "shadowRoots": [
                        {
                            "nodeType": 11,
                            "shadowRootType": "user-agent",
                            "backendNodeId": 13,
                            "children": [
                                {
                                    "nodeType": 3,
                                    "nodeName": "#text",
                                    "backendNodeId": 14,
                                    "nodeValue": SECRET,
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    }


def _ax_with_password() -> dict[str, Any]:
    return {
        "nodes": [
            {"nodeId": "1", "backendDOMNodeId": 11, "value": {"type": "string", "value": "shoes"}},
            {
                "nodeId": "2",
                "backendDOMNodeId": 12,
                "name": {"type": "computedString", "value": "Password"},
                "value": {"type": "string", "value": SECRET},
            },
            {
                "nodeId": "3",
                "backendDOMNodeId": 14,
                "name": {"type": "computedString", "value": SECRET},
            },
        ]
    }


class TestRawCapture:
    def test_every_copy_of_a_sensitive_value_is_withheld(self) -> None:
        capture = TargetCapture(
            raw_dom=_dom_with_password(),
            snapshot=_snapshot_with_password(),
            ax_tree=_ax_with_password(),
            js_click_listener_ids={3, 1},
            loader_id="L1",
            url="http://shop.test/",
        )
        data, entries = redact_raw_capture(capture)
        text = json.dumps(data)
        assert SECRET not in text
        assert "shoes" in text, "a non-sensitive value is the agent's to see — kept"
        assert data["js_click_listener_ids"] == [1, 3]
        assert {e.rule for e in entries} == {"input_value"}
        locations = {e.location for e in entries}
        assert "/raw_dom/root/children/1/attributes/3" in locations
        assert "/raw_dom/root/children/1/shadowRoots/0/children/0/nodeValue" in locations
        assert "/ax_tree/nodes/1/value/value" in locations
        assert "/ax_tree/nodes/2/name/value" in locations
        # The control's own accessible name (its label) is not its value.
        assert data["ax_tree"]["nodes"][1]["name"]["value"] == "Password"

    def test_the_capture_itself_is_not_changed(self) -> None:
        dom = _dom_with_password()
        capture = TargetCapture(raw_dom=dom, snapshot=_snapshot_with_password(), ax_tree={})
        redact_raw_capture(capture)
        assert capture.raw_dom["root"]["children"][1]["attributes"][3] == SECRET
        assert SECRET in capture.snapshot["strings"]

    def test_the_snapshot_string_table(self) -> None:
        """Only-sensitive strings are overwritten in place; shared ones are repointed."""
        snapshot = _snapshot_with_password()
        data, _ = redact_raw_capture(TargetCapture(raw_dom={}, snapshot=snapshot, ax_tree={}))
        strings = data["snapshot"]["strings"]
        assert SECRET not in strings
        nodes = data["snapshot"]["documents"][0]["nodes"]
        # The password's inputValue and value attribute point at <redacted> now.
        assert strings[nodes["inputValue"]["value"][1]] == REDACTED
        assert strings[nodes["attributes"][2][3]] == REDACTED
        # The text input's value is untouched.
        assert strings[nodes["inputValue"]["value"][0]] == "shoes"
        assert strings[data["snapshot"]["documents"][0]["layout"]["text"][0]] == REDACTED

    def test_a_string_shared_with_visible_content_is_repointed_not_overwritten(self) -> None:
        snapshot = _snapshot_with_password()
        # The secret also appears as layout-free visible text of another node.
        snapshot["documents"][0]["nodes"]["attributes"][1][3] = 7
        data, _ = redact_raw_capture(TargetCapture(raw_dom={}, snapshot=snapshot, ax_tree={}))
        strings = data["snapshot"]["strings"]
        nodes = data["snapshot"]["documents"][0]["nodes"]
        assert strings[nodes["attributes"][1][3]] == SECRET, "the text input's own value stays"
        assert strings[nodes["inputValue"]["value"][1]] == REDACTED

    def test_skyvern_describe_node_and_tree(self) -> None:
        raw = {
            "skyvern_tree": {
                "tagName": "input",
                "attributes": {"type": "password", "value": SECRET},
                "text": SECRET,
                "children": [],
            },
            "skyvern_nodes": {
                "0": {"nodeName": "INPUT", "attributes": ["type", "password", "value", SECRET]}
            },
        }
        data, entries = redact_raw_capture(TargetCapture(raw_dom=raw, snapshot={}, ax_tree={}))
        assert SECRET not in json.dumps(data)
        assert len(entries) == 3

    def test_out_of_process_frames_are_redacted_too(self) -> None:
        frame = TargetCapture(raw_dom=_dom_with_password(), snapshot={}, ax_tree={}, target_id="F1")
        capture = TargetCapture(raw_dom={}, snapshot={}, ax_tree={}, frames={"F1": frame})
        data, entries = redact_raw_capture(capture)
        assert SECRET not in json.dumps(data)
        assert all(e.location.startswith("/frames/F1/") for e in entries)


def test_ledgers_carry_the_browser_policy() -> None:
    assert f"{PRIVACY_POLICY}+browser-2" == BROWSER_REDACTION_POLICY
    assert browser_ledger([]).policy == BROWSER_REDACTION_POLICY


#: Browser policy version → digest of the rule sources. A rule changed in
#: place fails ``test_a_browser_rule_change_bumps_the_policy``: bump
#: ``BROWSER_REDACTION_POLICY`` and pin the new digest. A change that alters no
#: behaviour (a comment, a docstring) moves it too — re-pin, and say so.
_KNOWN_BROWSER_POLICIES = {
    "af-privacy-1+browser-1": "93828aa353848cf72dfaddbe903d73b908d9bf51e0ba4e81ff5205e71b4a92d7",
    # browser-2: cookie values are no longer hashed (storage.json stores them raw).
    "af-privacy-1+browser-2": "5bf1fca59190d55ecad9b05a04753c0ba8a8c55351c9babc322c7dd27f0c3a17",
}


def _rule_digest() -> str:
    functions = (
        sensitivity.is_sensitive_control,
        sensitivity.attribute_map,
        redaction.stripped_header_set,
        redaction.strip_headers,
        redaction._media_type,
        redaction._redact_urlencoded,
        redaction._redact_multipart,
        redaction._redact_json,
        redaction.redact_post_body,
        redaction._DomRedaction,
        redaction._capture_dict,
        redaction.redact_raw_capture,
    )
    material = "\n".join(
        [
            sensitivity.SENSITIVE_HINT_PATTERN.pattern,
            str(int(sensitivity.SENSITIVE_HINT_PATTERN.flags)),
            ",".join(sorted(sensitivity.FORM_CONTROL_TAGS)),
            ",".join(sorted(sensitivity.SENSITIVE_INPUT_TYPES)),
            ",".join(redaction.DEFAULT_STRIPPED_REQUEST_HEADERS),
            redaction._DISPOSITION_NAME.pattern,
            redaction._FORM_SHAPE.pattern.decode("latin-1"),
            *(inspect.getsource(function) for function in functions),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def test_a_browser_rule_change_bumps_the_policy() -> None:
    assert BROWSER_REDACTION_POLICY in _KNOWN_BROWSER_POLICIES, (
        f"BROWSER_REDACTION_POLICY {BROWSER_REDACTION_POLICY!r} has no pinned digest"
    )
    assert _KNOWN_BROWSER_POLICIES[BROWSER_REDACTION_POLICY] == _rule_digest(), (
        "the browser redaction rules changed but BROWSER_REDACTION_POLICY did not: bump it "
        "and pin the new digest"
    )


def test_the_tripwire_covers_the_sensitivity_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    pinned = _rule_digest()

    def is_sensitive_control(tag: str, attrs: Any) -> bool:  # same name, another body
        return False

    monkeypatch.setattr(sensitivity, "is_sensitive_control", is_sensitive_control)
    assert _rule_digest() != pinned

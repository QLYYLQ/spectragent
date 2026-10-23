"""Rules that parse a field's value and ignore some of its sub-fields (§4.14 "请求规则").

``parse_query`` / ``parse_body_paths`` declare that a query parameter or a body
field holds more fields — a URL-encoded query string or a JSON document — so a
rule can ignore a client time or a signature inside it while the video id next
to it stays in the key. Parsing happens in the one canonicaliser, before the
body cap and its digest, for recorded and live requests alike, and the
declarations are part of the rule text the replay fingerprint freezes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.canonical import (
    BODY_SHA256_PARAM,
    MAX_BODY_KEY_CHARS,
    body_params,
    canonical_query,
    parsed_subfields,
    request_keys,
)
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher
from spectragent.core.capabilities.browser.replay.site_rules import (
    ParsedField,
    SiteRule,
    SiteRuleError,
    SiteRules,
)
from tests._helpers.replay_archive import indexed

JSON = "application/json"
URL = "https://player.test/proxy"


def player_rule(**fields: Any) -> SiteRule:
    """A rule for a player request whose ``info`` field holds a URL-encoded query string."""
    return SiteRule(
        id="player",
        exhaustion="repeat_last",
        unavailable="miss",
        parse_body_paths=(ParsedField(field="info", format="urlencoded"),),
        ignore_body_paths=("info.key", "info.tm"),
        **fields,
    )


def player_body(**info: str) -> bytes:
    """``{"kind": "video", "info": "vid=…&defn=…&key=…&tm=…"}``, like the recorded player requests."""
    fields = {"vid": "v0001", "defn": "shd", "key": "--01AB", "tm": "1700000000", **info}
    return json.dumps({"kind": "video", "info": urlencode(fields)}).encode()


def match(rules: SiteRules, recorded: bytes, live: bytes, url: str = URL) -> str | None:
    """The live request's miss reason against one recorded request (``None``: a hit)."""
    record = indexed(
        url,
        record_id="recorded",
        order=0,
        method="POST",
        body=recorded,
        content_type=JSON,
        rule=rules.require_for_request("POST", url),
    )
    result = Matcher(ArchiveIndex([record]), rules=rules).match(
        LiveRequest("POST", url, {"Content-Type": JSON}, live)
    )
    return result.reason


class TestParsing:
    def test_a_urlencoded_value_becomes_named_sub_fields(self) -> None:
        assert parsed_subfields("info", "vid=v1&scene=a%3Db&empty=&s=x+y", "urlencoded") == (
            ("info.vid", "v1"),
            ("info.scene", "a=b"),
            ("info.empty", ""),
            ("info.s", "x y"),
        )

    def test_a_json_value_is_flattened_as_if_written_inline(self) -> None:
        value = json.dumps({"pre": {"video": {"vid": "v1", "flow_id": "f"}}, "ids": [1, 2]})
        assert parsed_subfields("ssp", value, "json") == (
            ("ssp.pre.video.vid", "v1"),
            ("ssp.pre.video.flow_id", "f"),
            ("ssp.ids.0", "1"),
            ("ssp.ids.1", "2"),
        )
        assert parsed_subfields("ssp", "{}", "json") == (("ssp", "{}"),)

    @pytest.mark.parametrize(
        ("value", "format"),
        [
            ("", "urlencoded"),
            ("token", "urlencoded"),
            ("a=1&token", "urlencoded"),
            ("=1", "urlencoded"),
            ("", "json"),
            ('"a string"', "json"),
            ("12", "json"),
            ("{not json", "json"),
        ],
    )
    def test_a_value_of_another_shape_does_not_parse(self, value: str, format: Any) -> None:
        assert parsed_subfields("f", value, format) is None

    def test_a_value_that_does_not_parse_stays_one_field(self) -> None:
        rule = player_rule()
        body = json.dumps({"kind": "video", "info": "plain-token"}).encode()
        assert ("info", "plain-token") in body_params(body, JSON, rule)


class TestMatching:
    def test_ignored_sub_fields_match_and_the_video_id_stays_keyed(self) -> None:
        rules = SiteRules([player_rule()])
        recorded = player_body()
        assert match(rules, recorded, player_body(key="--01FF", tm="1700000009")) is None
        assert match(rules, recorded, player_body(vid="v0002", key="--01FF")) == "no_match"
        assert match(rules, recorded, player_body(defn="hd")) == "no_match"

    def test_without_the_parse_declaration_the_whole_field_is_one_value(self) -> None:
        rules = SiteRules([player_rule().model_copy(update={"parse_body_paths": ()})])
        assert match(rules, player_body(), player_body(key="--01FF")) == "no_match"

    def test_the_rest_of_the_body_stays_keyed(self) -> None:
        rules = SiteRules([player_rule()])
        other = json.loads(player_body())
        other["kind"] = "ad"
        assert match(rules, player_body(), json.dumps(other).encode()) == "no_match"

    def test_sub_field_order_and_encoding_do_not_change_the_normalized_key(self) -> None:
        rule = player_rule()
        a = json.dumps({"kind": "video", "info": "vid=v1&s=x%20y&key=1"}).encode()
        b = json.dumps({"kind": "video", "info": "s=x+y&key=2&vid=v1"}).encode()
        keys_a = request_keys("POST", URL, a, JSON, rule)
        keys_b = request_keys("POST", URL, b, JSON, rule)
        assert keys_a.normalized == keys_b.normalized
        assert keys_a.exact != keys_b.exact

    def test_a_query_parameter_holding_a_query_string(self) -> None:
        rule = SiteRule(
            id="redirect",
            exhaustion="repeat_last",
            unavailable="miss",
            parse_query=(ParsedField(field="ext", format="urlencoded"),),
            ignore_query=("ext.trace",),
        )
        recorded = canonical_query("id=7&ext=" + "lt%3D1%26trace%3Daaa", rule)
        assert recorded == (("ext.lt", "1"), ("id", "7"))
        assert canonical_query("id=7&ext=" + "trace%3Dbbb%26lt%3D1", rule) == recorded
        assert canonical_query("id=7&ext=" + "lt%3D2%26trace%3Daaa", rule) != recorded

    def test_a_json_string_in_a_query_parameter_and_in_a_json_body(self) -> None:
        rule = SiteRule(
            id="ads",
            exhaustion="repeat_last",
            unavailable="miss",
            parse_query=(ParsedField(field="bop", format="json"),),
            ignore_query=("bop.dfp",),
            parse_body_paths=(ParsedField(field="ssp", format="json"),),
            ignore_body_paths=("ssp.*.flow_id",),
        )
        query = "bop=" + json.dumps({"version": "10", "dfp": "a"})
        other = "bop=" + json.dumps({"dfp": "b", "version": "10"})
        assert canonical_query(query, rule) == canonical_query(other, rule)
        assert canonical_query(query, rule) == (("bop.version", "10"),)

        def body(flow: str, vid: str) -> bytes:
            ssp = json.dumps({"pre": {"video": {"vid": vid, "flow_id": flow}}})
            return json.dumps({"ssp": ssp}).encode()

        assert body_params(body("f1", "v1"), JSON, rule) == body_params(
            body("f2", "v1"), JSON, rule
        )
        assert body_params(body("f1", "v1"), JSON, rule) != body_params(
            body("f1", "v2"), JSON, rule
        )

    def test_form_bodies_and_graphql_variables_parse_too(self) -> None:
        rule = SiteRule(
            id="forms",
            exhaustion="repeat_last",
            unavailable="miss",
            parse_body_paths=(
                ParsedField(field="info", format="urlencoded"),
                ParsedField(field="variables.payload", format="json"),
            ),
            ignore_body_paths=("info.tm", "variables.payload.nonce"),
        )
        form = "application/x-www-form-urlencoded"

        def encoded(tm: str) -> bytes:
            return urlencode({"info": f"vid=v1&tm={tm}"}).encode()

        assert body_params(encoded("1"), form, rule) == body_params(encoded("2"), form, rule)

        def operation(nonce: str, item: str) -> bytes:
            payload = json.dumps({"nonce": nonce, "item": item})
            return json.dumps(
                {"query": "query Q { q }", "variables": {"payload": payload}}
            ).encode()

        assert body_params(operation("n1", "i"), JSON, rule) == body_params(
            operation("n2", "i"), JSON, rule
        )
        assert body_params(operation("n1", "i"), JSON, rule) != body_params(
            operation("n1", "j"), JSON, rule
        )

    def test_keep_globs_keep_a_sub_field(self) -> None:
        rule = player_rule(keep_body_paths=("info.tm",))
        assert body_params(player_body(tm="1"), JSON, rule) != body_params(
            player_body(tm="2"), JSON, rule
        )

    def test_sub_fields_are_parsed_and_ignored_before_the_body_cap(self) -> None:
        """The ignored sub-field alone pushes the body past the cap: the rest is still keyed."""
        rule = player_rule()
        huge = "--01" + "A" * MAX_BODY_KEY_CHARS
        keyed = body_params(player_body(key=huge), JSON, rule)
        assert ("info.vid", "v0001") in keyed
        assert [name for name, _ in body_params(player_body(key=huge), JSON)] == [BODY_SHA256_PARAM]

    def test_recorded_and_live_requests_are_keyed_alike(self) -> None:
        """The archive loader and the live matcher call the same function with the same rule."""
        rules = SiteRules([player_rule()])
        rule = rules.require_for_request("POST", URL)
        recorded = indexed(
            URL,
            record_id="r",
            order=0,
            method="POST",
            body=player_body(),
            content_type=JSON,
            rule=rule,
        )
        live = request_keys("POST", URL, player_body(key="--01FF"), JSON, rule)
        assert recorded.keys.normalized == live.normalized
        assert recorded.keys.body_key == live.body_key
        assert "info.vid=v0001" in (live.body_key or "")


class TestRuleInput:
    def write(self, root: Path, rule: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "player.yaml").write_text(
            "rules:\n- id: player\n  exhaustion: repeat_last\n  unavailable: miss\n" + rule
        )
        return root

    def test_the_yaml_syntax(self, tmp_path: Path) -> None:
        root = self.write(
            tmp_path,
            "  parse_body_paths:\n  - {field: info, format: urlencoded}\n"
            "  - {field: ssp, format: json}\n"
            "  ignore_body_paths: [info.key]\n"
            "  parse_query:\n  - {field: ext, format: urlencoded}\n",
        )
        rule = SiteRules.load(root).require_for_request("POST", URL)
        assert rule.body_path_format("info") == "urlencoded"
        assert rule.body_path_format("ssp") == "json"
        assert rule.body_path_format("info.key") is None
        assert rule.query_format("ext") == "urlencoded"
        assert isinstance(rule.parse_body_paths, tuple)

    @pytest.mark.parametrize(
        ("declaration", "message"),
        [
            ("  parse_body_paths: [{field: info, format: xml}]\n", "urlencoded"),
            ("  parse_body_paths: [{field: info}]\n", "format"),
            ("  parse_body_paths: [{field: '', format: json}]\n", "field"),
            ("  parse_body_paths: [{field: info, format: json, glob: true}]\n", "glob"),
            ("  parse_query: [info]\n", "parse_query"),
            (
                "  parse_body_paths: [{field: info, format: json}, {field: info, format: urlencoded}]\n",
                r"player.*parse_body_paths declares \['info'\] more than once",
            ),
            (
                "  parse_body_paths: [{field: info, format: json}, {field: info.a, format: json}]\n",
                r"player.*'info\.a' is a sub-field of 'info'",
            ),
            (
                "  parse_body_paths: [{field: info, format: urlencoded}]\n  ignore_body_paths: ['*']\n",
                r"player.*'info' is ignored whole",
            ),
            (
                "  parse_query: [{field: ext, format: urlencoded}]\n  ignore_query: [ext]\n",
                r"player.*parse_query field 'ext' is ignored whole",
            ),
        ],
    )
    def test_bad_declarations_are_refused_naming_the_file_and_rule(
        self, tmp_path: Path, declaration: str, message: str
    ) -> None:
        root = self.write(tmp_path, declaration)
        with pytest.raises(SiteRuleError, match=r"player\.yaml") as refused:
            SiteRules.load(root)
        assert refused.match(message)

    def test_the_declarations_are_part_of_the_frozen_rule_set_and_its_digest(
        self, tmp_path: Path
    ) -> None:
        parsed = SiteRules.load(
            self.write(tmp_path / "a", "  parse_body_paths: [{field: info, format: urlencoded}]\n")
        )
        as_json = SiteRules.load(
            self.write(tmp_path / "b", "  parse_body_paths: [{field: info, format: json}]\n")
        )
        plain = SiteRules.load(self.write(tmp_path / "c", ""))
        assert len({parsed.sha256, as_json.sha256, plain.sha256}) == 3
        restored = SiteRules.from_snapshot(parsed.snapshot())
        assert restored.require_for_request("POST", URL).body_path_format("info") == "urlencoded"
        in_memory = SiteRules([player_rule()])
        again = SiteRules.from_snapshot(in_memory.snapshot())
        assert (
            again.require_for_request("POST", URL).parse_body_paths
            == player_rule().parse_body_paths
        )
        assert (
            in_memory.sha256
            != SiteRules([player_rule().model_copy(update={"parse_body_paths": ()})]).sha256
        )

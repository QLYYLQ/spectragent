"""Per-site replay rules (§4.14 "站点规则", ``site_rules.py``).

Which file a host picks (longest suffix, no Public Suffix List), what each
key does — through the canonicaliser and the matcher that consume it, not
just as a parsed field — what loading refuses, and the directory digest the
replay fingerprint records.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.canonical import body_params, request_keys
from spectragent.core.capabilities.browser.replay.matcher import (
    LiveRequest,
    Matcher,
    MatcherConfig,
)
from spectragent.core.capabilities.browser.replay.site_rules import (
    EchoRule,
    SiteRule,
    SiteRuleError,
    SiteRules,
)
from tests._helpers.replay_archive import indexed


def write_rules(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text)
    return root


class TestWhichRuleAppliesToAHost:
    @pytest.fixture()
    def rules(self, tmp_path: Path) -> SiteRules:
        return SiteRules.load(
            write_rules(
                tmp_path / "rules",
                {
                    "example.com.yaml": "ignore_query: [a]\n",
                    "api.example.com.yaml": "ignore_query: [b]\n",
                    "README.md": "not a rule",
                    "notes.txt": "nor this",
                },
            )
        )

    def test_the_longest_suffix_on_label_boundaries(self, rules: SiteRules) -> None:
        assert rules.domains() == ["api.example.com", "example.com"]
        api, site = SiteRule(ignore_query=["b"]), SiteRule(ignore_query=["a"])
        assert rules.for_host("x.y.api.example.com") == api
        assert rules.for_host("api.example.com") == api
        assert rules.for_host("www.example.com") == site
        assert rules.for_host("example.com") == site
        assert rules.for_host("EXAMPLE.COM.") == site
        assert rules.for_host("notexample.com") is None
        assert rules.for_host("example.org") is None

    def test_no_directory_is_no_rules(self) -> None:
        empty = SiteRules.load(None)
        assert (len(empty), empty.sha256, empty.for_host("example.com")) == (0, None, None)


class TestEachKey:
    def test_ignore_query(self) -> None:
        rule = SiteRule(ignore_query=["session*"])
        with_session = request_keys("GET", "http://a.test/p?session_id=1&x=1", None, None, rule)
        other_session = request_keys("GET", "http://a.test/p?session_id=2&x=1", None, None, rule)
        assert with_session.normalized == other_session.normalized
        assert with_session.exact != other_session.exact
        unruled = request_keys("GET", "http://a.test/p?session_id=1&x=1", None, None)
        assert (
            unruled.normalized
            != request_keys("GET", "http://a.test/p?session_id=2&x=1", None, None).normalized
        )

    def test_ignore_body_paths_for_json_forms_and_graphql(self) -> None:
        rule = SiteRule(ignore_body_paths=["client.ts", "nonce", "variables.cursor"])
        a = json.dumps({"q": "x", "client": {"ts": 1, "id": 7}}).encode()
        b = json.dumps({"q": "x", "client": {"ts": 2, "id": 7}}).encode()
        assert body_params(a, "application/json", rule) == body_params(b, "application/json", rule)
        assert body_params(a, "application/json") != body_params(b, "application/json")
        form = "application/x-www-form-urlencoded"
        assert body_params(b"q=1&nonce=a", form, rule) == body_params(b"q=1&nonce=b", form, rule)
        op = {"query": "query Q { q }", "variables": {"cursor": "c1", "n": 1}}
        other = {"query": "query Q { q }", "variables": {"cursor": "c2", "n": 1}}
        assert body_params(json.dumps(op).encode(), "application/json", rule) == body_params(
            json.dumps(other).encode(), "application/json", rule
        )

    def test_ignore_headers_takes_a_header_out_of_the_tie_break(self) -> None:
        """Two runs' records at the same place in their recordings: headers decide — unless ignored."""
        url = "http://a.test/api"
        first = indexed(
            url,
            record_id="r-a",
            order=0,
            run_id="run-a",
            request_headers={"X-Trace": "zzz"},
            started_at=2.0,
        )
        second = indexed(
            url,
            record_id="r-b",
            order=0,
            run_id="run-b",
            request_headers={"X-Trace": "live"},
            started_at=1.0,
        )
        request = LiveRequest("GET", url, headers={"X-Trace": "live"})
        index = ArchiveIndex([first, second])
        assert Matcher(index).match(request).record == second  # shares X-Trace
        rules = _rules_with("a.test", SiteRule(ignore_headers=["x-*"]))
        # Ignored: the tie falls through to the earlier recording.
        assert Matcher(ArchiveIndex([first, second]), rules=rules).match(request).record == second
        late = indexed(
            url,
            record_id="r-b",
            order=0,
            run_id="run-b",
            request_headers={"X-Trace": "live"},
            started_at=3.0,
        )
        assert Matcher(ArchiveIndex([first, late])).match(request).record == late
        assert Matcher(ArchiveIndex([first, late]), rules=rules).match(request).record == first

    def test_fuzzy_on_opens_the_fuzzy_level_for_documents_and_non_get(self) -> None:
        recorded = indexed("http://a.test/page?x=1&y=2", record_id="r1", order=0)
        document = LiveRequest("GET", "http://a.test/page?x=1&y=3", is_document=True)
        config = MatcherConfig(levels=["exact", "normalized"])
        assert Matcher(ArchiveIndex([recorded]), config).match(document).reason == "no_match"
        rules = _rules_with("a.test", SiteRule(fuzzy=True))
        hit = Matcher(ArchiveIndex([recorded]), config, rules).match(document)
        assert (hit.record, hit.level, hit.fuzzy_score) == (recorded, "fuzzy", 0.5)
        posted = indexed(
            "http://a.test/api?x=1",
            record_id="r2",
            order=0,
            method="POST",
            body=b"a=1",
            content_type="application/x-www-form-urlencoded",
        )
        live_post = LiveRequest(
            "POST",
            "http://a.test/api?x=2",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=b"a=1",
        )
        assert Matcher(ArchiveIndex([posted]), config).match(live_post).reason == "no_match"
        assert Matcher(ArchiveIndex([posted]), config, rules).match(live_post).level == "fuzzy"

    def test_synthesize_204(self) -> None:
        rule = SiteRule(synthesize_204=["https://stats.example.com/*", "*/collect?*"])
        assert rule.synthesizes_204("https://stats.example.com/v1/event")
        assert rule.synthesizes_204("https://example.com/collect?v=2#frag")
        assert not rule.synthesizes_204("https://example.com/api/items")

    def test_response_echo_copies_the_request_id(self) -> None:
        rule = SiteRule(
            response_echo=[
                EchoRule(
                    url="https://example.com/aura*",
                    request_path="message.actions.*.id",
                    response_path="actions.*.id",
                )
            ]
        )
        request = "message=" + json.dumps({"actions": [{"id": "101;a"}, {"id": "102;a"}]})
        response = json.dumps(
            {"actions": [{"id": "7;a", "state": "SUCCESS"}, {"id": "8;a", "state": "SUCCESS"}]}
        ).encode()
        echoed = json.loads(
            rule.echo(
                "https://example.com/aura?r=1",
                request.encode(),
                "application/x-www-form-urlencoded",
                response,
            )
        )
        assert [action["id"] for action in echoed["actions"]] == ["101;a", "102;a"]
        assert [action["state"] for action in echoed["actions"]] == ["SUCCESS", "SUCCESS"]

    def test_response_echo_broadcasts_one_value_and_leaves_other_responses_alone(self) -> None:
        rule = SiteRule(
            response_echo=[EchoRule(url="*", request_path="requestId", response_path="items.*.rid")]
        )
        response = json.dumps({"items": [{"rid": "x"}, {"rid": "y"}]}).encode()
        out = json.loads(
            rule.echo("http://a.test/x", b'{"requestId": "r9"}', "application/json", response)
        )
        assert [item["rid"] for item in out["items"]] == ["r9", "r9"]
        # Not JSON, no value in the request, or nothing to change: the bytes come back untouched.
        assert rule.echo("http://a.test/x", b'{"requestId": "r9"}', None, b"<html>") == b"<html>"
        assert rule.echo("http://a.test/x", b'{"other": 1}', None, response) == response
        same = json.dumps({"items": [{"rid": "r9"}]}, indent=2).encode()
        assert rule.echo("http://a.test/x", b'{"requestId": "r9"}', None, same) == same
        # Echo never adds a key the response did not have.
        bare = b'{"items": [{}]}'
        assert rule.echo("http://a.test/x", b'{"requestId": "r9"}', None, bare) == bare
        # A URL the rule does not name.
        scoped = SiteRule(
            response_echo=[
                EchoRule(
                    url="http://b.test/*", request_path="requestId", response_path="items.*.rid"
                )
            ]
        )
        assert scoped.echo("http://a.test/x", b'{"requestId": "r9"}', None, response) == response


class TestLoading:
    def test_a_bare_yaml_on_is_true(self, tmp_path: Path) -> None:
        rules = SiteRules.load(write_rules(tmp_path / "r", {"a.test.yaml": "fuzzy: on\n"}))
        rule = rules.for_host("a.test")
        assert rule is not None and rule.fuzzy is True

    def test_an_empty_file_is_a_rule_that_changes_nothing(self, tmp_path: Path) -> None:
        rules = SiteRules.load(write_rules(tmp_path / "r", {"a.test.yaml": ""}))
        assert rules.for_host("a.test") == SiteRule()

    @pytest.mark.parametrize(
        ("text", "complaint"),
        [
            ("ignore_queries: [a]\n", "not a valid rule"),
            ("fuzzy: maybe\n", "not a valid rule"),
            ("- a\n- b\n", "not a mapping"),
            ("ignore_query: [a\n", "not YAML"),
            ("response_echo: [{url: '*', request_path: a}]\n", "not a valid rule"),
        ],
    )
    def test_what_loading_refuses_names_the_file(
        self, tmp_path: Path, text: str, complaint: str
    ) -> None:
        root = write_rules(tmp_path / "r", {"bad.test.yaml": text})
        with pytest.raises(SiteRuleError, match=complaint) as caught:
            SiteRules.load(root)
        assert "bad.test.yaml" in str(caught.value)

    def test_a_missing_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SiteRuleError, match="does not exist"):
            SiteRules.load(tmp_path / "nowhere")


class TestTheDigest:
    def test_stable_and_sensitive_to_every_rule_file(self, tmp_path: Path) -> None:
        files = {"a.test.yaml": "fuzzy: on\n", "b.test.yaml": "ignore_query: [x]\n"}
        one = SiteRules.load(write_rules(tmp_path / "one", files)).sha256
        two = SiteRules.load(
            write_rules(tmp_path / "two", dict(reversed(list(files.items()))))
        ).sha256
        assert one is not None and one == two
        changed = SiteRules.load(
            write_rules(tmp_path / "three", {**files, "b.test.yaml": "ignore_query: [y]\n"})
        ).sha256
        renamed = SiteRules.load(
            write_rules(
                tmp_path / "four",
                {"a.test.yaml": "fuzzy: on\n", "c.test.yaml": "ignore_query: [x]\n"},
            )
        ).sha256
        added = SiteRules.load(write_rules(tmp_path / "five", {**files, "c.test.yaml": ""})).sha256
        assert len({one, changed, renamed, added}) == 4

    def test_files_that_are_not_rules_do_not_count(self, tmp_path: Path) -> None:
        files = {"a.test.yaml": "fuzzy: on\n"}
        plain = SiteRules.load(write_rules(tmp_path / "one", files)).sha256
        with_readme = SiteRules.load(
            write_rules(tmp_path / "two", {**files, "README.md": "docs", "x.yml": "fuzzy: on"})
        ).sha256
        assert plain == with_readme

    def test_the_shipped_directory_loads_every_rule(self) -> None:
        """Every shipped ``<domain>.yaml`` is a valid rule; its digest names the directory."""
        shipped = Path(__file__).resolve().parents[3] / "configs" / "replay_rules"
        rules = SiteRules.load(shipped)
        files = sorted(path.name.removesuffix(".yaml") for path in shipped.glob("*.yaml"))
        assert files and rules.domains() == files
        assert rules.sha256 is not None and len(rules.sha256) == 64

    @pytest.mark.parametrize(
        ("host", "parameter"),
        [
            ("erebor.douban.com", "callback"),
            ("sso.le.com", "jsonp"),
            ("anonflow2.eastmoney.com", "callback"),
        ],
    )
    def test_the_shipped_random_callback_parameters_are_ignored(
        self, host: str, parameter: str
    ) -> None:
        """Safe since the replay writes the requested callback name into the answer."""
        shipped = Path(__file__).resolve().parents[3] / "configs" / "replay_rules"
        rule = SiteRules.load(shipped).for_host(host)
        assert rule is not None and rule.ignores_query(parameter)


def _rules_with(domain: str, rule: SiteRule) -> SiteRules:
    return SiteRules({domain: rule}, sha256="test")

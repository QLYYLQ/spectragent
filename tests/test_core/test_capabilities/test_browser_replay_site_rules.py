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
import yaml
from pydantic import ValidationError

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.canonical import body_params, request_keys
from spectragent.core.capabilities.browser.replay.matcher import (
    LiveRequest,
    Matcher,
    MatcherConfig,
)
from spectragent.core.capabilities.browser.replay.site_rules import (
    EchoRule,
    RequestScope,
    SiteRule,
    SiteRuleError,
    SiteRules,
    SiteRulesSnapshot,
)
from tests._helpers.replay_archive import indexed


def write_rules(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text)
    return root


class TestEachKey:
    def test_ignore_query(self) -> None:
        rule = SiteRule(
            id="policy", exhaustion="repeat_last", unavailable="miss", ignore_query=("session*",)
        )
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
        rule = SiteRule(
            id="policy",
            exhaustion="repeat_last",
            unavailable="miss",
            ignore_body_paths=(
                "client.ts",
                "nonce",
                "variables.cursor",
            ),
        )
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
        assert (
            Matcher(
                index,
                rules=_rules_with(
                    "a.test", SiteRule(id="policy", exhaustion="repeat_last", unavailable="miss")
                ),
            )
            .match(request)
            .record
            == second
        )  # shares X-Trace
        rules = _rules_with(
            "a.test",
            SiteRule(
                id="policy", exhaustion="repeat_last", unavailable="miss", ignore_headers=("x-*",)
            ),
        )
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
        assert (
            Matcher(
                ArchiveIndex([first, late]),
                rules=_rules_with(
                    "a.test", SiteRule(id="policy", exhaustion="repeat_last", unavailable="miss")
                ),
            )
            .match(request)
            .record
            == late
        )
        assert Matcher(ArchiveIndex([first, late]), rules=rules).match(request).record == first

    def test_fuzzy_on_opens_the_fuzzy_level_for_documents_and_non_get(self) -> None:
        recorded = indexed("http://a.test/page?x=1&y=2", record_id="r1", order=0)
        document = LiveRequest("GET", "http://a.test/page?x=1&y=3", is_document=True)
        config = MatcherConfig(
            levels=(
                "exact",
                "normalized",
                "fuzzy",
            )
        )
        assert Matcher(ArchiveIndex([recorded]), config).match(document).reason == "no_match"
        rules = _rules_with(
            "a.test",
            SiteRule(id="policy", exhaustion="repeat_last", unavailable="miss", fuzzy=True),
        )
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
        rule = SiteRule(
            id="policy",
            exhaustion="repeat_last",
            unavailable="miss",
            synthesize_204=(
                "https://stats.example.com/*",
                "*/collect?*",
            ),
        )
        assert rule.synthesizes_204("https://stats.example.com/v1/event")
        assert rule.synthesizes_204("https://example.com/collect?v=2#frag")
        assert not rule.synthesizes_204("https://example.com/api/items")

    def test_response_echo_copies_the_request_id(self) -> None:
        rule = SiteRule(
            id="policy",
            exhaustion="repeat_last",
            unavailable="miss",
            response_echo=(
                EchoRule(
                    url="https://example.com/aura*",
                    request_path="message.actions.*.id",
                    response_path="actions.*.id",
                ),
            ),
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
            id="policy",
            exhaustion="repeat_last",
            unavailable="miss",
            response_echo=(
                EchoRule(url="*", request_path="requestId", response_path="items.*.rid"),
            ),
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
            id="policy",
            exhaustion="repeat_last",
            unavailable="miss",
            response_echo=(
                EchoRule(
                    url="http://b.test/*", request_path="requestId", response_path="items.*.rid"
                ),
            ),
        )
        assert scoped.echo("http://a.test/x", b'{"requestId": "r9"}', None, response) == response


def _rules_with(domain: str, rule: SiteRule) -> SiteRules:
    return SiteRules([rule.model_copy(update={"scope": RequestScope(host=domain)})])


class TestRequestSelection:
    def test_endpoint_method_and_effective_port_select_independently(self) -> None:
        rules = SiteRules(
            [
                SiteRule(id="fallback", priority=-1, exhaustion="repeat_last", unavailable="miss"),
                SiteRule(
                    id="read",
                    scope=RequestScope(host="api.test", path="/stock", method="GET", port=443),
                    exhaustion="miss",
                    unavailable="miss",
                ),
                SiteRule(
                    id="write",
                    scope=RequestScope(host="api.test", path="/stock", method="POST", port=443),
                    exhaustion="miss",
                    unavailable="miss",
                ),
                SiteRule(
                    id="alternate",
                    scope=RequestScope(url="https://api.test:8443/stock*"),
                    exhaustion="repeat_last",
                    unavailable="miss",
                ),
            ]
        )
        assert rules.require_for_request("GET", "https://API.test/stock").id == "read"
        assert rules.require_for_request("POST", "https://api.test/stock").id == "write"
        assert rules.require_for_request("GET", "https://api.test:8443/stock").id == "alternate"
        assert rules.require_for_request("GET", "https://api.test/other").id == "fallback"

    def test_highest_priority_wins_without_merging(self) -> None:
        broad = SiteRule(
            id="broad", exhaustion="repeat_last", unavailable="miss", ignore_query=("id",)
        )
        fine = SiteRule(
            id="fine",
            priority=10,
            exhaustion="miss",
            unavailable="miss",
            scope=RequestScope(path="/stock"),
        )
        for rules in (SiteRules([broad, fine]), SiteRules([fine, broad])):
            selected = rules.require_for_request("GET", "https://a.test/stock")
            assert selected == fine and not selected.ignores_query("id")

    def test_equal_priority_conflict_names_both_rules_and_request(self) -> None:
        rules = SiteRules(
            [
                SiteRule(
                    id="left",
                    exhaustion="miss",
                    unavailable="miss",
                    scope=RequestScope(host="a.test"),
                ),
                SiteRule(
                    id="right",
                    exhaustion="repeat_last",
                    unavailable="miss",
                    scope=RequestScope(path="/stock"),
                ),
            ]
        )
        with pytest.raises(SiteRuleError, match="left.*right.*GET.*stock"):
            rules.validate_requests([("GET", "https://a.test/stock")])

    def test_host_globs_never_imply_a_domain_suffix(self) -> None:
        rules = SiteRules(
            [
                SiteRule(
                    id="sub",
                    exhaustion="miss",
                    unavailable="miss",
                    scope=RequestScope(host="*.a.test"),
                )
            ]
        )
        assert rules.for_request("GET", "http://www.a.test/") is not None
        assert rules.for_request("GET", "http://a.test/") is None
        assert rules.for_request("GET", "http://nota.test/") is None

    def test_keep_fields_override_ignore_globs(self) -> None:
        rule = SiteRule(
            id="keep",
            exhaustion="miss",
            unavailable="miss",
            ignore_query=("*",),
            keep_query=("id",),
            ignore_body_paths=("client.*",),
            keep_body_paths=("client.product",),
            ignore_headers=("x-*",),
            keep_headers=("X-Variant",),
        )
        assert rule.ignores_query("ts") and not rule.ignores_query("id")
        assert rule.ignores_body_path("client.ts") and not rule.ignores_body_path("client.product")
        assert rule.ignores_header("X-Trace") and not rule.ignores_header("x-variant")


class TestRuleInput:
    @pytest.mark.parametrize(
        "body",
        [
            "rules: [{id: a}]",
            "rules: [{id: a, exhaustion: invalid, unavailable: miss}]",
            "rules: [{id: a, exhaustion: miss, unavailable: invalid}]",
            "rules: []",
            "",
            "[]",
            "rules: [{id: a, exhaustion: miss, unavailable: miss, unknown: true}]",
        ],
    )
    def test_invalid_or_undeclared_policy_is_rejected_with_file(
        self, tmp_path: Path, body: str
    ) -> None:
        root = write_rules(tmp_path, {"policy.yaml": body})
        with pytest.raises(SiteRuleError, match="policy.yaml"):
            SiteRules.load(root)

    def test_a_rule_without_its_unavailable_policy_is_refused_naming_it(
        self, tmp_path: Path
    ) -> None:
        """Like ``exhaustion``, there is no default: a rule that does not say is refused."""
        root = write_rules(tmp_path, {"policy.yaml": "rules: [{id: a, exhaustion: repeat_last}]"})
        with pytest.raises(SiteRuleError, match=r"policy\.yaml(.|\n)*unavailable"):
            SiteRules.load(root)
        with pytest.raises(ValidationError, match="unavailable"):
            SiteRule.model_validate({"id": "a", "exhaustion": "miss"})

    def test_missing_directory_and_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SiteRuleError, match="does not exist"):
            SiteRules.load(tmp_path / "absent")
        with pytest.raises(SiteRuleError, match="duplicate.*same"):
            SiteRules(
                [
                    SiteRule(id="same", exhaustion="miss", unavailable="miss"),
                    SiteRule(id="same", exhaustion="miss", unavailable="miss"),
                ]
            )

    def test_rules_cannot_expand_run_levels(self) -> None:
        for rule in (
            SiteRule(id="fuzzy", exhaustion="miss", unavailable="miss", fuzzy=True),
            SiteRule(
                id="normalized", exhaustion="miss", unavailable="miss", levels=("normalized",)
            ),
        ):
            with pytest.raises(SiteRuleError, match="matcher.levels"):
                SiteRules([rule]).validate_levels(["exact"])

    def test_known_requests_require_coverage_but_unknown_requests_remain_diagnostic(self) -> None:
        rules = SiteRules(
            [
                SiteRule(
                    id="known",
                    exhaustion="miss",
                    unavailable="miss",
                    scope=RequestScope(path="/known"),
                )
            ]
        )
        assert rules.for_request("GET", "http://a.test/unknown") is None
        with pytest.raises(SiteRuleError, match="exhaustion.*GET.*unknown"):
            rules.validate_requests([("GET", "http://a.test/unknown")])


class TestFrozenSnapshot:
    def test_editing_and_deleting_files_never_changes_frozen_behavior(self, tmp_path: Path) -> None:
        text = "# exact source text\nrules:\n  - id: original\n    exhaustion: repeat_last\n    unavailable: miss\n    ignore_query: [ts]\n"
        root = write_rules(tmp_path, {"policy.yaml": text})
        rules = SiteRules.load(root)
        frozen = rules.snapshot()
        (root / "policy.yaml").write_text(
            "rules: [{id: changed, exhaustion: miss, unavailable: miss}]"
        )
        assert SiteRules.load(root).sha256 != rules.sha256
        (root / "policy.yaml").unlink()
        restored = SiteRules.from_snapshot(
            SiteRulesSnapshot.model_validate_json(frozen.model_dump_json())
        )
        assert restored.require_for_request("GET", "http://a.test/").ignores_query("ts")
        assert rules.require_for_request("GET", "http://a.test/").exhaustion == "repeat_last"
        assert restored.snapshot().files[0].text == text
        assert restored.sha256 == rules.sha256

    def test_body_and_collection_digests_are_verified(self) -> None:
        rules = SiteRules([SiteRule(id="one", exhaustion="miss", unavailable="miss")])
        snapshot = rules.snapshot()
        wrong_file = snapshot.files[0].model_copy(update={"text": "changed"})
        for bad in (
            snapshot.model_copy(update={"sha256": "0" * 64}),
            snapshot.model_copy(update={"files": (wrong_file,)}),
        ):
            with pytest.raises(SiteRuleError, match="sha256 mismatch"):
                SiteRules.from_snapshot(bad)

    def test_collections_and_nested_rules_are_immutable(self) -> None:
        rules = SiteRules(
            [SiteRule(id="one", exhaustion="miss", unavailable="miss", ignore_query=("ts",))]
        )
        rule = rules.require_for_request("GET", "http://a.test/")
        assert isinstance(rule.ignore_query, tuple)
        with pytest.raises(ValidationError, match="frozen"):
            rule.exhaustion = "repeat_last"

    def test_shipped_directory_has_explicit_fallback_and_no_stock_id_erasure(self) -> None:
        rules = SiteRules.load(Path(__file__).resolve().parents[3] / "configs" / "replay_rules")
        selected = rules.require_for_request("POST", "https://m.ctrip.com/restapi/business/stock")
        assert selected.exhaustion == "repeat_last"
        assert not selected.ignores_body_path("id")

    def test_every_shipped_rule_writes_its_unavailable_policy_and_the_baseline_serves_identical(
        self,
    ) -> None:
        """Each rule file states the policy itself; no rule relies on a parse-time default."""
        root = Path(__file__).resolve().parents[3] / "configs" / "replay_rules"
        written = [
            (path.name, rule)
            for path in sorted(root.glob("*.yaml"))
            for rule in yaml.safe_load(path.read_text()).get("rules", [])
        ]
        assert written
        assert [name for name, rule in written if "unavailable" not in rule] == []
        baseline = SiteRules.load(root).require_for_request("GET", "https://any.test/")
        assert (baseline.id, baseline.unavailable) == ("baseline", "serve_identical")

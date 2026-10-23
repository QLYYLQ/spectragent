"""Page-URL rules: the parameters restoration's URL comparison leaves out, and nothing else.

A site may write a volatile parameter into its own page URL with no request
(``docs/architecture_design.md`` §4.14 "恢复：重定位与校验"). The replay rule set
declares those parameters in its ``page_urls`` section; ``compare_page_urls`` leaves
exactly them out, for pages both URLs of which are in the rule's scope, and compares
the rest byte for byte. Request matching never reads the section.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from spectragent.core.capabilities.browser.replay.archive_index import ArchiveIndex
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, Matcher
from spectragent.core.capabilities.browser.replay.page_urls import (
    PageScope,
    PageUrlRule,
    compare_page_urls,
)
from spectragent.core.capabilities.browser.replay.site_rules import (
    SiteRule,
    SiteRuleError,
    SiteRules,
    SiteRulesSnapshot,
)
from tests._helpers.replay_archive import indexed

SHIPPED = Path(__file__).resolve().parents[3] / "configs" / "replay_rules"
SEARCH = "https://search.test/all"
RULE = PageUrlRule(
    id="search-vt", scope=PageScope(host="search.test", path="/all"), ignore_query=("vt",)
)


def compare(recorded: str, restored: str, rules: tuple[PageUrlRule, ...] = (RULE,)) -> tuple:
    result = compare_page_urls(recorded, restored, rules)
    return result.match, result.ignored_query, result.rule_ids


class TestCompare:
    def test_a_declared_parameter_alone_is_left_out(self) -> None:
        assert compare(
            f"{SEARCH}?vt=01672110&keyword=kettle", f"{SEARCH}?vt=01660827&keyword=kettle"
        ) == (
            True,
            ("vt",),
            ("search-vt",),
        )

    def test_present_on_one_side_only_is_left_out_too(self) -> None:
        assert compare(f"{SEARCH}?vt=1&keyword=kettle", f"{SEARCH}?keyword=kettle")[0] is True
        assert compare(f"{SEARCH}?vt=1", SEARCH)[0] is True

    @pytest.mark.parametrize(
        "restored",
        [
            f"{SEARCH}?vt=2&keyword=teapot&order=click",  # another value of a kept parameter
            f"{SEARCH}?vt=2&keyword=kettle&order=click&page=2",  # an added parameter
            f"{SEARCH}?vt=2&keyword=kettle",  # a removed parameter
            f"{SEARCH}?vt=2&order=click&keyword=kettle",  # the order of the kept ones
            f"{SEARCH}?vt=2&keyword=kett%6Ce&order=click",  # another spelling of the same value
            f"{SEARCH}?vt=2&keyword=kettle&order=click#reviews",  # the fragment
            "http://search.test/all?vt=2&keyword=kettle&order=click",  # the scheme
            "https://search.test:8443/all?vt=2&keyword=kettle&order=click",  # the port
        ],
    )
    def test_every_other_difference_still_mismatches(self, restored: str) -> None:
        match, ignored, rules = compare(f"{SEARCH}?vt=1&keyword=kettle&order=click", restored)
        assert match is False
        assert ignored == ("vt",) and rules == ("search-vt",), "what was left out is still told"

    @pytest.mark.parametrize(
        ("recorded", "restored"),
        [
            (
                "https://www.search.test/all?vt=1",
                "https://www.search.test/all?vt=2",
            ),  # no subdomains
            ("https://search.test/video?vt=1", "https://search.test/video?vt=2"),  # another path
            ("https://search.test/all?vt=1", "https://search.test/video?vt=1"),  # one side in scope
        ],
    )
    def test_outside_the_scope_of_either_url_the_urls_compare_exactly(
        self, recorded: str, restored: str
    ) -> None:
        assert compare(recorded, restored) == (False, (), ())

    def test_without_a_rule_or_a_declared_parameter_the_urls_compare_exactly(self) -> None:
        assert compare(f"{SEARCH}?vt=1", f"{SEARCH}?vt=2", ()) == (False, (), ())
        assert compare(f"{SEARCH}?keyword=a", f"{SEARCH}?keyword=a") == (True, (), ())
        assert compare(f"{SEARCH}?", SEARCH) == (False, (), ()), "byte for byte"

    def test_names_are_decoded_globs_and_every_matching_rule_contributes(self) -> None:
        tracking = PageUrlRule(
            id="search-tracking", scope=PageScope(host="*.test"), ignore_query=("spm_*",)
        )
        recorded = f"{SEARCH}?v%74=1&spm_from=a&keyword=kettle"
        restored = f"{SEARCH}?vt=2&spm_id=b&keyword=kettle"
        assert compare(recorded, restored, (RULE, tracking)) == (
            True,
            ("spm_from", "spm_id", "vt"),
            ("search-tracking", "search-vt"),
        )

    def test_a_url_that_does_not_parse_compares_exactly(self) -> None:
        assert compare(
            "https://search.test:port/all?vt=1", "https://search.test:port/all?vt=2"
        ) == (
            False,
            (),
            (),
        )

    def test_a_rule_names_a_host_and_at_least_one_parameter(self) -> None:
        for bad in (
            {"id": "a", "scope": {}, "ignore_query": ["vt"]},
            {"id": "a", "scope": {"host": ""}, "ignore_query": ["vt"]},
            {"id": "a", "scope": {"host": "a.test"}, "ignore_query": []},
            {"id": "a", "scope": {"host": "a.test", "method": "GET"}, "ignore_query": ["vt"]},
        ):
            with pytest.raises(ValidationError):
                PageUrlRule.model_validate(bad)


PAGE_URLS = "page_urls:\n  - id: search-vt\n    scope: {host: search.test, path: /all}\n    ignore_query: [vt]\n"
REQUESTS = "rules: [{id: fallback, scope: {}, exhaustion: repeat_last, unavailable: miss}]\n"


class TestTheRuleSet:
    def test_a_file_may_hold_only_page_url_rules_and_they_are_frozen_with_the_set(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "requests.yaml").write_text(REQUESTS)
        (tmp_path / "page_urls.yaml").write_text(PAGE_URLS)
        rules = SiteRules.load(tmp_path)
        assert [rule.id for rule in rules.page_urls] == ["search-vt"]
        assert len(rules) == 1, "a page-URL rule is not a request rule"
        frozen = rules.snapshot()
        (tmp_path / "page_urls.yaml").write_text(PAGE_URLS.replace("[vt]", "[vt, page]"))
        assert SiteRules.load(tmp_path).sha256 != rules.sha256, "the digest covers them"
        (tmp_path / "page_urls.yaml").unlink()
        restored = SiteRules.from_snapshot(
            SiteRulesSnapshot.model_validate_json(frozen.model_dump_json())
        )
        assert restored.page_urls == rules.page_urls

    def test_a_file_and_a_section_together_and_identities_shared_with_request_rules(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "site.yaml").write_text(REQUESTS + PAGE_URLS)
        assert [rule.id for rule in SiteRules.load(tmp_path).page_urls] == ["search-vt"]
        (tmp_path / "site.yaml").write_text(REQUESTS + PAGE_URLS.replace("search-vt", "fallback"))
        with pytest.raises(SiteRuleError, match="duplicate.*fallback"):
            SiteRules.load(tmp_path)
        with pytest.raises(SiteRuleError, match="duplicate.*search-vt"):
            SiteRules(page_urls=[RULE, RULE])

    @pytest.mark.parametrize(
        "body",
        [
            "page_urls: []",
            "rules: []\npage_urls: []",
            "page_urls: [{id: a, scope: {host: a.test}, ignore_query: [vt], unknown: true}]",
            "page_urls: [{id: a, scope: {path: /all}, ignore_query: [vt]}]",
        ],
    )
    def test_an_empty_or_invalid_section_is_refused_naming_the_file(
        self, tmp_path: Path, body: str
    ) -> None:
        (tmp_path / "page_urls.yaml").write_text(body)
        with pytest.raises(SiteRuleError, match="page_urls.yaml"):
            SiteRules.load(tmp_path)

    def test_request_matching_never_reads_them(self) -> None:
        """A page-URL rule for the search page does not make its document request match."""
        policy = SiteRule(id="fallback", exhaustion="repeat_last", unavailable="miss")
        rules = SiteRules([policy], page_urls=[RULE])
        recorded = indexed(f"{SEARCH}?keyword=kettle", record_id="doc", order=0, rule=policy)
        result = Matcher(ArchiveIndex([recorded]), rules=rules).match(
            LiveRequest("GET", f"{SEARCH}?vt=1&keyword=kettle")
        )
        assert result.reason == "no_match"


# ── the published declarations ────────────────────────────────────────

BILIBILI = "https://search.bilibili.com/all"
BILIBILI_QUERY = (
    "keyword=AI%E7%BB%98%E7%94%BB&from_source=web_search&spm_id_from=333.1007&search_source=5"
)
SCREENER = "https://search.10jqka.com.cn/screener/result"
SCREENER_QUERY = "w=%E4%B8%8A%E8%AF%81%E6%8C%87%E6%95%B0&querytype=stock"


def shipped() -> tuple[PageUrlRule, ...]:
    return SiteRules.load(SHIPPED).page_urls


@pytest.mark.parametrize(
    ("recorded", "restored", "rule_id"),
    [
        (
            f"{BILIBILI}?vt=01672110&{BILIBILI_QUERY}",
            f"{BILIBILI}?vt=01660827&{BILIBILI_QUERY}",
            "bilibili-search-vt",
        ),
        (
            f"{SCREENER}?{SCREENER_QUERY}&sign=1792701901115",
            f"{SCREENER}?{SCREENER_QUERY}&sign=1792701825777",
            "tonghuashun-screener-sign",
        ),
    ],
)
def test_the_published_rules_leave_out_the_page_written_parameter(
    recorded: str, restored: str, rule_id: str
) -> None:
    result = compare_page_urls(recorded, restored, shipped())
    assert result.match is True and result.rule_ids == (rule_id,)


@pytest.mark.parametrize(
    ("recorded", "restored"),
    [
        *(
            (
                f"{BILIBILI}?vt=1&{BILIBILI_QUERY}",
                f"{BILIBILI}?vt=2&{BILIBILI_QUERY}".replace(old, new),
            )
            for old, new in (
                ("keyword=AI", "keyword=BI"),
                ("spm_id_from=333.1007", "spm_id_from=333.788"),
                ("search_source=5", "search_source=6"),
                ("from_source=web_search", "from_source=webtop_search"),
            )
        ),
        (f"{BILIBILI}?vt=1&{BILIBILI_QUERY}", f"{BILIBILI}?vt=2&{BILIBILI_QUERY}&order=click"),
        (f"{BILIBILI}?vt=1&{BILIBILI_QUERY}", f"{BILIBILI}?vt=2&{BILIBILI_QUERY}&page=2"),
        (f"{SCREENER}?{SCREENER_QUERY}&sign=1", f"{SCREENER}?w=other&querytype=stock&sign=2"),
        (f"{SCREENER}?{SCREENER_QUERY}&sign=1", f"{SCREENER}?{SCREENER_QUERY[:-5]}fund&sign=2"),
        # Other pages of the same sites keep these parameters.
        ("https://www.bilibili.com/?vt=1", "https://www.bilibili.com/?vt=2"),
        (
            "https://search.10jqka.com.cn/search/result?sign=1",
            "https://search.10jqka.com.cn/search/result?sign=2",
        ),
    ],
)
def test_the_published_rules_keep_every_content_parameter(recorded: str, restored: str) -> None:
    assert compare_page_urls(recorded, restored, shipped()).match is False

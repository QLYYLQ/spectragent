"""Matching live requests against a recorded archive (§4.14 "三级匹配", ``matcher.py``).

Each level and its boundary, the fuzzy level's restrictions, chronological
pop with a used set shared across levels, the deliberate ``exhausted``
repeat, run affinity over a synthetic two-run index, header tie-breaks only
between otherwise-equal candidates, the nearest three on a miss, ``Range``
requests that use no cursor, and ``304`` only for conditional requests.
Records are built in memory with the loader's own canonicaliser
(``tests/_helpers/replay_archive.indexed``).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from spectragent.core.capabilities.browser.replay.archive_index import (
    ArchiveIndex,
    IndexedRecord,
    RangeMap,
    RangeSegment,
)
from spectragent.core.capabilities.browser.replay.matcher import (
    NEAREST,
    LiveRequest,
    Matcher,
    MatcherConfig,
    fuzzy_score,
    parse_range,
)
from spectragent.core.capabilities.browser.replay.site_rules import (
    ExhaustionPolicy,
    MatchLevel,
    RequestScope,
    SiteRule,
    SiteRuleError,
    SiteRules,
)
from tests._helpers.replay_archive import indexed as build_indexed

API = "http://shop.test/api"
FUZZY = MatcherConfig(
    levels=(
        "exact",
        "normalized",
        "fuzzy",
    )
)


def indexed(url: str, **kwargs: Any) -> IndexedRecord:
    """These sequence fixtures explicitly declare their cache-counter semantics."""
    return build_indexed(
        url, rule=SiteRule(id="fixture", exhaustion="repeat_last", ignore_query=("_",)), **kwargs
    )


def build_matcher(
    index: ArchiveIndex, config: MatcherConfig | None = None, rules: SiteRules | None = None
) -> Matcher:
    if rules is None:
        rules = SiteRules([SiteRule(id="fixture", exhaustion="repeat_last", ignore_query=("_",))])
    return Matcher(index, config, rules)


def get(url: str, **headers: str) -> LiveRequest:
    return LiveRequest("GET", url, headers=headers)


def ids(*results: object) -> list[str | None]:
    return [getattr(getattr(r, "record", None), "record_id", None) for r in results]


class TestLevels:
    def test_exact(self) -> None:
        record = indexed(f"{API}?id=1", record_id="r1", order=0)
        result = build_matcher(ArchiveIndex([record])).match(get(f"{API}?id=1"))
        assert (result.record, result.level, result.exhausted, result.reason) == (
            record,
            "exact",
            False,
            None,
        )
        assert result.servable and result.fuzzy_score is None

    def test_normalized(self) -> None:
        record = indexed(f"{API}?id=1&_=111", record_id="r1", order=0)
        result = build_matcher(ArchiveIndex([record])).match(get(f"{API}?_=222&id=1"))
        assert (result.record, result.level) == (record, "normalized")

    def test_fuzzy_needs_its_level(self) -> None:
        record = indexed(f"{API}?a=1&b=2", record_id="r1", order=0)
        live = get(f"{API}?a=1&b=3")
        assert build_matcher(ArchiveIndex([record])).match(live).reason == "no_match"
        result = build_matcher(ArchiveIndex([record]), FUZZY).match(live)
        assert (result.record, result.level, result.fuzzy_score) == (record, "fuzzy", 0.5)

    def test_a_level_left_out_is_not_tried(self) -> None:
        record = indexed(f"{API}?id=1", record_id="r1", order=0)
        normalized_only = MatcherConfig(levels=("normalized",))
        result = build_matcher(ArchiveIndex([record]), normalized_only).match(get(f"{API}?id=1"))
        assert result.level == "normalized"

    def test_fuzzy_is_same_host_same_path_same_method(self) -> None:
        records = [
            indexed("http://other.test/api?a=1", record_id="host", order=0),
            indexed("http://shop.test/apix?a=1", record_id="path", order=1),
            indexed(f"{API}?a=1", record_id="method", order=2, method="DELETE"),
        ]
        assert (
            build_matcher(ArchiveIndex(records), FUZZY).match(get(f"{API}?a=2")).reason
            == "no_match"
        )


class TestTheFuzzyThreshold:
    @pytest.mark.parametrize(
        ("threshold", "hit"), [(0.0, True), (0.5, True), (0.5000001, False), (1.0, False)]
    )
    def test_inclusive_boundary(self, threshold: float, hit: bool) -> None:
        record = indexed(f"{API}?a=1&b=2", record_id="r1", order=0)
        config = MatcherConfig(levels=("fuzzy",), fuzzy_threshold=threshold)
        result = build_matcher(ArchiveIndex([record]), config).match(get(f"{API}?a=1&b=3"))
        assert (result.record is not None) is hit

    def test_the_score(self) -> None:
        assert fuzzy_score((), ()) == 1.0
        assert fuzzy_score((("a", "1"),), (("a", "1"),)) == 1.0
        assert fuzzy_score((("a", "1"),), (("a", "2"),)) == 0.0, "M counts from 0, unlike WPR"
        assert fuzzy_score((("a", "1"), ("b", "2")), (("a", "1"),)) == pytest.approx(2 / 3)
        assert fuzzy_score((("a", "1"), ("a", "2")), (("a", "2"), ("a", "1"))) == 1.0
        assert fuzzy_score((("a", "1"), ("a", "2")), (("a", "1"),)) == 0.0

    def test_off_for_documents_and_non_get_unless_the_site_says_on(self) -> None:
        page = indexed(f"{API}?a=1&b=2", record_id="page", order=0)
        document = LiveRequest("GET", f"{API}?a=1&b=3", is_document=True)
        assert build_matcher(ArchiveIndex([page]), FUZZY).match(document).reason == "no_match"
        posted = indexed(f"{API}?a=1&b=2", record_id="post", order=0, method="POST")
        post = LiveRequest("POST", f"{API}?a=1&b=3")
        assert build_matcher(ArchiveIndex([posted]), FUZZY).match(post).reason == "no_match"
        rules = SiteRules([SiteRule(id="fuzzy", exhaustion="repeat_last", fuzzy=True)])
        assert build_matcher(ArchiveIndex([page]), FUZZY, rules).match(document).level == "fuzzy"
        assert build_matcher(ArchiveIndex([posted]), FUZZY, rules).match(post).level == "fuzzy"

    def test_the_best_score_wins_then_headers_then_the_earlier_then_the_smaller_url(self) -> None:
        near = indexed(f"{API}?a=1&b=2&c=9", record_id="near", order=5)
        far = indexed(f"{API}?a=1&b=8&c=9", record_id="far", order=0)
        live = get(f"{API}?a=1&b=2&c=0")
        assert build_matcher(ArchiveIndex([near, far]), FUZZY).match(live).record == near
        # Equal scores: shared headers first ...
        left = indexed(f"{API}?a=1&x=1", record_id="left", order=0, request_headers={"Accept": "a"})
        right = indexed(
            f"{API}?a=1&x=2", record_id="right", order=1, request_headers={"Accept": "b"}
        )
        tied = get(f"{API}?a=1&x=3", Accept="b")
        assert build_matcher(ArchiveIndex([left, right]), FUZZY).match(tied).record == right
        # ... then the earlier recording ...
        plain = get(f"{API}?a=1&x=3")
        assert build_matcher(ArchiveIndex([left, right]), FUZZY).match(plain).record == left
        # ... then the smaller URL.
        b = indexed(f"{API}?a=1&x=b", record_id="b", order=0)
        a = indexed(f"{API}?a=1&x=a", record_id="a", order=0, run_id="run-b")
        assert build_matcher(ArchiveIndex([b, a]), FUZZY).match(plain).record == a


class TestEachRecordIsUsedOnce:
    def test_a_polling_key_is_served_in_recording_order(self) -> None:
        polls = [indexed(f"{API}/poll", record_id=f"p{n}", order=n) for n in (2, 0, 1)]
        matcher = build_matcher(ArchiveIndex(polls))
        results = [matcher.match(get(f"{API}/poll")) for _ in range(3)]
        assert ids(*results) == ["p0", "p1", "p2"]
        assert not any(result.exhausted for result in results)

    def test_the_used_set_is_shared_across_levels(self) -> None:
        one = indexed(f"{API}?_=1", record_id="one", order=0)
        two = indexed(f"{API}?_=2", record_id="two", order=1)
        matcher = build_matcher(ArchiveIndex([one, two]))
        first = matcher.match(get(f"{API}?_=9"))  # L1: the earlier of the two
        second = matcher.match(get(f"{API}?_=1"))  # L0 owns the exhausted sequence.
        assert (ids(first, second), first.level, second.level) == (
            ["one", "one"],
            "normalized",
            "exact",
        )

    def test_exhausted_repeats_the_last_record(self) -> None:
        polls = [indexed(f"{API}/poll", record_id=f"p{n}", order=n) for n in range(2)]
        matcher = build_matcher(ArchiveIndex(polls))
        served = [matcher.match(get(f"{API}/poll")) for _ in range(4)]
        assert ids(*served) == ["p0", "p1", "p1", "p1"]
        assert [result.exhausted for result in served] == [False, False, True, True]
        assert served[3].level == "exact" and served[3].servable

    def test_exhausted_repeats_from_the_best_level_that_had_candidates(self) -> None:
        exact = indexed(f"{API}?_=1", record_id="exact", order=0)
        other = indexed(f"{API}?_=2", record_id="other", order=1)
        matcher = build_matcher(ArchiveIndex([exact, other]))
        assert ids(matcher.match(get(f"{API}?_=1")), matcher.match(get(f"{API}?_=5"))) == [
            "exact",
            "other",
        ]
        again = matcher.match(get(f"{API}?_=1"))
        assert (again.record, again.level, again.exhausted) == (exact, "exact", True)

    def test_a_hit_on_an_unservable_record_carries_its_reason_and_uses_it(self) -> None:
        big = indexed(f"{API}/big", record_id="big", order=0, miss_reason="too_large")
        matcher = build_matcher(ArchiveIndex([big]))
        first = matcher.match(get(f"{API}/big"))
        assert (first.record, first.reason, first.servable) == (big, "too_large", False)
        assert matcher.match(get(f"{API}/big")).exhausted


class TestRunAffinity:
    def test_affinity_on_a_two_run_index(self) -> None:
        """ "Right HTML, wrong JSON": once a run is hit, its records come first."""
        page_a = indexed(
            f"{API}/page",
            record_id="page-a",
            order=0,
            run_id="run-a",
            request_headers={"Accept": "text/html"},
        )
        page_b = indexed(
            f"{API}/page",
            record_id="page-b",
            order=0,
            run_id="run-b",
            request_headers={"Accept": "*/*"},
        )
        data_a = indexed(f"{API}/data", record_id="data-a", order=1, run_id="run-a")
        data_b = indexed(f"{API}/data", record_id="data-b", order=3, run_id="run-b")
        matcher = build_matcher(ArchiveIndex([page_a, page_b, data_a, data_b]))
        assert matcher.affinity is None
        first = matcher.match(get(f"{API}/page", Accept="*/*"))  # tie broken by the header
        assert (first.record, matcher.affinity) == (page_b, "run-b")
        # Without affinity data-a (order 1) would come first; run-b's comes first now.
        assert matcher.match(get(f"{API}/data")).record == data_b
        assert matcher.match(get(f"{API}/data")).record == data_a  # then the other run
        repeat = matcher.match(get(f"{API}/data"))
        assert (repeat.record, repeat.exhausted) == (data_b, True), "the affinity run's last"

    def test_without_a_hit_there_is_no_affinity(self) -> None:
        data_a = indexed(f"{API}/data", record_id="data-a", order=1, run_id="run-a")
        data_b = indexed(f"{API}/data", record_id="data-b", order=3, run_id="run-b")
        matcher = build_matcher(ArchiveIndex([data_b, data_a]))
        assert matcher.match(get(f"{API}/nothing")).reason == "no_match"
        assert matcher.affinity is None
        assert matcher.match(get(f"{API}/data")).record == data_a
        assert matcher.affinity == "run-a"


class TestHeadersOnlyBreakTies:
    def test_recording_order_beats_matching_headers(self) -> None:
        first = indexed(API, record_id="first", order=0, request_headers={"Accept": "x"})
        second = indexed(API, record_id="second", order=1, request_headers={"Accept": "live"})
        matcher = build_matcher(ArchiveIndex([first, second]))
        assert ids(
            matcher.match(get(API, Accept="live")), matcher.match(get(API, Accept="live"))
        ) == [
            "first",
            "second",
        ]

    def test_between_equals_the_shared_headers_decide_never_credentials(self) -> None:
        a = indexed(API, record_id="a", order=0, run_id="run-a", request_headers={"Accept": "x"})
        b = indexed(API, record_id="b", order=0, run_id="run-b", request_headers={"Accept": "y"})
        assert build_matcher(ArchiveIndex([a, b])).match(get(API, Accept="y")).record == b
        assert build_matcher(ArchiveIndex([a, b])).match(get(API, Accept="x")).record == a
        # A stripped header (a credential) never counts, even if a record carried one:
        # otherwise the cookie-carrying record would win; as it is, the ids decide.
        cookie = indexed(
            API, record_id="z-cookie", order=0, run_id="run-c", request_headers={"Cookie": "s=1"}
        )
        plain = indexed(API, record_id="a-plain", order=0, run_id="run-d")
        for records in ([cookie, plain], [plain, cookie]):
            assert (
                build_matcher(ArchiveIndex(records)).match(get(API, Cookie="s=1")).record == plain
            )


class TestTheNearestOnAMiss:
    def test_same_method_same_path_then_score_at_most_three(self) -> None:
        records = [
            indexed(f"{API}?a=1&b=1", record_id="path-close", order=0),
            indexed(f"{API}?z=9", record_id="path-far", order=1),
            indexed("http://shop.test/other?a=1", record_id="other-path", order=2),
            indexed(f"{API}?a=1&b=1", record_id="post", order=3, method="POST"),
            indexed("http://elsewhere.test/api?a=1", record_id="other-host", order=4),
        ]
        matcher = build_matcher(ArchiveIndex(records))
        matcher.match(get(f"{API}?z=9"))  # uses path-far
        miss = matcher.match(get(f"{API}?a=1&b=2"))
        assert miss.reason == "no_match" and miss.record is None
        assert [c.record_id for c in miss.candidates] == ["path-close", "path-far", "other-path"]
        assert len(miss.candidates) == NEAREST
        close, far, _ = miss.candidates
        assert (close.score, close.consumed, far.consumed) == (0.5, False, True)
        assert (close.method, close.url) == ("GET", f"{API}?a=1&b=1")

    def test_a_host_never_seen_has_no_candidates(self) -> None:
        record = indexed(f"{API}?a=1", record_id="r1", order=0)
        miss = build_matcher(ArchiveIndex([record])).match(get("http://nowhere.test/api?a=1"))
        assert (miss.reason, miss.candidates) == ("no_match", ())


class TestRanges:
    @staticmethod
    def _index() -> tuple[ArchiveIndex, IndexedRecord, IndexedRecord]:
        video = "http://shop.test/v.mp4"
        full = indexed(video, record_id="full", order=0, payload_length=10, body_status="ok")
        part = indexed(video, record_id="part", order=1, status=206, payload_length=5)
        key = full.keys.normalized
        ranges = RangeMap(
            {
                key: [
                    RangeSegment(start=0, end=9, total=10, record=full),
                    RangeSegment(start=10, end=14, total=None, record=part),
                ]
            }
        )
        return ArchiveIndex([full], ranges), full, part

    def test_a_range_request_is_served_from_the_map_and_uses_no_cursor(self) -> None:
        index, full, _ = self._index()
        matcher = build_matcher(index)
        ranged = matcher.match(get("http://shop.test/v.mp4", Range="bytes=2-"))
        assert ranged.servable and ranged.range is not None
        assert (ranged.range.start, ranged.range.end, ranged.range.total) == (2, 9, 10)
        assert ranged.record == full and not ranged.exhausted
        whole = matcher.match(get("http://shop.test/v.mp4"))
        assert (whole.record, whole.exhausted) == (full, False), "the range used no cursor"

    def test_suffix_and_closed_ranges_and_clipping(self) -> None:
        index, _, _ = self._index()
        matcher = build_matcher(index)
        suffix = matcher.match(get("http://shop.test/v.mp4", Range="bytes=-3"))
        assert suffix.range is not None and (suffix.range.start, suffix.range.end) == (7, 9)
        closed = matcher.match(get("http://shop.test/v.mp4", Range="bytes=1-4"))
        assert closed.range is not None and (closed.range.start, closed.range.end) == (1, 4)
        assert closed.range.clipped(2).end == 2 and closed.range.clipped(100) is closed.range

    def test_uncovered_and_unknown(self) -> None:
        index, _, _ = self._index()
        matcher = build_matcher(index)
        assert matcher.match(get("http://shop.test/v.mp4", Range="bytes=10-")).reason == (
            "range_uncovered"
        ), "beyond the known total"
        assert matcher.match(get("http://shop.test/v.mp4", Range="bytes=0-1,4-5")).reason == (
            "range_uncovered"
        )
        assert matcher.match(get("http://shop.test/a.mp4", Range="bytes=0-")).reason == "no_match"

    def test_parse_range(self) -> None:
        assert parse_range("bytes=0-") == (0, None)
        assert parse_range("bytes=5-9") == (5, 9)
        assert parse_range("bytes=-500") == (None, 500)
        assert parse_range(" Bytes = 1 - 2 ") == (1, 2)
        for bad in ("bytes=-", "items=0-1", "bytes=0-1,3-4", "bytes=a-b"):
            assert parse_range(bad) is None, bad


class TestNotModified:
    def test_a_304_answers_only_a_conditional_request(self) -> None:
        cached = indexed(f"{API}/logo.png", record_id="cached", order=0, status=304)
        fresh = indexed(f"{API}/logo.png", record_id="fresh", order=1)
        matcher = build_matcher(ArchiveIndex([cached, fresh]))
        assert matcher.match(get(f"{API}/logo.png")).record == fresh
        conditional = build_matcher(ArchiveIndex([cached, fresh]))
        assert (
            conditional.match(get(f"{API}/logo.png", **{"If-None-Match": '"e1"'})).record == cached
        )
        only_304 = build_matcher(ArchiveIndex([cached]))
        assert only_304.match(get(f"{API}/logo.png")).reason == "no_match"


class TestTheConfig:
    def test_defaults(self) -> None:
        config = MatcherConfig()
        assert (config.levels, config.fuzzy_threshold, config.site_rules) == (
            ("exact", "normalized"),
            0.5,
            None,
        )
        assert config.fingerprint() == {
            "levels": ["exact", "normalized"],
            "fuzzy_threshold": 0.5,
            "site_rules": None,
        }

    @pytest.mark.parametrize(
        "bad",
        [
            {"levels": []},
            {"levels": ["exact", "exact"]},
            {"levels": ["fuzzzy"]},
            {"fuzzy_threshold": -0.1},
            {"fuzzy_threshold": 1.1},
            {"extra": 1},
        ],
    )
    def test_refused(self, bad: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            MatcherConfig(**bad)  # type: ignore[arg-type]


class TestExplicitPolicyBoundaries:
    @pytest.mark.parametrize("exhaustion", ["repeat_last", "miss"])
    def test_strong_sequence_exhaustion_does_not_consume_a_weaker_candidate(
        self, exhaustion: ExhaustionPolicy
    ) -> None:
        rule = SiteRule(id="stock", exhaustion=exhaustion, ignore_query=("_",))
        exact = build_indexed(f"{API}?_=1", record_id="exact", order=0, rule=rule)
        other = build_indexed(f"{API}?_=2", record_id="other", order=1, rule=rule)
        matcher = Matcher(ArchiveIndex([exact, other]), rules=SiteRules([rule]))
        assert matcher.match(get(f"{API}?_=1")).record == exact
        exhausted = matcher.match(get(f"{API}?_=1"))
        assert exhausted.level == "exact" and exhausted.exhausted
        assert exhausted.rule_id == "stock" and exhausted.exhaustion_policy == exhaustion
        if exhaustion == "miss":
            assert exhausted.reason == "sequence_exhausted" and exhausted.record is None
        else:
            assert exhausted.record == exact and exhausted.reason is None
        assert matcher.match(get(f"{API}?_=2")).record == other

    def test_endpoint_polling_and_business_sequences_have_separate_policies(self) -> None:
        rules = SiteRules(
            [
                SiteRule(id="poll", scope=RequestScope(path="/poll"), exhaustion="repeat_last"),
                SiteRule(id="stock", scope=RequestScope(path="/stock"), exhaustion="miss"),
            ]
        )
        poll = indexed("http://shop.test/poll", record_id="poll", order=0)
        stock = indexed("http://shop.test/stock", record_id="stock", order=1)
        matcher = Matcher(ArchiveIndex([poll, stock]), rules=rules)
        for record in (poll, stock):
            assert matcher.match(get(record.url)).record == record
        assert matcher.match(get(poll.url)).record == poll
        assert matcher.match(get(stock.url)).reason == "sequence_exhausted"

    def test_no_policy_is_refused_only_when_a_candidate_would_be_served(self) -> None:
        record = indexed(API, record_id="record", order=0)
        matcher = Matcher(ArchiveIndex([record]))
        assert matcher.match(get(API + "/unknown")).reason == "no_match"
        with pytest.raises(SiteRuleError, match="exhaustion.*GET"):
            matcher.match(get(API))

    def test_rule_can_restrict_levels_and_run_order_is_always_strongest_first(self) -> None:
        record = indexed(API, record_id="record", order=0)
        config = MatcherConfig(
            levels=(
                "fuzzy",
                "normalized",
                "exact",
            )
        )
        assert build_matcher(ArchiveIndex([record]), config).match(get(API)).level == "exact"
        rules = SiteRules([SiteRule(id="restricted", exhaustion="miss", levels=("normalized",))])
        assert Matcher(ArchiveIndex([record]), config, rules).match(get(API)).level == "normalized"

    def test_fuzzy_does_not_cross_effective_ports(self) -> None:
        record = indexed("http://shop.test:8080/api?a=1", record_id="record", order=0)
        matcher = build_matcher(
            ArchiveIndex([record]), MatcherConfig(levels=("fuzzy",), fuzzy_threshold=0)
        )
        assert matcher.match(get("http://shop.test:8081/api?a=1")).reason == "no_match"

    def test_allowed_levels_are_frozen_from_the_callers_list(self) -> None:
        levels = ["exact"]
        config = TypeAdapter(MatcherConfig).validate_python({"levels": levels})
        levels.append("fuzzy")
        assert config.levels == ("exact",)
        assert config.fingerprint()["levels"] == ["exact"]


class TestRangeLevelBoundary:
    def test_exact_only_never_uses_normalized_bytes(self) -> None:
        rule = SiteRule(id="video", exhaustion="repeat_last", ignore_query=("ts",))
        record = build_indexed(
            "http://shop.test/v?ts=1", record_id="record", order=0, rule=rule, payload_length=10
        )
        ranges = RangeMap({record.keys.normalized: [RangeSegment(0, 9, 10, record)]})
        index = ArchiveIndex([record], ranges)
        request = get("http://shop.test/v?ts=2", Range="bytes=0-4")
        exact = Matcher(index, MatcherConfig(levels=("exact",)), SiteRules([rule])).match(request)
        assert exact.reason == "match_level_disallowed" and exact.range is None
        normalized = Matcher(index, MatcherConfig(levels=("normalized",)), SiteRules([rule])).match(
            request
        )
        assert normalized.level == "normalized" and normalized.range is not None
        uncovered = Matcher(index, MatcherConfig(levels=("normalized",)), SiteRules([rule])).match(
            get("http://shop.test/v?ts=2", Range="bytes=20-30")
        )
        assert uncovered.reason == "range_uncovered"

    def test_exact_ranges_never_join_bytes_from_another_normalized_url(self) -> None:
        rule = SiteRule(id="video", exhaustion="miss", ignore_query=("ts",))
        first = build_indexed("http://shop.test/v?ts=1", record_id="first", order=0, rule=rule)
        other = build_indexed("http://shop.test/v?ts=2", record_id="other", order=1, rule=rule)
        ranges = RangeMap(
            {first.keys.normalized: [RangeSegment(0, 4, 10, first), RangeSegment(5, 9, 10, other)]}
        )
        index = ArchiveIndex([], ranges)
        request = get(first.url, Range="bytes=5-9")
        exact = Matcher(
            index,
            MatcherConfig(
                levels=(
                    "exact",
                    "normalized",
                )
            ),
            SiteRules([rule]),
        ).match(request)
        assert exact.reason == "range_uncovered"
        normalized = Matcher(index, MatcherConfig(levels=("normalized",)), SiteRules([rule])).match(
            request
        )
        assert normalized.range is not None and normalized.range.parts[0][0] == other

    def test_rule_level_restrictions_apply_to_ranges(self) -> None:
        rule = SiteRule(
            id="video", exhaustion="repeat_last", levels=("exact",), ignore_query=("ts",)
        )
        record = build_indexed("http://shop.test/v?ts=1", record_id="record", order=0, rule=rule)
        index = ArchiveIndex(
            [], RangeMap({record.keys.normalized: [RangeSegment(0, 9, 10, record)]})
        )
        result = Matcher(
            index,
            MatcherConfig(
                levels=(
                    "exact",
                    "normalized",
                )
            ),
            SiteRules([rule]),
        ).match(get("http://shop.test/v?ts=2", Range="bytes=0-4"))
        assert result.reason == "match_level_disallowed"


class TestRequestRuleIsolation:
    @pytest.fixture
    def rules(self) -> SiteRules:
        return SiteRules(
            [
                SiteRule(
                    id=f"account-{account}",
                    scope=RequestScope(url=f"*account={account}*"),
                    exhaustion="miss",
                    ignore_query=("account", "nonce"),
                )
                for account in ("a", "b")
            ]
        )

    @pytest.mark.parametrize("level", ["normalized", "fuzzy"])
    def test_equal_keys_under_different_rules_never_authorize_a_response(
        self, rules: SiteRules, level: MatchLevel
    ) -> None:
        recorded_url = f"{API}?account=b&nonce=1&item=7"
        recorded = build_indexed(
            recorded_url,
            record_id="account-b",
            order=0,
            rule=rules.require_for_request("GET", recorded_url),
        )
        matcher = Matcher(ArchiveIndex([recorded]), MatcherConfig(levels=(level,)), rules)
        live = get(f"{API}?account=a&nonce=2&item=7")
        result = matcher.match(live)
        assert result.keys.normalized == recorded.keys.normalized
        assert result.reason == "no_match" and result.record is None
        assert result.rule_id == "account-a"
        # Rejecting another rule's record never consumes that record's sequence.
        assert matcher.match(get(f"{API}?account=b&nonce=2&item=7")).record == recorded

    def test_fuzzy_similarity_still_requires_the_same_selected_rule(self, rules: SiteRules) -> None:
        url = f"{API}?account=b&nonce=1&item=7&variant=red"
        recorded = build_indexed(
            url, record_id="account-b", order=0, rule=rules.require_for_request("GET", url)
        )
        matcher = Matcher(
            ArchiveIndex([recorded]), MatcherConfig(levels=("fuzzy",), fuzzy_threshold=0.5), rules
        )
        result = matcher.match(get(f"{API}?account=a&nonce=2&item=7&variant=blue"))
        assert result.keys.normalized != recorded.keys.normalized
        assert fuzzy_score(result.keys.params, recorded.keys.params) == 0.5
        assert result.reason == "no_match" and result.record is None

    @pytest.mark.parametrize("level", ["exact", "normalized", "fuzzy"])
    def test_ranges_cannot_use_a_resource_selected_by_another_rule(
        self, rules: SiteRules, level: MatchLevel
    ) -> None:
        url = f"{API}?account=b&nonce=1&item=7"
        recorded = build_indexed(
            url, record_id="account-b", order=0, rule=rules.require_for_request("GET", url)
        )
        index = ArchiveIndex(
            [], RangeMap({recorded.keys.normalized: [RangeSegment(0, 9, 10, recorded)]})
        )
        result = Matcher(index, MatcherConfig(levels=(level,)), rules).match(
            get(f"{API}?account=a&nonce=2&item=7", Range="bytes=0-4")
        )
        assert result.reason == "no_match" and result.range is None
        assert result.rule_id == "account-a"

    def test_ranges_never_fill_a_gap_with_another_rules_bytes(self, rules: SiteRules) -> None:
        first_url = f"{API}?account=a&nonce=1"
        second_url = f"{API}?account=b&nonce=1"
        first = build_indexed(
            first_url,
            record_id="account-a",
            order=0,
            rule=rules.require_for_request("GET", first_url),
        )
        second = build_indexed(
            second_url,
            record_id="account-b",
            order=1,
            rule=rules.require_for_request("GET", second_url),
        )
        assert first.keys.normalized == second.keys.normalized
        index = ArchiveIndex(
            [],
            RangeMap(
                {
                    first.keys.normalized: [
                        RangeSegment(0, 4, 10, first),
                        RangeSegment(5, 9, 10, second),
                    ]
                }
            ),
        )
        result = Matcher(index, MatcherConfig(levels=("normalized",)), rules).match(
            get(f"{API}?account=a&nonce=2", Range="bytes=5-9")
        )
        assert result.reason == "range_uncovered" and result.range is None

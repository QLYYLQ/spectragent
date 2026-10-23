"""The browser's offline statistics over a recorded archive (§4.16 "browser 一侧的读法", ``archive_stats.py``).

Every archive is recorded by M29's own ``browser.network`` session and
committed by the real WAL (``tests/_helpers/replay_archive.py``), then read
back through ``open_archive`` — the reader the flywheel metrics hand these
functions. Replay-log decisions are built from real ``ReplayLogEntry``
objects, as ``iter_decisions`` yields them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.archive_stats import (
    BodyCompleteness,
    ReplayNearMisses,
    SiteRuleCandidate,
    body_completeness,
    challenge_documents,
    challenge_signals,
    failure_association,
    near_misses,
    not_feeding_page_counts,
    revisit_saving,
    site_rule_candidates,
)
from spectragent.core.capabilities.browser.replay.archive_index import (
    ArchiveIndex,
    ExchangeLine,
    IndexLine,
    RecordedArchive,
    ReplayArchiveError,
    iter_index_lines,
)
from spectragent.core.capabilities.browser.replay.canonical import (
    MAX_BODY_KEY_CHARS,
    request_keys,
)
from spectragent.core.capabilities.browser.replay.replay_log import (
    MAX_KEY_CHARS,
    ReplayCandidate,
    ReplayLogEntry,
)
from spectragent.core.capabilities.browser.replay.site_rules import (
    ParsedField,
    RequestScope,
    SiteRule,
    SiteRules,
)
from tests._helpers.replay_archive import RecordingScript, indexed

SHOP = "http://shop.test"
CHALLENGE = {"Server": "cloudflare", "CF-RAY": "8a1b2c3d4e5f-AMS", "cf-mitigated": "challenge"}


def _lines(run: Path) -> list[Any]:
    return [line for _step, line in iter_index_lines(open_archive(run))]


# ── body completeness ─────────────────────────────────────────────────


async def test_every_record_ends_in_one_explicit_state(tmp_path: Path) -> None:
    """One record per state; the late body and the never-settled request are read off the last line."""
    recording = RecordingScript(tmp_path)
    await recording.exchange(f"{SHOP}/a")
    await recording.redirect(f"{SHOP}/old", f"{SHOP}/new")
    await recording.too_large(f"{SHOP}/huge")
    recording.failed(f"{SHOP}/broken")
    recording.websocket("ws://shop.test/socket")
    await recording.streamed_later(f"{SHOP}/now.js", b"n" * 5000)
    await recording.commit(0)
    finish = await recording.streamed_later(f"{SHOP}/late.js", b"l" * 6000, hold=True)
    recording.in_flight(f"{SHOP}/never")
    await recording.commit(1)
    assert finish is not None
    finish()
    await recording.commit_trailing()
    lines = _lines(recording.finish())
    assert "body" in [line.kind for line in lines], "the fixture writes a late body line"

    found = body_completeness(lines)

    assert found.by_state == {
        "ok": 2,  # /a and the page the redirect lands on
        "redirect": 1,
        "too_large": 1,
        "incomplete": 2,  # the failed request and the one still pending at the end
        "websocket": 1,
        "streamed_later": 2,  # bodies streamed: in the same commit and a later one
    }
    assert (found.records, found.with_body, found.no_body_needed, found.explicit_no_body) == (
        9,
        4,
        2,
        3,
    )


async def test_a_streamed_body_still_pending_at_the_end_is_incomplete(tmp_path: Path) -> None:
    recording = RecordingScript(tmp_path)
    finish = await recording.streamed_later(f"{SHOP}/late.js", b"l" * 6000, hold=True)
    await recording.commit(0)
    await recording.commit_trailing()  # the body never arrived
    assert finish is not None
    assert body_completeness(_lines(recording.finish())).by_state == {"incomplete": 1}


def test_no_record_is_no_state() -> None:
    assert body_completeness([]) == BodyCompleteness()
    assert BodyCompleteness().records == 0


def test_completeness_adds_up() -> None:
    total = BodyCompleteness({"ok": 2}) + BodyCompleteness({"ok": 1, "redirect": 1})
    assert total.by_state == {"ok": 3, "redirect": 1}


# ── the revisit saving ────────────────────────────────────────────────


async def test_a_revisit_saves_the_full_record_less_its_own(tmp_path: Path) -> None:
    # Incompressible (the WARC is gzip), so the full record dwarfs the revisit's headers.
    payload = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(200))
    recording = RecordingScript(tmp_path)
    await recording.exchange(f"{SHOP}/first", body=payload)
    await recording.exchange(f"{SHOP}/again", body=payload)
    await recording.commit(0)
    lines = _lines(recording.finish())
    exchanges = {line.url: line for line in lines if isinstance(line, ExchangeLine)}
    first, again = exchanges[f"{SHOP}/first"], exchanges[f"{SHOP}/again"]
    assert again.revisit_of == first.response_record_id
    assert first.warc.response is not None and again.warc.response is not None

    saved = revisit_saving(lines)

    assert saved == first.warc.response.length - again.warc.response.length
    assert saved > len(payload) // 2, "the revisit replaced the payload, not a few header bytes"


def test_no_revisit_saves_nothing() -> None:
    assert revisit_saving([]) == 0


def test_a_revisit_of_a_record_the_archive_lacks_is_refused() -> None:
    line = IndexLine.model_validate(
        {
            "kind": "exchange",
            "record_id": "r1",
            "url": f"{SHOP}/x",
            "body_status": "ok",
            "response_record_id": "c",
            "revisit_of": "gone",
            "warc": {"response": {"offset": 0, "length": 300}},
        }
    ).root
    with pytest.raises(ValueError, match="'gone'"):
        revisit_saving([line])


# ── challenge documents ───────────────────────────────────────────────


async def test_a_challenged_document_is_found_with_its_step_and_marks(tmp_path: Path) -> None:
    recording = RecordingScript(tmp_path)
    await recording.exchange(f"{SHOP}/", resource_type="Document", body=b"<p>shop</p>")
    await recording.commit(0)
    await recording.exchange(
        f"{SHOP}/checkout?__cf_chl_tk=abc",
        status=403,
        resource_type="Document",
        headers=CHALLENGE,
        body=b"<p>Just a moment...</p>",
    )
    # Not a challenge: the marks on a 200, the status without the marks, a
    # challenged XHR, cloudflare without a ray.
    await recording.exchange(f"{SHOP}/ok", resource_type="Document", headers=CHALLENGE)
    await recording.exchange(f"{SHOP}/denied", status=403, resource_type="Document")
    await recording.exchange(f"{SHOP}/api", status=403, headers=CHALLENGE)
    await recording.exchange(
        f"{SHOP}/busy", status=503, resource_type="Document", headers={"Server": "cloudflare"}
    )
    await recording.commit(1)

    found = challenge_documents(open_archive(recording.finish()))

    assert [(item.step, item.url, item.status) for item in found] == [
        (1, f"{SHOP}/checkout?__cf_chl_tk=abc", 403)
    ]
    assert found[0].signals == ("cf-mitigated: challenge", "server: cloudflare + cf-ray")


@pytest.mark.parametrize(
    ("status", "headers", "signals"),
    [
        (403, [("cf-mitigated", "Challenge")], ("cf-mitigated: challenge",)),
        (429, [("Server", "Cloudflare"), ("CF-Ray", "1")], ("server: cloudflare + cf-ray",)),
        (503, [("server", "cloudflare")], ()),
        (200, [("cf-mitigated", "challenge")], ()),
        (None, [("cf-mitigated", "challenge")], ()),
        (403, [("cf-mitigated", "block")], ()),
    ],
)
def test_challenge_signals(
    status: int | None, headers: list[tuple[str, str]], signals: tuple
) -> None:
    assert challenge_signals(status, headers) == signals


# ── site-rule candidates ──────────────────────────────────────────────


def _miss(
    url: str,
    *,
    candidates: list[str],
    method: str = "GET",
    record_ids: list[str] | None = None,
    candidate_method: str | None = None,
    **fields: Any,
) -> ReplayLogEntry:
    ids = record_ids if record_ids is not None else [f"r{i}" for i in range(len(candidates))]
    base: dict[str, Any] = {
        "kind": "miss",
        "captured_at": 1.0,
        "cycle": 1,
        "method": method,
        "url": url,
        "resource_type": "XHR",
        "request_class": "subresource",
        "reason": "no_match",
        "answer": "fulfilled",
        "status": 404,
        "candidates": [
            ReplayCandidate(
                record_id=ids[i],
                method=candidate_method or method,
                url=c,
                score=0.5,
                consumed=False,
            )
            for i, c in enumerate(candidates)
        ],
    }
    base.update(fields)
    return ReplayLogEntry(**base)


JSON = "application/json"


def _source(
    *requests: tuple[str, str, bytes | None] | tuple[str, str, bytes | None, dict[str, Any]],
    rules: SiteRules | None = None,
) -> RecordedArchive:
    """An in-memory source archive: one record ``r<i>`` per ``(method, url, body[, fields])``.

    Keyed with *rules*; *fields* are the record's other facts (``status``,
    ``resource_type``, ``payload_length``).
    """
    rules = rules if rules is not None else SiteRules()
    records = [
        indexed(
            request[1],
            record_id=f"r{i}",
            order=i,
            method=request[0],
            body=request[2],
            content_type=JSON if request[2] is not None else None,
            rule=rules.for_request(request[0], request[1]),
            **(request[3] if len(request) > 3 else {}),
        )
        for i, request in enumerate(requests)
    ]
    return RecordedArchive(
        run_id="source",
        fingerprint=None,
        fingerprint_sha256=None,
        t0_ms=None,
        index=ArchiveIndex(records),
        rules=rules,
    )


def _body_miss(
    url: str, body: bytes, record: int, recorded_url: str, rules: SiteRules | None = None
) -> ReplayLogEntry:
    """A POST miss as the server logs it: its body key computed with the rule the rules select."""
    rule = (rules or SiteRules()).for_request("POST", url)
    keys = request_keys("POST", url, body, JSON, rule)
    return _miss(
        url,
        candidates=[recorded_url],
        method="POST",
        record_ids=[f"r{record}"],
        body_key=keys.body_key,
    )


Decisions = Sequence[tuple[int | None, ReplayLogEntry, int]]


def _ranked(
    source: RecordedArchive,
    *runs: Decisions,
    checks: dict[int, bool] | None = None,
    site: str = SHOP,
) -> list[SiteRuleCandidate]:
    """``site_rule_candidates`` over *runs*, each replayed from *source* with the same *checks*."""
    return site_rule_candidates(
        ReplayNearMisses(
            run=f"run-{i}", site=site, misses=near_misses(run, source), checks=checks or {}
        )
        for i, run in enumerate(runs)
    )


def _each_its_own_run(source: RecordedArchive, decisions: Decisions) -> list[SiteRuleCandidate]:
    """Every decision in a run of its own: each counts once, as one request did before."""
    return _ranked(source, *([decision] for decision in decisions))


def test_near_misses_count_by_endpoint_and_differing_parameter() -> None:
    source = _source(
        ("GET", f"{SHOP}/data.json?session=1&page=1", None),
        ("GET", f"{SHOP}/data.json?a=2", None),
        ("GET", "http://cdn.test/data.json?a=2", None),
        ("GET", "http://news.test/p?visit=8", None),
        ("GET", f"{SHOP}/other.json?session=1", None),
    )
    decisions = [
        (
            1,
            _miss(
                f"{SHOP}/data.json?session=2&page=1",
                candidates=[f"{SHOP}/data.json?session=1&page=1"],
            ),
            1,
        ),
        (
            2,
            _miss(f"{SHOP}/data.json?session=3", candidates=[f"{SHOP}/data.json?session=1&page=1"]),
            3,
        ),
        (
            2,
            _miss(
                f"{SHOP}/other.json?session=2",
                candidates=[f"{SHOP}/other.json?session=1"],
                record_ids=["r4"],
            ),
            1,
        ),
        # The nearest candidate is another path, another host, another method: no endpoint rule.
        (
            2,
            _miss(
                f"{SHOP}/elsewhere.json?a=1",
                candidates=[f"{SHOP}/data.json?a=2"],
                record_ids=["r1"],
            ),
            1,
        ),
        (
            2,
            _miss(
                f"{SHOP}/data.json?a=1",
                candidates=["http://cdn.test/data.json?a=2"],
                record_ids=["r2"],
            ),
            1,
        ),
        (
            2,
            _miss(
                f"{SHOP}/data.json?a=1",
                candidates=[f"{SHOP}/data.json?a=2"],
                method="POST",
                candidate_method="GET",
                record_ids=["r1"],
            ),
            1,
        ),
        # No candidate; a different reason; folded (no candidates are kept).
        (3, _miss(f"{SHOP}/data.json?a=1", candidates=[]), 1),
        (
            3,
            _miss(
                f"{SHOP}/data.json?a=1",
                candidates=[f"{SHOP}/data.json?a=2"],
                record_ids=["r1"],
                reason="too_large",
            ),
            1,
        ),
        (
            None,
            _miss(
                "http://news.test/p?visit=9",
                candidates=["http://news.test/p?visit=8"],
                record_ids=["r3"],
            ),
            1,
        ),
    ]

    found = _ranked(source, decisions)

    assert [(c.host, c.method, c.path, c.runs, c.requests, dict(c.query)) for c in found] == [
        ("news.test", "GET", "/p", 1, 1, {"visit": 1}),
        ("shop.test", "GET", "/data.json", 1, 4, {"page": 1, "session": 1}),
        ("shop.test", "GET", "/other.json", 1, 1, {"session": 1}),
    ], "an endpoint is host, method and path, not the host alone; equal ranks by host and path"
    assert [(f.query, f.body, f.runs) for f in found[1].field_sets] == [
        (("page", "session"), (), 1),
        (("session",), (), 1),
    ], "the fields that differed together, each combination once per run"


def test_counts_are_in_replay_runs_not_requests() -> None:
    """A request a page repeats every second counts once per run, like one it sends once."""
    source = _source(
        ("GET", f"{SHOP}/poll?t=1", None),
        ("GET", f"{SHOP}/search?q=a&s=1", None),
    )
    poll = _miss(f"{SHOP}/poll?t=2", candidates=[f"{SHOP}/poll?t=1"], record_ids=["r0"])
    search = _miss(
        f"{SHOP}/search?q=a&s=2", candidates=[f"{SHOP}/search?q=a&s=1"], record_ids=["r1"]
    )

    found = _ranked(source, [(1, poll, 1000)], [(1, search, 1)], [(2, search, 1)])

    assert [(c.path, c.runs, c.requests) for c in found] == [
        ("/search", 2, 2),
        ("/poll", 1, 1000),
    ], "two runs beat a thousand requests in one"


def test_cache_shaped_parameters_remain_candidates_without_an_explicit_rule() -> None:
    """A counter-shaped parameter is not globally assumed irrelevant to content."""
    source = _source(("GET", f"{SHOP}/poll?_=2&id=7", None))
    decisions = [(1, _miss(f"{SHOP}/poll?_=1&id=7", candidates=[f"{SHOP}/poll?_=2&id=7"]), 1)]
    assert _ranked(source, decisions)[0].query == {"_": 1}


def test_body_fields_that_differ_are_counted_by_their_flattened_path() -> None:
    url = f"{SHOP}/api/search"
    recorded = b'{"query": "kettle", "page": 1, "client": {"trace": "aaa", "ts": 100}}'
    source = _source(("POST", url, recorded))
    decisions = [
        (
            1,
            _body_miss(
                url,
                b'{"query": "kettle", "page": 1, "client": {"trace": "bbb", "ts": 101}}',
                0,
                url,
            ),
            1,
        ),
        (
            2,
            _body_miss(
                url,
                b'{"query": "kettle", "page": 1, "client": {"trace": "ccc", "ts": 100}}',
                0,
                url,
            ),
            1,
        ),
        # The same body: not a near miss of this kind (something else kept it from matching).
        (3, _body_miss(url, recorded, 0, url), 1),
    ]

    [candidate] = _each_its_own_run(source, decisions)

    assert (candidate.method, candidate.path, candidate.runs) == ("POST", "/api/search", 2)
    assert candidate.query == {} and candidate.body == {"client.trace": 2, "client.ts": 1}
    assert [(f.body, f.runs) for f in candidate.field_sets] == [
        (("client.trace",), 1),
        (("client.trace", "client.ts"), 1),
    ]
    assert candidate.opaque_body == 0


def test_a_field_the_replays_own_rule_ignores_is_not_reported() -> None:
    url = f"{SHOP}/api/search"
    rules = SiteRules(
        [
            SiteRule(
                id="search",
                scope=RequestScope(host="shop.test", path="/api/search", method="POST"),
                exhaustion="repeat_last",
                unavailable="miss",
                ignore_query=("t",),
                ignore_body_paths=("client.trace",),
            )
        ]
    )
    source = _source(
        ("POST", f"{url}?t=1&q=kettle", b'{"client": {"trace": "a"}, "page": 1}'), rules=rules
    )
    live = f"{url}?t=2&q=kettle"
    decisions = [
        (
            1,
            _body_miss(
                live, b'{"client": {"trace": "b"}, "page": 2}', 0, f"{url}?t=1&q=kettle", rules
            ),
            1,
        )
    ]

    [candidate] = _ranked(source, decisions)

    assert candidate.query == {} and candidate.body == {"page": 1}, (
        "t and client.trace are already ignored by the rule both sides were keyed with"
    )


def test_the_sub_fields_of_a_parsed_field_are_reported_by_their_names() -> None:
    """With the field parsed by the replay's rule, the report names the sub-fields that differ."""
    url = f"{SHOP}/api/play"
    rules = SiteRules(
        [
            SiteRule(
                id="play",
                scope=RequestScope(host="shop.test", path="/api/play", method="POST"),
                exhaustion="repeat_last",
                unavailable="miss",
                parse_query=(ParsedField(field="ext", format="urlencoded"),),
                parse_body_paths=(ParsedField(field="info", format="urlencoded"),),
                ignore_body_paths=("info.key",),
            )
        ]
    )
    recorded = f"{url}?ext=lt%3D1%26trace%3Da"
    source = _source(("POST", recorded, b'{"info": "vid=v1&key=k1&tm=100"}'), rules=rules)
    live = f"{url}?ext=lt%3D1%26trace%3Db"
    decisions = [(1, _body_miss(live, b'{"info": "vid=v1&key=k2&tm=101"}', 0, recorded, rules), 1)]

    [candidate] = site_rule_candidates(near_misses(decisions, source))

    assert candidate.query == {"ext.trace": 1} and candidate.body == {"info.tm": 1}


def test_bodies_known_only_by_digest_count_as_an_opaque_difference() -> None:
    url = f"{SHOP}/api/report"
    long_value = "x" * (MAX_BODY_KEY_CHARS + 10)
    source = _source(("POST", url, json.dumps({"blob": long_value, "n": 1}).encode()))
    decisions = [
        (1, _body_miss(url, json.dumps({"blob": long_value, "n": 2}).encode(), 0, url), 2),
        (2, _body_miss(url, json.dumps({"blob": long_value, "n": 1}).encode(), 0, url), 1),
    ]

    [candidate] = _each_its_own_run(source, decisions)

    assert (candidate.runs, candidate.requests, candidate.opaque_body, candidate.body) == (
        1,
        2,
        1,
        {},
    ), "the second body is the recorded one: equal digests are no difference"
    assert [(f.body, f.opaque_body, f.runs) for f in candidate.field_sets] == [((), True, 1)]


def test_a_body_key_longer_than_a_match_key_still_names_its_fields() -> None:
    """The log keeps the body key whole up to the canonicaliser's cap: only the matcher hashes."""
    url = f"{SHOP}/api/report"
    fields = {f"k{i}": "v" * 20 for i in range(30)}  # past the log's 512 characters, under 4096
    source = _source(("POST", url, json.dumps(fields).encode()))
    changed = json.dumps({**fields, "k0": "w" * 20, "k7": "w" * 20}).encode()
    entry = _body_miss(url, changed, 0, url)
    assert entry.body_key is not None and MAX_KEY_CHARS < len(entry.body_key) < MAX_BODY_KEY_CHARS
    same = _body_miss(url, json.dumps(fields).encode(), 0, url)

    [candidate] = _ranked(source, [(1, entry, 1), (2, same, 1)])

    assert (candidate.requests, candidate.opaque_body) == (1, 0)
    assert candidate.body == {"k0": 1, "k7": 1}


def test_a_body_key_that_is_not_a_canonical_parameter_string_names_no_field() -> None:
    url = f"{SHOP}/api/report"
    source = _source(("POST", url, json.dumps({"n": 1}).encode()))
    entry = _miss(
        url, candidates=[url], method="POST", record_ids=["r0"], body_key="sha256:" + "0" * 64
    )

    [candidate] = _ranked(source, [(1, entry, 1)])

    assert (candidate.body, candidate.opaque_body) == ({}, 1)


def test_a_candidate_the_source_does_not_hold_is_refused() -> None:
    source = _source(("GET", f"{SHOP}/data.json?a=1", None))
    decisions = [
        (
            1,
            _miss(
                f"{SHOP}/data.json?a=2", candidates=[f"{SHOP}/data.json?a=1"], record_ids=["gone"]
            ),
            1,
        )
    ]
    with pytest.raises(ReplayArchiveError, match="does not hold"):
        near_misses(decisions, source)


def test_requests_that_do_not_feed_the_page_are_counted_apart() -> None:
    """Decided from the recording and the replay's rules, never from a list of domains."""
    rules = SiteRules(
        [
            SiteRule(
                id="collector",
                scope=RequestScope(host="shop.test", path="/collect"),
                exhaustion="repeat_last",
                unavailable="miss",
                synthesize_204=(f"{SHOP}/collect*",),
            )
        ]
    )
    source = _source(
        ("GET", f"{SHOP}/collect?n=1", None),
        ("GET", f"{SHOP}/pixel?n=1", None, {"resource_type": "Image"}),
        ("POST", f"{SHOP}/beacon?n=1", None, {"resource_type": "Ping"}),
        ("GET", f"{SHOP}/log?n=1", None, {"status": 204}),
        ("GET", f"{SHOP}/ack?n=1", None, {"payload_length": 0}),
        ("GET", f"{SHOP}/items?n=1", None, {"payload_length": 512}),
        rules=rules,
    )
    paths = ["/collect", "/pixel", "/beacon", "/log", "/ack", "/items"]
    decisions: Decisions = [
        (
            1,
            _miss(
                f"{SHOP}{path}?n=2",
                candidates=[f"{SHOP}{path}?n=1"],
                record_ids=[f"r{i}"],
                method="POST" if path == "/beacon" else "GET",
            ),
            2,
        )
        for i, path in enumerate(paths)
    ]
    runs = [ReplayNearMisses(run="run-0", site=SHOP, misses=near_misses(decisions, source))]

    assert [c.path for c in site_rule_candidates(runs)] == ["/items"]
    assert {
        reason: (count.endpoints, count.requests)
        for reason, count in not_feeding_page_counts(runs).items()
    } == {
        "rule_answers_204": (1, 2),
        "ping_or_image": (2, 4),
        "recorded_empty": (2, 4),
    }, "a rule's synthesize_204, Chrome's Ping or Image, a recorded 204 or empty answer"


def test_endpoints_rank_by_how_their_near_misses_go_with_failed_checks() -> None:
    """A request made at every step misses where restoration passes too; one that breaks it does not."""
    source = _source(
        ("GET", f"{SHOP}/poll?t=1", None),
        ("GET", f"{SHOP}/search?q=a&sig=1", None),
        ("GET", f"{SHOP}/late?t=1", None),
    )
    poll = _miss(f"{SHOP}/poll?t=2", candidates=[f"{SHOP}/poll?t=1"], record_ids=["r0"])
    search = _miss(
        f"{SHOP}/search?q=a&sig=2", candidates=[f"{SHOP}/search?q=a&sig=1"], record_ids=["r1"]
    )
    late = _miss(f"{SHOP}/late?t=2", candidates=[f"{SHOP}/late?t=1"], record_ids=["r2"])
    run: Decisions = [(0, poll, 1), (1, poll, 5), (2, poll, 1), (2, search, 1), (None, late, 1)]

    found = _ranked(source, run, run, checks={0: True, 1: True, 2: False})

    assert [
        (c.path, c.failed_check_steps, c.passed_check_steps, c.failed_restoration_runs)
        for c in found
    ] == [
        ("/search", 2, 0, 2),
        ("/poll", 2, 4, 2),
        ("/late", 0, 0, 0),
    ], "a close-out collection's near miss is at no checked step"
    assert found[0].failure_association > found[1].failure_association > 0
    assert found[2].failure_association == 0.0


def test_a_failed_restoration_run_counts_only_near_misses_at_or_before_its_first_failed_check() -> (
    None
):
    source = _source(("GET", f"{SHOP}/a?t=1", None), ("GET", f"{SHOP}/b?t=1", None))
    early = _miss(f"{SHOP}/a?t=2", candidates=[f"{SHOP}/a?t=1"], record_ids=["r0"])
    after = _miss(f"{SHOP}/b?t=2", candidates=[f"{SHOP}/b?t=1"], record_ids=["r1"])

    found = _ranked(source, [(1, early, 1), (3, after, 1)], checks={1: True, 2: False, 3: False})

    assert {c.path: (c.failed_restoration_runs, c.failed_check_steps) for c in found} == {
        "/a": (1, 0),
        "/b": (0, 1),
    }, "/a missed before the first failed check (step 2), /b only after it"


def test_endpoints_are_counted_and_ranked_per_site() -> None:
    source = _source(("GET", f"{SHOP}/a?t=1", None))
    entry = _miss(f"{SHOP}/a?t=2", candidates=[f"{SHOP}/a?t=1"], record_ids=["r0"])
    found = site_rule_candidates(
        ReplayNearMisses(run=f"run-{site}", site=site, misses=near_misses([(1, entry, 1)], source))
        for site in ("z.test", "a.test", "z.test")
    )
    assert [(c.site, c.path, c.runs) for c in found] == [("a.test", "/a", 1), ("z.test", "/a", 2)]


@pytest.mark.parametrize(
    ("failed", "passed", "expected"),
    [(0, 0, 0.0), (0, 5, 0.0), (1, 0, 0.206543), (32, 0, 0.892817), (57, 1, 0.908589)],
)
def test_failure_association_is_wilsons_lower_bound(
    failed: int, passed: int, expected: float
) -> None:
    assert failure_association(failed, passed) == expected

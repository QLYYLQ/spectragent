"""The browser's offline statistics over a recorded archive (§4.16 "browser 一侧的读法", ``archive_stats.py``).

Every archive is recorded by M29's own ``browser.network`` session and
committed by the real WAL (``tests/_helpers/replay_archive.py``), then read
back through ``open_archive`` — the reader the flywheel metrics hand these
functions. Replay-log decisions are built from real ``ReplayLogEntry``
objects, as ``iter_decisions`` yields them.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.archive_stats import (
    BodyCompleteness,
    body_completeness,
    challenge_documents,
    challenge_signals,
    revisit_saving,
    site_rule_candidates,
)
from spectragent.core.capabilities.browser.replay.archive_index import (
    ExchangeLine,
    IndexLine,
    iter_index_lines,
)
from spectragent.core.capabilities.browser.replay.replay_log import (
    ReplayCandidate,
    ReplayLogEntry,
)
from tests._helpers.replay_archive import RecordingScript

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


def _miss(url: str, *, candidates: list[str], method: str = "GET", **fields: Any) -> ReplayLogEntry:
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
            ReplayCandidate(record_id=f"r{i}", method="GET", url=c, score=0.5, consumed=False)
            for i, c in enumerate(candidates)
        ],
    }
    base.update(fields)
    return ReplayLogEntry(**base)


def test_near_misses_count_by_host_and_differing_parameter() -> None:
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
        # The nearest candidate is another path, another host, another method: not a query rule.
        (2, _miss(f"{SHOP}/other.json?a=1", candidates=[f"{SHOP}/data.json?a=2"]), 1),
        (2, _miss(f"{SHOP}/data.json?a=1", candidates=["http://cdn.test/data.json?a=2"]), 1),
        (2, _miss(f"{SHOP}/data.json?a=1", candidates=[f"{SHOP}/data.json?a=2"], method="POST"), 1),
        # No candidate; a different reason; a hit; folded (no candidates are kept).
        (3, _miss(f"{SHOP}/data.json?a=1", candidates=[]), 1),
        (
            3,
            _miss(
                f"{SHOP}/data.json?a=1", candidates=[f"{SHOP}/data.json?a=2"], reason="too_large"
            ),
            1,
        ),
        (None, _miss("http://news.test/p?visit=9", candidates=["http://news.test/p?visit=8"]), 1),
    ]

    found = site_rule_candidates(decisions)

    assert {host: (c.misses, dict(c.params)) for host, c in found.items()} == {
        "news.test": (1, {"visit": 1}),
        "shop.test": (4, {"page": 3, "session": 4}),
    }


def test_volatile_parameters_alone_never_make_a_candidate() -> None:
    """``_=<number>`` is dropped by the canonical query: equal queries mean another cause."""
    decisions = [(1, _miss(f"{SHOP}/poll?_=1&id=7", candidates=[f"{SHOP}/poll?_=2&id=7"]), 1)]
    assert site_rule_candidates(decisions) == {}

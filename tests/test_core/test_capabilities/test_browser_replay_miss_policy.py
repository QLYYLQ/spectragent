"""The ``strict`` miss policy's classes (§4.14 "miss（`strict`）").

Documents first (main → fail + diverged, sub → fail), then the benign (204),
then everything else (404); never a continue. The end-to-end counterpart is
``test_browser_replay_live.py::test_strict_classes_end_to_end``.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from spectragent.core.capabilities.browser.replay.miss_policy import (
    REPLAY_MISS_HEADER,
    MissDecision,
    ReplayMiss,
)
from spectragent.core.capabilities.browser.replay.strict import StrictMissPolicy, host_matches


def _miss(**fields: Any) -> ReplayMiss:
    base: dict[str, Any] = {
        "method": "GET",
        "url": "http://shop.test/api/data.json",
        "resource_type": "XHR",
        "is_document": False,
        "frame": None,
        "content_type": None,
        "reason": "no_match",
    }
    base.update(fields)
    return ReplayMiss(**base)


def _document(frame: str, **fields: Any) -> ReplayMiss:
    return _miss(resource_type="Document", is_document=True, frame=frame, **fields)


POLICY = StrictMissPolicy()


def test_a_main_frame_document_miss_fails_and_diverges() -> None:
    decision = POLICY.decide(_document("main"))
    assert decision == MissDecision(
        request_class="document", answer="fail", error_reason="Failed", diverged=True
    )


def test_a_sub_frame_document_miss_fails_without_diverging() -> None:
    decision = POLICY.decide(_document("sub"))
    assert decision.answer == "fail" and decision.error_reason == "Failed"
    assert decision.request_class == "document" and not decision.diverged


def test_documents_come_before_benign() -> None:
    """An analytics iframe that missed fails like any document (no empty 204 page)."""
    decision = POLICY.decide(_document("sub", url="https://www.googletagmanager.com/ns.html"))
    assert decision.request_class == "document" and decision.answer == "fail"


@pytest.mark.parametrize(
    "miss",
    [
        _miss(resource_type="Ping", method="POST"),
        _miss(resource_type="CSPViolationReport", method="POST"),
        _miss(method="POST", content_type="application/csp-report"),
        _miss(method="POST", content_type="application/reports+json; charset=utf-8"),
        _miss(url="https://www.google-analytics.com/g/collect?v=2"),
        _miss(url="https://o123.ingest.sentry.io/api/1/envelope/"),
        _miss(site_benign=True),
    ],
    ids=[
        "ping",
        "csp-type",
        "csp-report",
        "reporting-api",
        "analytics-host",
        "sentry",
        "site-rule",
    ],
)
def test_benign_misses_are_answered_204(miss: ReplayMiss) -> None:
    decision = POLICY.decide(miss)
    assert decision == MissDecision(
        request_class="benign", answer="fulfill", status=204, headers=(REPLAY_MISS_HEADER,)
    )


def test_every_other_subresource_is_a_404_marked_as_a_miss() -> None:
    decision = POLICY.decide(_miss())
    assert decision == MissDecision(
        request_class="subresource", answer="fulfill", status=404, headers=(("x-replay-miss", "1"),)
    )


def test_hosts_match_by_suffix_on_a_label_boundary() -> None:
    assert host_matches("www.google-analytics.com", ["google-analytics.com"])
    assert host_matches("google-analytics.com.", ["google-analytics.com"])
    assert not host_matches("notgoogle-analytics.com", ["google-analytics.com"])
    assert POLICY.decide(_miss(url="https://notgoogle-analytics.com/x")).status == 404


def test_extra_benign_hosts_add_to_the_built_in_list() -> None:
    policy = StrictMissPolicy(extra_benign_hosts=["stats.example.com"])
    assert policy.decide(_miss(url="https://a.stats.example.com/hit")).status == 204
    assert policy.decide(_miss(url="https://example.com/hit")).status == 404
    assert POLICY.decide(_miss(url="https://a.stats.example.com/hit")).status == 404


@pytest.mark.parametrize("reason", ["too_large", "body_unavailable", "range_uncovered"])
def test_a_recorded_but_unservable_match_is_answered_by_the_same_table(reason: str) -> None:
    assert POLICY.decide(_miss(reason=reason)).status == 404
    assert POLICY.decide(_document("main", reason=reason)).diverged


def test_no_decision_is_ever_a_continue() -> None:
    """Every combination of the facts a policy reads gets fulfil or fail — nothing else exists."""
    for document, frame, resource_type, site_benign in itertools.product(
        (True, False), ("main", "sub", None), ("Document", "XHR", "Ping", None), (True, False)
    ):
        miss = _miss(
            is_document=document,
            frame=frame,
            resource_type=resource_type,
            site_benign=site_benign,
        )
        decision = POLICY.decide(miss)
        assert decision.answer in ("fulfill", "fail")
        assert decision == POLICY.decide(miss), "decide is pure"

"""The browser's restoration evidence: its measures, the limits it records, its checks.

``docs/architecture_design.md`` §4.9 (restoration evidence per capability) and
§4.14 "恢复：重定位与校验": ``BrowserRestorationEvidence`` judges itself
(``failed_checks``) against the limits the ``BrowserCapability`` knobs set,
which it records; a measure not taken decides nothing. The media check's own
cases are in ``test_browser_media.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.restoration_evidence import (
    BrowserRestorationEvidence,
    MaskedArea,
    MediaAreaEvidence,
    MediaComparison,
    MediaEvidence,
)
from tests._helpers.browser_evidence import browser_evidence


def test_the_capability_names_this_type() -> None:
    assert BrowserCapability.RestorationEvidence is BrowserRestorationEvidence


def test_the_checks_in_reporting_order() -> None:
    assert BrowserRestorationEvidence.REASONS == (
        "document_miss",
        "url_mismatch",
        "signature_mismatch",
        "media_mismatch",
        "screenshot_distance",
    )


@pytest.mark.parametrize(
    ("reason", "measures"),
    [
        ("document_miss", {"document_misses": 2}),
        (
            "url_mismatch",
            {
                "url_match": False,
                "recorded_url": "https://a.test/",
                "restored_url": "https://b.test/",
            },
        ),
        ("signature_mismatch", {"signature_match": False, "signature_missing": 1}),
        ("screenshot_distance", {"screenshot_distance": 9}),
    ],
)
def test_each_check_from_its_own_measure(reason: str, measures: dict[str, Any]) -> None:
    assert browser_evidence(**measures).failed_checks() == [reason]


def test_every_check_at_once_is_reported_in_order() -> None:
    media = MediaEvidence(
        comparisons=[MediaComparison(element="main#0 video", correspondence="missing")]
    )
    evidence = browser_evidence(
        screenshot_distance=30,
        media=media,
        signature_match=False,
        url_match=False,
        document_misses=1,
    )
    assert evidence.failed_checks() == list(BrowserRestorationEvidence.REASONS)


def test_a_measure_not_taken_decides_nothing() -> None:
    evidence = browser_evidence()
    assert evidence.failed_checks() == []
    assert (evidence.url_match, evidence.signature_match) == (None, None)
    assert (evidence.screenshot_distance, evidence.document_misses, evidence.media) == (
        None,
        None,
        None,
    )
    assert (evidence.signature_missing, evidence.signature_extra) == (0, 0)
    assert (evidence.url_ignored_query, evidence.url_rule_ids) == ([], [])


def test_the_screenshot_limit_is_inclusive_and_the_evidences_own() -> None:
    assert browser_evidence(screenshot_distance=5).failed_checks() == []
    assert browser_evidence(screenshot_distance=6).failed_checks() == ["screenshot_distance"]
    assert browser_evidence(screenshot_distance=9, screenshot_max_distance=12).failed_checks() == []


def test_the_media_tolerance_is_the_evidences_own() -> None:
    media = MediaEvidence(
        comparisons=[
            MediaComparison(
                element="main#0 video",
                correspondence="matched",
                reading="before",
                recorded_time=41.0,
                restored_time=10.0,
                recorded_paused=True,
                restored_paused=True,
            )
        ]
    )
    assert browser_evidence(media=media).failed_checks() == ["media_mismatch"]
    assert browser_evidence(media=media, media_time_tolerance_s=60.0).failed_checks() == []


@pytest.mark.parametrize("missing", ["screenshot_max_distance", "media_time_tolerance_s"])
def test_the_limits_are_always_recorded(missing: str) -> None:
    fields: dict[str, Any] = {
        "capability_id": "browser",
        "screenshot_max_distance": 5,
        "media_time_tolerance_s": 1.0,
    }
    del fields[missing]
    with pytest.raises(ValidationError, match=missing):
        BrowserRestorationEvidence.model_validate(fields)


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="surprise"):
        BrowserRestorationEvidence.model_validate(
            {
                "capability_id": "browser",
                "screenshot_max_distance": 5,
                "media_time_tolerance_s": 1.0,
                "surprise": 1,
            }
        )


def test_the_url_parameters_left_out_round_trip() -> None:
    evidence = browser_evidence(
        url_match=True,
        recorded_url="https://a.test/all?vt=1",
        restored_url="https://a.test/all?vt=2",
        url_ignored_query=["vt"],
        url_rule_ids=["a-vt"],
    )
    assert BrowserRestorationEvidence.model_validate_json(evidence.model_dump_json()) == evidence


def test_the_media_areas_set_aside_round_trip() -> None:
    evidence = browser_evidence(
        screenshot_distance=0,
        media_areas=MediaAreaEvidence(
            rule_ids=["play-media"],
            areas=[
                MaskedArea(
                    side="recorded",
                    kind="unreachable_frame",
                    element="main/unplaced-0",
                    x=60,
                    y=84,
                    width=778,
                    height=438,
                )
            ],
            masked_share=0.3698,
            unmasked_distance=23,
            masked_distance=0,
            mask_applied=True,
        ),
    )
    assert BrowserRestorationEvidence.model_validate_json(evidence.model_dump_json()) == evidence


@pytest.mark.parametrize(
    "bad",
    [
        {"rule_ids": []},
        {"rule_ids": ["a"], "masked_share": 1.5},
        {
            "rule_ids": ["a"],
            "areas": [
                {
                    "side": "recorded",
                    "kind": "canvas",
                    "element": "x",
                    "x": 0,
                    "y": 0,
                    "width": 1,
                    "height": 1,
                }
            ],
        },
        {
            "rule_ids": ["a"],
            "areas": [
                {
                    "side": "recorded",
                    "kind": "media_element",
                    "element": "x",
                    "x": 0,
                    "y": 0,
                    "width": 0,
                    "height": 1,
                }
            ],
        },
    ],
)
def test_media_area_evidence_names_a_rule_and_only_real_boxes(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MediaAreaEvidence.model_validate(bad)

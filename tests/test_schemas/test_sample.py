"""``CanonicalStepSample``: the format-neutral half of a training sample (arch §4.15)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from spectragent.schemas.action import Action
from spectragent.schemas.sample import (
    BranchSampleMeta,
    CanonicalStepSample,
    FragmentRef,
    ImageRef,
    SampleLabels,
    SampleRedaction,
    ThoughtRecord,
    TranslationRecord,
    Untranslatable,
    UntranslatableReason,
    VerdictLabel,
)


def _sample(*, params: dict[str, object] | None = None) -> CanonicalStepSample:
    return CanonicalStepSample(
        sample_id="run-1/s2",
        run_id="run-1",
        source_trace="recorded/run-1",
        step=2,
        benchmark="online_mind2web",
        subset="test",
        task_id="t-7",
        agent_id="browser_use",
        loop_id="react",
        llm_model="gpt-4o",
        config_hash="abc",
        task="Put a kettle in the cart",
        observations=[
            FragmentRef(
                key="browser.screenshot",
                file="observations/002_browser.screenshot.json",
                artifact="observations/002_browser.screenshot.png",
                artifact_sha256="f" * 64,
            ),
            FragmentRef(key="browser.dom", file="observations/002_browser.dom.json", failed=True),
        ],
        image=ImageRef(
            fragment="browser.screenshot",
            sha256="f" * 64,
            media_type="image/png",
            image_width=2560,
            image_height=1440,
            viewport_width=1280,
            viewport_height=720,
        ),
        history_steps=[0, 1],
        teacher_actions=[
            Action(name="browser.click", params=params or {"index": 7}),
        ],
        thought=ThoughtRecord(text="open the form", source_field="next_goal"),
        actions=[
            Action(name="browser.click_coordinate", params={"coordinate_x": 30, "coordinate_y": 40})
        ],
        translation=[
            TranslationRecord(
                translator_id="index_to_coordinate",
                action_index=0,
                payload={"point_source": "center", "occlusion": "clear"},
            )
        ],
        labels=SampleLabels(
            step_status="ok",
            verdicts=[
                VerdictLabel(
                    verdict_slug="judge", evaluator_id="online_mind2web", predicted_label=1
                )
            ],
        ),
        redactions=[
            SampleRedaction(
                policy="af-privacy-1", location="/start_url", field="token", rule="url_query"
            )
        ],
        branch=BranchSampleMeta(
            branch_group_id="src/s1",
            source_run_id="src",
            checkpoint_step=1,
            branch_index=0,
            terminal_step=3,
            terminal_image_sha256="e" * 64,
        ),
    )


def test_json_round_trip() -> None:
    sample = _sample()
    assert CanonicalStepSample.model_validate_json(sample.model_dump_json()) == sample
    assert CanonicalStepSample.model_validate(json.loads(sample.canonical_json())) == sample


def test_canonical_json_is_key_sorted_whatever_the_insertion_order() -> None:
    one = _sample(params={"index": 7, "text": "a"})
    other = _sample(params={"text": "a", "index": 7})
    assert one.model_dump_json() != other.model_dump_json()  # insertion order shows
    assert one.canonical_json() == other.canonical_json()
    text = one.canonical_json()
    assert text == json.dumps(
        json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def test_frozen_and_closed() -> None:
    sample = _sample()
    with pytest.raises(ValidationError):
        sample.step = 3  # type: ignore[misc]
    data = sample.model_dump(mode="json")
    data["url"] = "http://example.test"
    with pytest.raises(ValidationError):
        CanonicalStepSample.model_validate(data)


def test_untranslatable_reasons() -> None:
    from typing import get_args

    assert set(get_args(UntranslatableReason)) == {
        "no_counterpart",
        "target_offscreen",
        "exceeds_action_budget",
        "occluded",
        "occlusion_unknown",
        "unrepresentable",
    }
    assert Untranslatable(reason="occluded", detail="covered").reason == "occluded"
    with pytest.raises(ValidationError):
        Untranslatable(reason="covered")  # type: ignore[arg-type]

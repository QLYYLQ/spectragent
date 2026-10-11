"""``identity`` and the translator contract every translator shares (arch §4.15)."""

from __future__ import annotations

import pytest

from spectragent.export import IdentityTranslator, InvalidTranslationError, Translation
from spectragent.schemas.action import Action
from spectragent.schemas.sample import TranslationRecord, Untranslatable
from tests.test_export.test_translate.conftest import Recorder


def test_the_teacher_actions_validated_and_unchanged(recorded: Recorder) -> None:
    actions = [
        Action(name="browser.click_coordinate", params={"coordinate_x": 3, "coordinate_y": 4}),
        Action(
            name="browser.type_coordinate",
            params={"coordinate_x": 3, "coordinate_y": 4, "text": "a"},
        ),
        Action(name="browser.done", params={"extracted_content": None}),
    ]
    sample, trace = recorded(actions)
    translation = IdentityTranslator().translate(sample, trace)
    assert translation.actions == actions  # unset params stay unset
    assert translation.untranslatable is None and translation.records == []


def test_an_unregistered_action_has_no_counterpart(recorded: Recorder) -> None:
    sample, trace = recorded([Action(name="desktop.launch", params={"app": "x"})])
    translation = IdentityTranslator().translate(sample, trace)
    assert translation.untranslatable == Untranslatable(
        reason="no_counterpart", detail="desktop.launch is not a registered action"
    )


def test_an_action_its_params_refuse_is_an_error_naming_it(recorded: Recorder) -> None:
    sample, trace = recorded(
        [Action(name="browser.click_coordinate", params={"coordinate_x": "far left"})]
    )
    with pytest.raises(InvalidTranslationError) as excinfo:
        IdentityTranslator().translate(sample, trace)
    assert excinfo.value.sample_id == sample.sample_id
    assert excinfo.value.action_name == "browser.click_coordinate"


def test_validation_coerces_what_the_params_declare(recorded: Recorder) -> None:
    sample, trace = recorded(
        [Action(name="browser.click_coordinate", params={"coordinate_x": 3.0, "coordinate_y": 4})]
    )
    translation = IdentityTranslator().translate(sample, trace)
    assert translation.actions is not None
    assert translation.actions[0].params == {"coordinate_x": 3, "coordinate_y": 4}
    assert type(translation.actions[0].params["coordinate_x"]) is int


class TestTranslation:
    def test_actions_or_a_reason_never_both_or_neither(self) -> None:
        with pytest.raises(ValueError):
            Translation(actions=None)
        with pytest.raises(ValueError):
            Translation(actions=[], untranslatable=Untranslatable(reason="occluded"))

    def test_applied_records_it_on_the_sample(self, recorded: Recorder) -> None:
        sample, _trace = recorded([Action(name="browser.wait", params={})])
        record = TranslationRecord(translator_id="x", action_index=0, payload={"k": 1})
        refused = Translation.refused("occluded", "covered", [record]).applied(sample)
        assert refused.actions is None
        assert refused.untranslatable == Untranslatable(reason="occluded", detail="covered")
        assert refused.translation == [record]
        assert sample.untranslatable is None  # the original is untouched

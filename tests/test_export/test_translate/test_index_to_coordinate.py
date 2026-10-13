"""``index_to_coordinate`` over WAL-built traces of the ``sticky`` page (arch §4.15).

The translator reads the step back exactly as committed — the ``browser.dom``
fragment (decoded, relinked), the screenshot, the document stamp and the
``browser.dom_raw`` files — and hands them to ``index_points``. These tests
pin what it makes of them: the counterpart actions and their params, what
stays unchanged, every untranslatable reason, and the occlusion evidence in
its ruled order.
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser.dom.models import HitTest
from spectragent.export import IndexToCoordinateTranslator, OcclusionEvidenceMissingError
from spectragent.schemas.action import Action
from tests._helpers.occlusion_pages import INDEX, raw_document, sticky_state
from tests.test_export.test_translate.conftest import Recorder


def _click(name: str) -> Action:
    return Action(name="browser.click", params={"index": INDEX[name]})


class TestGeometry:
    @pytest.mark.parametrize("dpr", [1, 2])
    def test_a_click_becomes_a_coordinate_click_at_the_centre(
        self, recorded: Recorder, dpr: int
    ) -> None:
        sample, trace = recorded([_click("clear")], dpr=dpr)
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        # page box (580, 700, 240, 40) minus the scroll (0, 500): CSS px, whatever the DPR
        assert translation.actions == [
            Action(
                name="browser.click_coordinate", params={"coordinate_x": 700, "coordinate_y": 220}
            )
        ]
        (record,) = translation.records
        assert (record.translator_id, record.action_index) == ("index_to_coordinate", 0)
        assert record.payload["element_index"] == INDEX["clear"]
        assert record.payload["point"] == [700, 220]

    def test_a_type_keeps_its_text_and_clear(self, recorded: Recorder) -> None:
        sample, trace = recorded(
            [
                Action(
                    name="browser.type",
                    params={"index": INDEX["clear"], "text": "a", "clear": False},
                )
            ]
        )
        translation = IndexToCoordinateTranslator(occlusion="none").translate(sample, trace)
        assert translation.actions == [
            Action(
                name="browser.type_coordinate",
                params={"coordinate_x": 700, "coordinate_y": 220, "text": "a", "clear": False},
            )
        ]

    def test_an_iframe_element_is_placed_by_its_page_box(self, recorded: Recorder) -> None:
        """``bounds`` already include the frame's offset: no second offset."""
        sample, trace = recorded([_click("inside")])
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.actions is not None
        # page (868, 608, 48, 17) - scroll -> (868..916, 108..125)
        assert translation.actions[0].params == {"coordinate_x": 892, "coordinate_y": 116}
        assert translation.records[0].payload["frame_clip"] == "applied"

    def test_actions_without_an_element_pass_unchanged(self, recorded: Recorder) -> None:
        actions = [
            Action(name="browser.scroll", params={"direction": "down"}),
            Action(name="browser.navigate", params={"url": "http://shop.example.test/"}),
            _click("clear"),
            Action(name="browser.done", params={"extracted_content": "ok", "success": True}),
        ]
        sample, trace = recorded(actions)
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.actions is not None
        assert [a.name for a in translation.actions] == [
            "browser.scroll",
            "browser.navigate",
            "browser.click_coordinate",
            "browser.done",
        ]
        assert translation.actions[0] == actions[0]
        assert translation.actions[3] == actions[3]
        assert [r.action_index for r in translation.records] == [2]


class TestUntranslatable:
    @pytest.mark.parametrize(
        "action",
        [
            Action(name="browser.select_dropdown", params={"index": 7, "text": "Large"}),
            Action(name="browser.scroll", params={"direction": "down", "index": 7}),
            Action(name="browser.extract_content", params={"index": 7}),
        ],
    )
    def test_an_element_action_without_a_coordinate_counterpart(
        self, recorded: Recorder, action: Action
    ) -> None:
        sample, trace = recorded([action])
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.actions is None and translation.untranslatable is not None
        assert translation.untranslatable.reason == "no_counterpart"
        assert action.name in translation.untranslatable.detail

    def test_an_index_missing_from_the_recorded_selector_map(self, recorded: Recorder) -> None:
        sample, trace = recorded([Action(name="browser.click", params={"index": 4242})])
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.untranslatable is not None
        assert translation.untranslatable.reason == "no_counterpart"

    def test_off_screen(self, recorded: Recorder) -> None:
        sample, trace = recorded([_click("below")])
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.untranslatable is not None
        assert translation.untranslatable.reason == "target_offscreen"
        assert translation.records[0].payload["element_index"] == INDEX["below"]

    def test_no_recorded_dom(self, recorded: Recorder) -> None:
        sample, trace = recorded([_click("clear")], dom=False)
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.untranslatable is not None
        assert translation.untranslatable.reason == "no_counterpart"
        assert "browser.dom" in translation.untranslatable.detail

    def test_one_untranslatable_action_makes_the_step_untranslatable(
        self, recorded: Recorder
    ) -> None:
        sample, trace = recorded([_click("clear"), _click("below")])
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.actions is None
        assert [r.action_index for r in translation.records] == [0, 1]


class TestOcclusion:
    def test_hit_test_evidence_first(self, recorded: Recorder) -> None:
        state = sticky_state(
            hit_tests={
                "partial": HitTest(False, (302.0, 114.0)),
                "covered": HitTest(False, None),
            }
        )
        sample, trace = recorded([_click("partial")], state=state, documents=[raw_document()])
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.actions is not None
        assert translation.actions[0].params == {"coordinate_x": 302, "coordinate_y": 114}
        assert translation.records[0].payload["evidence"] == "hit_test"
        sample, trace = recorded([_click("covered")], state=state)
        refused = IndexToCoordinateTranslator().translate(sample, trace)
        assert refused.untranslatable is not None
        assert refused.untranslatable.reason == "occluded"

    @pytest.mark.parametrize("dpr", [1.0, 2.0])
    def test_dom_raw_evidence_before_the_traced_paint_order(
        self, recorded: Recorder, dpr: float
    ) -> None:
        sample, trace = recorded(
            [_click("partial")], state=sticky_state(), documents=[raw_document(dpr=dpr)]
        )
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.actions is not None
        assert translation.actions[0].params == {"coordinate_x": 302, "coordinate_y": 114}
        assert (
            translation.records[0].payload["evidence"],
            translation.records[0].payload["point_source"],
        ) == ("dom_raw", "grid")

    def test_dom_raw_fully_covered(self, recorded: Recorder) -> None:
        sample, trace = recorded(
            [_click("covered")], state=sticky_state(paint_orders=False), documents=[raw_document()]
        )
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.untranslatable is not None
        assert translation.untranslatable.reason == "occluded"
        assert translation.records[0].payload["evidence"] == "dom_raw"

    def test_traced_paint_order_evidence(self, recorded: Recorder) -> None:
        sample, trace = recorded([_click("partial")], state=sticky_state())
        translation = IndexToCoordinateTranslator().translate(sample, trace)
        assert translation.actions is not None
        assert translation.actions[0].params == {"coordinate_x": 302, "coordinate_y": 114}
        assert translation.records[0].payload["evidence"] == "paint_order"

    def test_no_evidence_raises_naming_it(self, recorded: Recorder) -> None:
        sample, trace = recorded([_click("clear")], state=sticky_state(paint_orders=False))
        with pytest.raises(OcclusionEvidenceMissingError) as excinfo:
            IndexToCoordinateTranslator().translate(sample, trace)
        message = str(excinfo.value)
        assert sample.sample_id in message
        assert f"[{INDEX['clear']}]" in message
        assert "bind browser.dom_raw or record with cleaner skyvern_hit_test" in message

    def test_no_evidence_as_an_untranslatable_reason(self, recorded: Recorder) -> None:
        sample, trace = recorded([_click("clear")], state=sticky_state(paint_orders=False))
        translator = IndexToCoordinateTranslator(occlusion_unknown="untranslatable")
        translation = translator.translate(sample, trace)
        assert translation.untranslatable is not None
        assert translation.untranslatable.reason == "occlusion_unknown"

    def test_off_reads_no_evidence(self, recorded: Recorder) -> None:
        sample, trace = recorded([_click("covered")], state=sticky_state(paint_orders=False))
        translation = IndexToCoordinateTranslator(occlusion="none").translate(sample, trace)
        assert translation.actions is not None
        assert translation.actions[0].params == {"coordinate_x": 140, "coordinate_y": 50}
        assert translation.records[0].payload["occlusion"] == "unchecked"

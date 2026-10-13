"""``skyvern_hit_test``: the Skyvern cleaner plus an in-page hit test (M32, arch §4.12).

The user's ruling ships the hit test as a *separate* cleaner so that the
existing ``skyvern`` cleaner stays byte-identical. The goldens under
``data/`` were written by the ``skyvern`` cleaner as it was before the
variant existed, from a real Chrome capture of the fixture site's ``sticky``
page (``tests/_helpers/dom_captures.py``); these tests hold every later
version to them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.dom.budget import DOMBudget, DOMBudgetPass
from spectragent.core.capabilities.browser.dom.cleaning import DOMCaptureSource
from spectragent.core.capabilities.browser.dom.cleaning.skyvern import SkyvernCleaner
from spectragent.core.capabilities.browser.dom.cleaning.skyvern_hit_test import (
    SkyvernHitTestCleaner,
)
from spectragent.core.capabilities.browser.dom.js import skyvern_capture
from spectragent.core.capabilities.browser.dom.js.skyvern_extraction import (
    EXTRACTION_SCRIPT,
    HIT_TEST_FUNCTION,
)
from spectragent.core.capabilities.browser.dom.models import DOMState, HitTest, TargetCapture
from spectragent.core.capabilities.browser.dom.serializers.text_serializer import TextSerializer
from spectragent.core.capabilities.browser.observations import DOMObs
from spectragent.core.composer import ConfigComposer
from spectragent.core.trace.encoding import encode_fragment
from spectragent.scripts.generate_all import _collect_registrars
from tests._helpers.dom_captures import (
    CAPTURES_DIR,
    capture_from_json,
    capture_to_json,
    load_capture,
)
from tests._helpers.dom_payloads import clean, reference_page

#: sha256 of ``EXTRACTION_SCRIPT`` before M32 — the page script is not touched.
EXTRACTION_SCRIPT_SHA256 = "6a1090d627150711730312c3f57d3edbc5396a6aebb78631cef42b6a90f1d0e1"
#: The fragment's capture time is wall clock; the golden pins it.
CAPTURED_AT = 1_700_000_000.0
#: sha256 of the ``browser.dom`` trace JSON the ``browser_use`` cleaner wrote for
#: ``dom_payloads.reference_page`` (without / with its overlay) before M32.
BROWSER_USE_DOM_JSON_SHA256 = {
    False: "2cbf2d6aadb315e37898bf5ee76c0a5832eb34eee6ca6736c386afa9985a37e6",
    True: "8dc58f1f56cf70d42c6218eed2286452a23c503fc51fe605b98838661fb6c68f",
}


async def _skyvern_dom_json(cleaner: Any) -> tuple[str, bytes]:
    """What the DOM watchdog hands the trace for the committed capture."""
    state = await cleaner.clean(load_capture("skyvern_sticky"), (1280, 720))
    data = _dom_json(state)  # serializes: fills ``serialized_dom``
    return state.serialized_dom, data


def _dom_json(state: DOMState) -> bytes:
    """The ``browser.dom`` trace file the DOM watchdog's fragment encodes to."""
    budget = DOMBudgetPass(DOMBudget()).apply(state, TextSerializer().serialize)
    obs = DOMObs(
        element_tree=state,
        selector_map=dict(state.selector_map),
        budget=budget,
        captured_at=CAPTURED_AT,
    )
    encoded = encode_fragment(0, obs)
    assert encoded is not None
    return encoded.file.data


class TestSkyvernUnchanged:
    """The ruling's proof obligation: the existing cleaner did not move."""

    def test_extraction_script_is_byte_identical(self) -> None:
        assert hashlib.sha256(EXTRACTION_SCRIPT.encode()).hexdigest() == EXTRACTION_SCRIPT_SHA256

    async def test_skyvern_cleaner_output_is_byte_identical(self) -> None:
        serialized, dom_json = await _skyvern_dom_json(SkyvernCleaner())
        golden_serialized = (CAPTURES_DIR / "skyvern_sticky.golden.serialized.txt").read_text(
            encoding="utf-8"
        )
        golden_json = (CAPTURES_DIR / "skyvern_sticky.golden.dom.json").read_bytes()
        assert serialized == golden_serialized
        assert dom_json == golden_json
        # The golden is the M18 defect itself: the link under the header is indexed.
        assert "[5]<a id=covered />" in serialized
        assert b'"hit_test"' not in dom_json

    async def test_browser_use_dom_json_is_byte_identical(self) -> None:
        """``hit_test`` is omitted when ``None``: other cleaners' trace JSON did not move."""
        for overlay, expected in BROWSER_USE_DOM_JSON_SHA256.items():
            data = _dom_json(await clean(reference_page(overlay=overlay)))
            assert b'"hit_test"' not in data
            assert hashlib.sha256(data).hexdigest() == expected

    async def test_capture_call_sequence_is_unchanged(self) -> None:
        """``capture_skyvern(source)`` sends exactly what it sent before M32."""
        calls, _capture = await _record_capture_calls()
        assert calls == [
            ("cdp", "Page.getFrameTree", ""),
            ("js", "Page.createIsolatedWorld", ""),
            ("js", "Runtime.evaluate", "EXTRACTION_SCRIPT"),
            (
                "js",
                "Runtime.callFunctionOn",
                "function(){return {tree:this.tree,scroll:this.scroll};}",
            ),
            ("js", "Runtime.callFunctionOn", "function(){return this.refs;}"),
            ("js", "Runtime.getProperties", ""),
            ("cdp", "DOM.describeNode", ""),
            ("js", "Runtime.releaseObjectGroup", ""),
        ]


async def _record_capture_calls(
    **kwargs: Any,
) -> tuple[list[tuple[str, str, str]], TargetCapture]:
    """Run ``capture_skyvern`` against a scripted one-document tab: what it sent, what it got."""
    calls: list[tuple[str, str, str]] = []
    tree = {
        "tagName": "html",
        "attributes": {},
        "interactive": False,
        "ref": None,
        "children": [
            {
                "tagName": "button",
                "attributes": {"id": "go"},
                "interactive": True,
                "ref": 0,
                "children": [],
                "rect": {"x": 10, "y": 10, "width": 40, "height": 20},
            }
        ],
        "rect": {"x": 0, "y": 0, "width": 1280, "height": 720},
    }

    class CDP:
        async def send(self, method: str, params: dict[str, Any], *, session_id: Any = None):
            calls.append(("cdp", method, ""))
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "root", "loaderId": "L1", "url": "u"}}}
            if method == "DOM.describeNode":
                return {"node": {"backendNodeId": 5, "nodeId": 0}}
            raise AssertionError(f"unexpected CDP command {method}")

    async def run_js(method: str, params: dict[str, Any], _session_id: Any) -> dict[str, Any]:
        declaration = params.get("functionDeclaration", "")
        if params.get("expression") == EXTRACTION_SCRIPT:
            declaration = "EXTRACTION_SCRIPT"
        if declaration == HIT_TEST_FUNCTION:
            declaration = "HIT_TEST_FUNCTION"
            assert params["objectId"] == "extraction" and params["returnByValue"] is True
        calls.append(("js", method, declaration))
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "extraction"}}
        if method == "Runtime.callFunctionOn":
            if "this.tree" in declaration:
                return {"result": {"value": {"tree": tree, "scroll": {"x": 0, "y": 0}}}}
            if "this.refs" in declaration:
                return {"result": {"objectId": "refs"}}
            return {"result": {"value": {"0": {"center_topmost": True, "offset": [20, 10]}}}}
        if method == "Runtime.getProperties":
            return {"result": [{"name": "0", "value": {"objectId": "ref0"}}]}
        if method == "Runtime.releaseObjectGroup":
            return {}
        raise AssertionError(f"unexpected JS command {method}")

    source = DOMCaptureSource(
        cdp=CDP(),  # type: ignore[arg-type]
        session_id="tab",
        frames=None,
        run_js=run_js,
    )
    capture = await skyvern_capture.capture_skyvern(source, **kwargs)
    return calls, capture


# ── the variant, offline: the committed capture plus hit-test results ──

#: Where each element of the committed capture lives: (frame path, element id).
_HIT_RESULTS: dict[str, dict[str, Any]] = {
    "home": {"center_topmost": True, "offset": [21.5, 11.0]},
    "covered": {"center_topmost": False, "offset": None},
    "partial": {"center_topmost": False, "offset": [2.0, 74.0]},
    "clear": {"center_topmost": True, "offset": [120.0, 20.0]},
    "agree": {"center_topmost": True, "offset": [10.0, 10.0]},
    "frame": {"center_topmost": True, "offset": [150.0, 60.0]},
    "inside": {"center_topmost": True, "offset": [25.5, 10.5]},
    # "below" is outside the viewport: the page script reports nothing for it
}


def _with_hit_tests(results: dict[str, dict[str, Any]] = _HIT_RESULTS) -> TargetCapture:
    """A copy of the committed capture, with the page's hit-test results per element id."""

    def add(capture: TargetCapture) -> None:
        tests: dict[int, Any] = {}
        for ref, node in capture.raw_dom["skyvern_nodes"].items():
            attributes = node.get("attributes") or []
            element_id = dict(zip(attributes[::2], attributes[1::2], strict=False)).get("id")
            if element_id in results:
                tests[ref] = results[element_id]
        capture.raw_dom["skyvern_hit_tests"] = tests
        for child in capture.frames.values():
            add(child)

    capture = capture_from_json(capture_to_json(load_capture("skyvern_sticky")))
    add(capture)
    return capture


def _by_id(state: DOMState) -> dict[str, Any]:
    assert state.dom_tree is not None
    return {
        node.attributes["id"]: node
        for node in [state.dom_tree, *state.dom_tree.descendants()]
        if "id" in node.attributes
    }


class TestTheVariant:
    async def test_drop_takes_a_fully_covered_element_out_of_the_selector_map(self) -> None:
        state = await SkyvernHitTestCleaner().clean(_with_hit_tests(), (1280, 720))
        indexed = {node.attributes.get("id") for node in state.selector_map.values()}
        assert indexed == {"home", "partial", "clear", "agree", "below", "frame", "inside"}
        covered = _by_id(state)["covered"]
        assert covered.element_index is None
        assert covered.hit_test == HitTest(center_topmost=False, visible_point=None)
        serialized = TextSerializer().serialize(state)
        # the agent still reads the link's text, but has no index to act on
        assert "Covered link" in serialized and "[5]" not in serialized

    async def test_keep_indexes_it_and_records_the_hit_test(self) -> None:
        cleaner = SkyvernHitTestCleaner(covered_elements="keep")
        state = await cleaner.clean(_with_hit_tests(), (1280, 720))
        assert state.selector_map[5].attributes["id"] == "covered"
        assert state.selector_map[5].hit_test == HitTest(False, None)

    async def test_visible_points_are_top_level_viewport_css_px(self) -> None:
        state = await SkyvernHitTestCleaner().clean(_with_hit_tests(), (1280, 720))
        nodes = _by_id(state)
        # page box (300, 540) + offset (2, 74) - scroll (0, 500)
        assert nodes["partial"].hit_test == HitTest(False, (302.0, 114.0))
        assert nodes["clear"].hit_test == HitTest(True, (700.0, 220.0))
        # a same-process iframe element: its page box already includes the frame offset
        assert nodes["inside"].hit_test == HitTest(True, (893.5, 120.5))

    async def test_off_screen_is_not_covered(self) -> None:
        state = await SkyvernHitTestCleaner().clean(_with_hit_tests(), (1280, 720))
        below = _by_id(state)["below"]
        assert below.hit_test is None and below.element_index == 9

    async def test_the_hit_test_reaches_the_trace_and_reads_back(self) -> None:
        state = await SkyvernHitTestCleaner().clean(_with_hit_tests(), (1280, 720))
        data = _dom_json(state)
        assert b'"hit_test"' in data
        restored = DOMObs.from_trace(json.loads(data), None)
        assert restored.selector_map[6].hit_test == HitTest(False, (302.0, 114.0))
        assert restored.selector_map[9].hit_test is None

    async def test_the_base_cleaner_ignores_hit_test_results(self) -> None:
        """``skyvern`` on a capture that carries them writes the golden bytes all the same."""
        state = await SkyvernCleaner().clean(_with_hit_tests(), (1280, 720))
        data = _dom_json(state)
        assert data == (CAPTURES_DIR / "skyvern_sticky.golden.dom.json").read_bytes()

    async def test_the_capture_adds_one_call_on_the_same_object(self) -> None:
        calls, capture = await _record_capture_calls(hit_test=True)
        assert calls[3:5] == [
            (
                "js",
                "Runtime.callFunctionOn",
                "function(){return {tree:this.tree,scroll:this.scroll};}",
            ),
            ("js", "Runtime.callFunctionOn", "HIT_TEST_FUNCTION"),
        ]
        assert calls[:4] + calls[5:] == (await _record_capture_calls())[0]
        assert capture.raw_dom["skyvern_hit_tests"] == {
            0: {"center_topmost": True, "offset": [20, 10]}
        }

    def test_composed_from_yaml_with_its_knob(self) -> None:
        def compose(cleaner: dict[str, Any]) -> Any:
            block = {
                "capability_id": "browser",
                "profile": "skyvern",
                "mouse_trajectory": {"trajectory_type": "instant"},
                "typing_rhythm": {"rhythm_type": "instant"},
                "replayability": {"replayability_id": "none"},
                "network_source": {"source_id": "live"},
                "dom_cleaner": cleaner,
                "dom_serializer": {"serializer_type": "text"},
                "backend": {"provider_id": "local_chrome", "headless": True},
            }
            composer = ConfigComposer(registrars=_collect_registrars())
            return composer.compose({"capability": [block]}).single("capability")

        cap = compose({"cleaner_type": "skyvern_hit_test", "covered_elements": "keep"})
        assert isinstance(cap, BrowserCapability)
        assert isinstance(cap.dom_cleaner, SkyvernHitTestCleaner)
        assert cap.dom_cleaner.covered_elements == "keep"
        default = compose({"cleaner_type": "skyvern_hit_test"}).dom_cleaner
        assert default.covered_elements == "drop"
        for wrong in (
            {"cleaner_type": "skyvern_hit_test", "covered_elements": "hide"},
            {"cleaner_type": "skyvern", "covered_elements": "drop"},
            {"cleaner_type": "skyvern_hit_tests"},
        ):
            with pytest.raises(ValidationError):
                compose(wrong)

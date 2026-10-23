"""Browser checkpoint restoration: retargeting recorded actions, measuring restored pages.

``docs/architecture_design.md`` §4.14 "恢复：重定位与校验". The recorded
observation always reaches these functions the way the restorer reads it —
encoded by the trace and decoded by ``from_trace`` — and the restored one is
the live object, so every assertion here crosses the asymmetry a real branch
run has: a decoded ``selector_map`` entry against a live node.

A relaunched browser is simulated by renumbering every handle (the backend
node id a fresh Chrome hands out) while keeping each element's position,
attributes and text — which is what pinned replay of the same archive
gives.
"""

from __future__ import annotations

import copy
import io
import json
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.base import RestorationSupport
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.dom.models import (
    FRAME_HANDLE_BASE,
    DOMElementNode,
    DOMRect,
    DOMState,
)
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.capabilities.browser.replay.page_urls import PageScope, PageUrlRule
from spectragent.core.capabilities.browser.replay.replay_log import ReplayLog, ReplayLogEntry
from spectragent.core.capabilities.browser.restoration import (
    TEXT_DEPTH_LIMIT,
    dhash,
    element_params,
    element_signature,
    hamming,
    measure,
    retarget_action,
    subtree_text,
)
from spectragent.core.loops.restore.checks import judge
from spectragent.core.trace.encoding import decode_fragment, encode_fragment
from spectragent.schemas.action import Action
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.failure import FragmentFailure
from tests._helpers.dom_payloads import CROSS, by_id, clean, reference_page
from tests._helpers.fragment_samples import FRAME_BUTTON_HANDLE, dom_page

TOLERANCE = 2.0
SHIFT = 1000


# ── building observations ────────────────────────────────────────────


def through_trace(fragment: FragmentBase) -> Any:
    """*fragment* as the restorer reads it from the source trace."""
    encoded = encode_fragment(0, fragment)
    assert encoded is not None
    artifact = encoded.artifact.data if encoded.artifact is not None else None
    return decode_fragment(encoded.key, type(fragment), json.loads(encoded.file.data), artifact)


def dom_of(state: DOMState) -> DOMObs:
    return DOMObs(element_tree=state, selector_map=dict(state.selector_map))


def relaunched(dom: DOMObs, shift: int = SHIFT) -> DOMObs:
    """The same page in a freshly launched browser: every handle renumbered, nothing else."""
    copied = copy.deepcopy(dom)
    renumbered: dict[int, Any] = {}
    for handle, node in copied.selector_map.items():
        namespace, backend = divmod(handle, FRAME_HANDLE_BASE)
        new = namespace * FRAME_HANDLE_BASE + backend + shift
        node.element_index = new
        node.backend_node_id = (node.backend_node_id or 0) + shift
        renumbered[new] = node
    copied.selector_map = renumbered
    return copied


def png(seed: int = 0, size: tuple[int, int] = (64, 36), fmt: str = "PNG") -> bytes:
    image = Image.new("RGB", size)
    pixels = image.load()
    assert pixels is not None
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = ((x * 7 + seed * 97) % 256, (y * 11 + seed * 31) % 256, (x * y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def meta(url: str = "https://shop.test/cart", tabs: list[dict[str, Any]] | None = None) -> Any:
    return BrowserMetaObs(url=url, page_title="Cart", tabs=tabs or [])


def shot(data: bytes | None) -> ScreenshotObs:
    return ScreenshotObs(image_bytes=data, viewport_width=1280, viewport_height=720)


def observation(**fragments: Any) -> Any:
    """A ``NegotiatedObservation`` stand-in: fragments under their local names."""
    return SimpleNamespace(**fragments)


def recorded_and_restored(
    dom: DOMObs, *, restored_dom: DOMObs | None = None, **extra: Any
) -> tuple[Any, Any]:
    recorded = observation(
        dom=through_trace(dom), **{k: through_trace(v) for k, v in extra.items()}
    )
    restored = observation(
        dom=restored_dom if restored_dom is not None else relaunched(dom), **extra
    )
    return recorded, restored


#: The browser actions as a capability holds them: by name, each with its Params.
ACTIONS = {key: cls for key, cls in ActionRegistrar.get_all().items() if key.startswith("browser.")}


def retarget(action: Action, recorded: Any, restored: Any, tolerance: float = TOLERANCE) -> Any:
    return retarget_action(action, recorded, restored, actions=ACTIONS, tolerance_px=tolerance)


# ── signatures ────────────────────────────────────────────────────────


class TestSignature:
    def test_the_recorded_and_the_live_element_sign_alike(self) -> None:
        """Every entry decoded from the trace has its live node's signature."""
        live = dom_of(dom_page())
        decoded = through_trace(live)
        assert decoded.selector_map.keys() == live.selector_map.keys()
        for handle, node in live.selector_map.items():
            assert element_signature(decoded.selector_map[handle]) == element_signature(node)

    async def test_a_cleaned_page_with_frames_signs_alike(self) -> None:
        live = dom_of(await clean(reference_page()))
        decoded = through_trace(live)
        for handle, node in live.selector_map.items():
            assert element_signature(decoded.selector_map[handle]) == element_signature(node)

    def test_text_is_read_from_text_nodes_and_text_content(self) -> None:
        state = dom_page()
        button = state.selector_map[4]
        assert subtree_text(button) == "Pay now", "the BrowserUse cleaner's #text child"
        assert subtree_text(state.selector_map[FRAME_BUTTON_HANDLE]) == "Confirm card"
        assert subtree_text(state.selector_map[8]) == "Confirm card", "into the frame document"

    def test_whitespace_is_folded(self) -> None:
        a = DOMElementNode(tag_name="a", text_content="  Pay\n\tnow ")
        b = DOMElementNode(tag_name="A", text_content="Pay now")
        assert element_signature(a) == element_signature(b)

    def test_role_attribute_else_the_ax_role(self) -> None:
        by_attribute = DOMElementNode(tag_name="div", attributes={"role": "button"})
        by_ax = DOMElementNode(tag_name="div", ax_role="button")
        neither = DOMElementNode(tag_name="div")
        assert element_signature(by_attribute) == element_signature(by_ax)
        assert element_signature(by_ax) != element_signature(neither)

    @pytest.mark.parametrize("attribute", ["id", "name", "aria-label"])
    def test_each_identifying_attribute_counts(self, attribute: str) -> None:
        plain = DOMElementNode(tag_name="input")
        named = DOMElementNode(tag_name="input", attributes={attribute: "q"})
        assert element_signature(plain) != element_signature(named)

    def test_other_attributes_and_position_do_not(self) -> None:
        a = DOMElementNode(tag_name="a", attributes={"href": "/x", "class": "c1"}, bounds=DOMRect())
        b = DOMElementNode(tag_name="a", attributes={"href": "/y"}, backend_node_id=99)
        assert element_signature(a) == element_signature(b)

    def test_text_below_the_depth_limit_is_not_read(self) -> None:
        root = node = DOMElementNode(tag_name="div")
        for _ in range(TEXT_DEPTH_LIMIT):
            child = DOMElementNode(tag_name="div")
            node.children.append(child)
            node = child
        node.children.append(DOMElementNode(tag_name="#text", node_type=3, node_value="deep"))
        assert subtree_text(root) == ""
        node.text_content = "at the limit"
        assert subtree_text(root) == "at the limit"


# ── retarget: element indexes ─────────────────────────────────────────


class TestIndexActions:
    def test_the_actions_that_name_an_element_are_the_ones_their_params_declare(self) -> None:
        # Read off each action's Params (a param marked RequiresFragment("browser.dom")),
        # not from a list restoration keeps of its own.
        named = {
            key: element_params(cls.Params)
            for key, cls in sorted(ACTIONS.items())
            if element_params(cls.Params)
        }
        assert named == {
            "browser.click": ("index",),
            "browser.extract_content": ("index",),
            "browser.scroll": ("index",),
            "browser.select_dropdown": ("index",),
            "browser.type": ("index",),
        }

    def test_a_name_the_capability_does_not_hold_runs_as_recorded(self) -> None:
        recorded, restored = recorded_and_restored(dom_of(dom_page()))
        refused = Action(name="click", params={"index": 4})
        result = retarget(refused, recorded, restored)
        assert result.method == "identity" and result.action == refused

    @pytest.mark.parametrize(
        ("name", "params"),
        [
            ("browser.click", {"index": 4}),
            ("browser.type", {"index": 4, "text": "hello", "clear": True}),
            ("browser.select_dropdown", {"index": 4, "value": "b"}),
            ("browser.scroll", {"index": 4, "direction": "down"}),
            ("browser.extract_content", {"index": 4}),
        ],
    )
    def test_each_family_is_refound_after_a_relaunch(
        self, name: str, params: dict[str, Any]
    ) -> None:
        recorded, restored = recorded_and_restored(dom_of(dom_page()))
        action = Action(name=name, params=params)
        result = retarget(action, recorded, restored)
        assert result.failure is None and result.method == "geometry_signature"
        assert result.action is not None
        assert result.action.params == {**params, "index": 4 + SHIFT}, "only the index moves"
        assert action.params == params, "the recorded action is not touched"

    @pytest.mark.parametrize("name", ["browser.scroll", "browser.extract_content"])
    def test_without_an_index_it_is_identity(self, name: str) -> None:
        recorded, restored = recorded_and_restored(dom_of(dom_page()))
        action = Action(name=name, params={"direction": "down"})
        result = retarget(action, recorded, restored)
        assert result.method == "identity" and result.action == action
        assert result.action is not action

    def test_nested_indexed_elements_are_told_apart(self) -> None:
        """The form holds the button, the iframe holds the frame button: each finds itself."""
        recorded, restored = recorded_and_restored(dom_of(dom_page()))
        for index in (3, 4, 8, FRAME_BUTTON_HANDLE):
            result = retarget(
                Action(name="browser.click", params={"index": index}), recorded, restored
            )
            namespace, backend = divmod(index, FRAME_HANDLE_BASE)
            assert result.action is not None, result.detail
            assert result.action.params["index"] == namespace * FRAME_HANDLE_BASE + backend + SHIFT

    async def test_an_out_of_process_frame_handle(self) -> None:
        """An element of the cross-origin frame keeps its namespace; its backend id is new."""
        state = await clean(reference_page())
        handle, _node = by_id(state, "cross-input")
        assert handle // FRAME_HANDLE_BASE == CROSS
        recorded, restored = recorded_and_restored(dom_of(state))
        result = retarget(
            Action(name="browser.type", params={"index": handle, "text": "x"}), recorded, restored
        )
        assert result.action is not None, result.detail
        assert result.action.params["index"] == handle + SHIFT
        assert result.action.params["index"] // FRAME_HANDLE_BASE == CROSS

    async def test_every_element_of_a_cleaned_page_is_refound(self) -> None:
        state = await clean(reference_page())
        recorded, restored = recorded_and_restored(dom_of(state))
        for handle in state.selector_map:
            result = retarget(
                Action(name="browser.click", params={"index": handle}), recorded, restored
            )
            assert result.action is not None, (handle, result.detail)
            assert result.action.params["index"] == handle + SHIFT

    def test_two_identical_candidates_are_ambiguous(self) -> None:
        dom = dom_of(dom_page())
        restored = relaunched(dom)
        twin = copy.deepcopy(restored.selector_map[4 + SHIFT])
        restored.selector_map[77_777] = twin
        recorded, live = recorded_and_restored(dom, restored_dom=restored)
        result = retarget(Action(name="browser.click", params={"index": 4}), recorded, live)
        assert result.action is None and result.failure == "ambiguous"
        assert result.method == "geometry_signature"
        assert "2 candidates" in result.detail

    @pytest.mark.parametrize("field", ["x", "y", "width", "height"])
    def test_each_bounds_value_has_the_tolerance(self, field: str) -> None:
        dom = dom_of(dom_page())
        for moved_by, found in ((TOLERANCE, True), (TOLERANCE + 0.5, False)):
            restored = relaunched(dom)
            bounds = restored.selector_map[4 + SHIFT].bounds
            setattr(bounds, field, getattr(bounds, field) + moved_by)
            recorded, live = recorded_and_restored(dom, restored_dom=restored)
            result = retarget(Action(name="browser.click", params={"index": 4}), recorded, live)
            assert (result.action is not None) is found, (field, moved_by, result.detail)
            if not found:
                assert result.failure == "not_found"

    def test_the_tolerance_is_the_callers(self) -> None:
        dom = dom_of(dom_page())
        restored = relaunched(dom)
        restored.selector_map[4 + SHIFT].bounds.y += 5
        recorded, live = recorded_and_restored(dom, restored_dom=restored)
        action = Action(name="browser.click", params={"index": 4})
        assert retarget(action, recorded, live).failure == "not_found"
        assert retarget(action, recorded, live, tolerance=5.0).action is not None

    def test_same_place_other_element_is_not_found(self) -> None:
        dom = dom_of(dom_page())
        restored = relaunched(dom)
        text = restored.selector_map[4 + SHIFT].children[0]
        text.node_value = "Pay later"
        recorded, live = recorded_and_restored(dom, restored_dom=restored)
        result = retarget(Action(name="browser.click", params={"index": 4}), recorded, live)
        assert result.action is None and result.failure == "not_found"
        assert "1 candidate(s) by position" in result.detail

    def test_an_index_the_recording_never_had(self) -> None:
        recorded, restored = recorded_and_restored(dom_of(dom_page()))
        result = retarget(Action(name="browser.click", params={"index": 999}), recorded, restored)
        assert result.failure == "not_found" and "not in the recorded selector map" in result.detail

    def test_a_recorded_element_without_bounds(self) -> None:
        state = dom_page()
        state.selector_map[4].bounds = None
        recorded, restored = recorded_and_restored(dom_of(state))
        result = retarget(Action(name="browser.click", params={"index": 4}), recorded, restored)
        assert result.failure == "not_found" and "no bounds" in result.detail

    @pytest.mark.parametrize("side", ["recorded", "restored"])
    def test_no_dom_on_either_side(self, side: str) -> None:
        dom = dom_of(dom_page())
        recorded, restored = recorded_and_restored(dom)
        failure = FragmentFailure(fragment_id="browser.dom", capability="browser", error="E")
        if side == "recorded":
            recorded = observation(dom=failure)
        else:
            restored = observation()
        result = retarget(Action(name="browser.click", params={"index": 4}), recorded, restored)
        assert result.failure == "not_found" and side in result.detail


# ── retarget: tabs and the rest ───────────────────────────────────────


def tabs(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {"tab_id": tab_id, "title": "", "url": url, "is_active": False} for tab_id, url in pairs
    ]


class TestSwitchTab:
    def test_by_tab_order_and_url(self) -> None:
        recorded = observation(
            browser_meta=meta(tabs=tabs(("A", "https://a.test/"), ("B", "https://b.test/")))
        )
        restored = observation(
            browser_meta=meta(tabs=tabs(("X", "https://a.test/"), ("Y", "https://b.test/")))
        )
        result = retarget(
            Action(name="browser.switch_tab", params={"target_id": "B"}), recorded, restored
        )
        assert result.method == "tab_order_url" and result.failure is None
        assert result.action == Action(name="browser.switch_tab", params={"target_id": "Y"})

    def test_the_most_recent_tab_is_identity(self) -> None:
        action = Action(name="browser.switch_tab", params={"target_id": None})
        result = retarget(action, observation(), observation())
        assert result.method == "identity" and result.action == action

    def test_more_restored_tabs_still_map_by_position(self) -> None:
        recorded = observation(browser_meta=meta(tabs=tabs(("A", "https://a.test/"))))
        restored = observation(
            browser_meta=meta(tabs=tabs(("X", "https://a.test/"), ("Z", "https://z.test/")))
        )
        result = retarget(
            Action(name="browser.switch_tab", params={"target_id": "A"}), recorded, restored
        )
        assert result.action is not None and result.action.params["target_id"] == "X"

    @pytest.mark.parametrize(
        ("restored_tabs", "fragment"),
        [
            (tabs(("X", "https://a.test/")), "1 tab(s) restored, 2 recorded"),
            (tabs(("X", "https://a.test/"), ("Y", "https://other.test/")), "recorded at"),
        ],
        ids=["fewer_tabs", "another_url"],
    )
    def test_not_found(self, restored_tabs: list[dict[str, Any]], fragment: str) -> None:
        recorded = observation(
            browser_meta=meta(tabs=tabs(("A", "https://a.test/"), ("B", "https://b.test/")))
        )
        restored = observation(browser_meta=meta(tabs=restored_tabs))
        result = retarget(
            Action(name="browser.switch_tab", params={"target_id": "B"}), recorded, restored
        )
        assert result.action is None and result.failure == "not_found"
        assert result.method == "tab_order_url" and fragment in result.detail

    def test_an_unknown_tab(self) -> None:
        recorded = observation(browser_meta=meta(tabs=tabs(("A", "https://a.test/"))))
        result = retarget(
            Action(name="browser.switch_tab", params={"target_id": "Q"}), recorded, recorded
        )
        assert result.failure == "not_found" and "not among the recorded tabs" in result.detail

    def test_no_browser_meta(self) -> None:
        result = retarget(
            Action(name="browser.switch_tab", params={"target_id": "A"}),
            observation(),
            observation(),
        )
        assert result.failure == "not_found" and "browser_meta" in result.detail


@pytest.mark.parametrize(
    "action",
    [
        Action(name="browser.click_coordinate", params={"x": 10, "y": 20}),
        Action(name="browser.type_coordinate", params={"x": 10, "y": 20, "text": "a"}),
        Action(name="browser.navigate", params={"url": "https://shop.test/"}),
        Action(name="browser.go_back"),
        Action(name="browser.go_forward"),
        Action(name="browser.send_keys", params={"keys": "Enter"}),
        Action(name="browser.wait", params={"seconds": 1}),
        Action(name="browser.done", params={"answer": "ok"}),
        Action(name="browser.drag", params={"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4}),
    ],
    ids=lambda action: action.name,
)
def test_everything_else_runs_as_recorded(action: Action) -> None:
    recorded, restored = recorded_and_restored(dom_of(dom_page()))
    result = retarget(action, recorded, restored)
    assert result.method == "identity" and result.failure is None
    assert result.action == action and result.action is not action


# ── measure ───────────────────────────────────────────────────────────


class TestDhash:
    def test_identical_bytes_are_zero_apart(self) -> None:
        a = dhash(png(1))
        assert a is not None and hamming(a, dhash(png(1)) or 0) == 0

    def test_the_same_pixels_in_another_encoding_are_close(self) -> None:
        a, b = dhash(png(1)), dhash(png(1, fmt="JPEG"))
        assert a is not None and b is not None and hamming(a, b) <= 5

    def test_another_page_is_far(self) -> None:
        a, b = dhash(png(1)), dhash(png(2))
        assert a is not None and b is not None and hamming(a, b) > 5

    def test_bytes_that_are_no_image(self) -> None:
        assert dhash(b"\x89PNG\r\n\x1a\nnot really") is None


#: A page-URL rule of the replay's rule set: the cart page writes ``session`` into its address.
CART_SESSION = PageUrlRule(
    id="cart-session", scope=PageScope(host="shop.test", path="/cart"), ignore_query=("session",)
)


def full(
    state: DOMState | None = None,
    *,
    url: str = "https://shop.test/cart",
    image: bytes | None = None,
) -> dict[str, Any]:
    return {
        "dom": dom_of(state or dom_page()),
        "browser_meta": meta(url),
        "screenshot": shot(png(1) if image is None else image),
    }


def recorded_view(fragments: dict[str, Any]) -> Any:
    return observation(**{name: through_trace(value) for name, value in fragments.items()})


def restored_view(fragments: dict[str, Any]) -> Any:
    restored = dict(fragments)
    restored["dom"] = relaunched(fragments["dom"])
    return observation(**restored)


class TestMeasure:
    @pytest.mark.parametrize(
        "restored_url, expected",
        [("https://shop.test/a", True), ("https://shop.test/b", False), (None, None)],
    )
    def test_visual_observations_compare_the_screenshot_source_url(
        self, restored_url: str | None, expected: bool | None
    ) -> None:
        before = shot(png())
        before.source_url = "https://shop.test/a"
        after = shot(png())
        after.source_url = restored_url
        evidence = measure(
            recorded_view({"screenshot": before}),
            observation(screenshot=after),
            capability_id="browser",
            document_misses=0,
            media=None,
        )
        assert evidence.url_match is expected
        assert evidence.recorded_url == before.source_url
        assert evidence.restored_url == restored_url
        assert evidence.signature_match is None
        assert evidence.screenshot_distance == 0

    def test_a_faithful_restoration_measures_clean(self) -> None:
        fragments = full()
        evidence = measure(
            recorded_view(fragments),
            restored_view(fragments),
            capability_id="browser",
            document_misses=0,
            media=None,
        )
        assert evidence.model_dump() == {
            "capability_id": "browser",
            "url_match": True,
            "recorded_url": "https://shop.test/cart",
            "restored_url": "https://shop.test/cart",
            "url_ignored_query": [],
            "url_rule_ids": [],
            "signature_match": True,
            "signature_missing": 0,
            "signature_extra": 0,
            "screenshot_distance": 0,
            "document_misses": 0,
            "media": None,
        }

    def test_page_url_rules_leave_their_parameters_out_and_the_evidence_says_which(self) -> None:
        recorded = full(url="https://shop.test/cart?session=1&item=7")
        restored = full(url="https://shop.test/cart?session=2&item=7")
        evidence = measure(
            recorded_view(recorded),
            restored_view(restored),
            capability_id="browser",
            document_misses=0,
            media=None,
            page_urls=(CART_SESSION,),
        )
        assert evidence.url_match is True
        assert (evidence.recorded_url, evidence.restored_url) == (
            "https://shop.test/cart?session=1&item=7",
            "https://shop.test/cart?session=2&item=7",
        ), "the evidence keeps both URLs as observed"
        assert (evidence.url_ignored_query, evidence.url_rule_ids) == (
            ["session"],
            ["cart-session"],
        )

    def test_with_page_url_rules_every_other_difference_still_mismatches(self) -> None:
        evidence = measure(
            recorded_view(full(url="https://shop.test/cart?session=1&item=7")),
            restored_view(full(url="https://shop.test/cart?session=2&item=8")),
            capability_id="browser",
            document_misses=0,
            media=None,
            page_urls=(CART_SESSION,),
        )
        assert evidence.url_match is False and evidence.url_ignored_query == ["session"]

    def test_nothing_measurable_is_none(self) -> None:
        evidence = measure(
            observation(), observation(), capability_id="browser", document_misses=None, media=None
        )
        assert evidence.url_match is None and evidence.recorded_url is None
        assert evidence.signature_match is None
        assert (evidence.signature_missing, evidence.signature_extra) == (0, 0)
        assert evidence.screenshot_distance is None and evidence.document_misses is None

    @pytest.mark.parametrize("missing", ["browser_meta", "dom", "screenshot"])
    def test_one_side_without_a_fragment(self, missing: str) -> None:
        fragments = full()
        restored = restored_view(fragments)
        delattr(restored, missing)
        evidence = measure(
            recorded_view(fragments),
            restored,
            capability_id="browser",
            document_misses=None,
            media=None,
        )
        measured = {
            "browser_meta": evidence.url_match,
            "dom": evidence.signature_match,
            "screenshot": evidence.screenshot_distance,
        }
        assert measured.pop(missing) is None
        assert all(value is not None for value in measured.values())

    def test_a_screenshot_without_bytes_is_not_measured(self) -> None:
        fragments = full()
        restored = restored_view(fragments)
        restored.screenshot = shot(None)
        evidence = measure(
            recorded_view(fragments),
            restored,
            capability_id="browser",
            document_misses=None,
            media=None,
        )
        assert evidence.screenshot_distance is None

    def test_the_signature_multiset_counts_missing_and_extra(self) -> None:
        recorded = full()
        state = dom_page()
        del state.selector_map[3]  # the form is gone
        state.selector_map[4].children[0].node_value = "Pay later"  # the button changed
        evidence = measure(
            recorded_view(recorded),
            observation(dom=relaunched(dom_of(state))),
            capability_id="browser",
            document_misses=None,
            media=None,
        )
        assert evidence.signature_match is False
        assert (evidence.signature_missing, evidence.signature_extra) == (2, 1)


# ── one case per measured reason, judged by the restorer's rules ──────


def _diverged_document() -> ReplayLogEntry:
    return ReplayLogEntry(
        kind="miss",
        captured_at=1.0,
        cycle=1,
        method="GET",
        url="https://shop.test/gone",
        resource_type="Document",
        frame="main",
        request_class="document",
        reason="no_match",
        answer="failed",
        diverged=True,
    )


def replaying_capability(
    log: ReplayLog, page_urls: tuple[PageUrlRule, ...] = ()
) -> BrowserCapability:
    """A capability whose network source intercepts: only its replay log and page-URL rules are read."""
    capability = BrowserCapability()
    capability._replay_server = SimpleNamespace(log=log, page_urls=page_urls)  # type: ignore[assignment]
    return capability


async def verdict(capability: BrowserCapability, recorded: Any, restored: Any) -> Any:
    evidence = await capability.verify_restoration(recorded, restored)
    return judge(
        2,
        [evidence],
        [],
        verify="strict",
        screenshot_max_distance=5,
        media_time_tolerance_s=1.0,
    )


class TestReasons:
    async def test_url_mismatch(self) -> None:
        fragments = full()
        restored = restored_view(fragments)
        restored.browser_meta = meta("https://shop.test/elsewhere")
        check = await verdict(BrowserCapability(), recorded_view(fragments), restored)
        assert check.reasons == ["url_mismatch"]
        assert check.evidence[0].restored_url == "https://shop.test/elsewhere"

    async def test_the_replays_page_url_rules_decide_the_url_check(self) -> None:
        """Under a replay its frozen page-URL rules apply; a live network has none."""
        recorded = recorded_view(full(url="https://shop.test/cart?session=1"))
        restored = restored_view(full(url="https://shop.test/cart?session=2"))
        replaying = replaying_capability(ReplayLog(), page_urls=(CART_SESSION,))
        check = await verdict(replaying, recorded, restored)
        assert check.verdict == "ok"
        assert check.evidence[0].url_ignored_query == ["session"]
        live = await verdict(BrowserCapability(), recorded, restored)
        assert live.reasons == ["url_mismatch"]
        assert (live.evidence[0].url_ignored_query, live.evidence[0].url_rule_ids) == ([], [])

    async def test_signature_mismatch(self) -> None:
        fragments = full()
        state = dom_page()
        del state.selector_map[8]
        restored = restored_view(fragments)
        restored.dom = relaunched(dom_of(state))
        check = await verdict(BrowserCapability(), recorded_view(fragments), restored)
        assert check.reasons == ["signature_mismatch"]

    async def test_screenshot_distance(self) -> None:
        fragments = full()
        restored = restored_view(fragments)
        restored.screenshot = shot(png(2))
        check = await verdict(BrowserCapability(), recorded_view(fragments), restored)
        assert check.reasons == ["screenshot_distance"]
        assert check.evidence[0].screenshot_distance > 5

    async def test_identical_screenshot_bytes_are_zero(self) -> None:
        fragments = full()
        check = await verdict(
            BrowserCapability(), recorded_view(fragments), restored_view(fragments)
        )
        assert check.verdict == "ok" and check.evidence[0].screenshot_distance == 0

    async def test_document_miss(self) -> None:
        log = ReplayLog(mode="summary", repeat_limit=1)
        capability = replaying_capability(log)
        fragments = full()
        recorded, restored = recorded_view(fragments), restored_view(fragments)

        assert (await verdict(capability, recorded, restored)).verdict == "ok"
        for _ in range(3):
            log.append(_diverged_document())  # two of them folded
        check = await verdict(capability, recorded, restored)
        assert check.reasons == ["document_miss"]
        assert check.evidence[0].document_misses == 3
        log.drain()  # browser.replay_log's collection does not reset the count
        again = await verdict(capability, recorded, restored)
        assert again.verdict == "ok" and again.evidence[0].document_misses == 0, (
            "only the misses since the previous measurement"
        )

    async def test_document_misses_are_reported_once(self) -> None:
        """The contract: a count since the previous call — measuring the same pair again reads 0."""
        log = ReplayLog()
        capability = replaying_capability(log)
        fragments = full()
        recorded, restored = recorded_view(fragments), restored_view(fragments)
        log.append(_diverged_document())
        first = await capability.verify_restoration(recorded, restored)
        second = await capability.verify_restoration(recorded, restored)
        assert (first.document_misses, second.document_misses) == (1, 0)

    async def test_a_live_network_measures_no_document_misses(self) -> None:
        fragments = full()
        evidence = await BrowserCapability().verify_restoration(
            recorded_view(fragments), restored_view(fragments)
        )
        assert evidence.document_misses is None and evidence.capability_id == "browser"


# ── the capability ────────────────────────────────────────────────────


class TestTheCapability:
    def test_it_implements_restoration_support(self) -> None:
        assert isinstance(BrowserCapability(), RestorationSupport)

    async def test_retarget_uses_the_tolerance_knob(self) -> None:
        dom = dom_of(dom_page())
        restored = relaunched(dom)
        restored.selector_map[4 + SHIFT].bounds.x += 4
        recorded, live = recorded_and_restored(dom, restored_dom=restored)
        action = Action(name="browser.click", params={"index": 4})
        assert (await BrowserCapability().retarget(action, recorded, live)).failure == "not_found"
        found = await BrowserCapability(retarget_tolerance_px=4.0).retarget(action, recorded, live)
        assert found.action is not None and found.action.params["index"] == 4 + SHIFT

    def test_the_tolerance_is_a_yaml_knob_never_negative(self) -> None:
        from pydantic import TypeAdapter

        from spectragent.generated._composed import BrowserCapabilityConfig

        spec = BrowserCapabilityConfig.model_fields["retarget_tolerance_px"]
        assert spec.default == 2.0
        knob: TypeAdapter[Any] = TypeAdapter(spec.rebuild_annotation())
        assert knob.validate_python(3.5) == 3.5
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            knob.validate_python(-1)

    async def test_teardown_forgets_the_count(self) -> None:
        log = ReplayLog()
        capability = replaying_capability(log)
        capability._replay_server.close = lambda: None  # type: ignore[union-attr]
        log.append(_diverged_document())
        assert capability._document_misses_since_last_check() == 1
        assert capability._diverged_seen == 1
        await capability.teardown()
        assert capability._replay_server is None and capability._diverged_seen == 0
        assert capability._document_misses_since_last_check() is None

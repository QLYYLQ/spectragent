"""Populated instances of every shipped observation fragment.

For tests that must cover *every* registered fragment class rather than
the ones their author remembered — first among them the trace round trip
(``trace_binary`` / ``trace_json_exclude`` / ``from_trace``) in
``tests/test_schemas/test_observation/test_fragment_from_trace.py``.

``SAMPLES`` maps a fully-qualified fragment key to named variants. Each
variant fills the fields that make its class's encoding non-trivial: the
DOM page uses every traced field type ``DOMElementNode`` has, nests an
indexed element inside an indexed iframe, and wires ``parent`` the way the
cleaners do; the screenshots carry real PNG and JPEG headers so the
artifact suffix is decided by the bytes. A guard test fails, naming this
file, the moment a fragment class is registered without a sample here.

The samples stay inside every encoder budget (no ``__truncated__``) and
reach no node twice (no ``__cycle__``), so the round trip over them is
exact — the lossy corners of the DOM encoding are tested separately.
"""

from __future__ import annotations

from collections.abc import Callable

from conscribe import discover

from spectragent.core.capabilities.browser.dom.budget import DOMBudgetReport
from spectragent.core.capabilities.browser.dom.models import (
    FRAME_HANDLE_BASE,
    DOMElementNode,
    DOMRect,
    DOMState,
    EnhancedAXProperty,
    ScrollInfo,
)
from spectragent.core.capabilities.browser.observations import (
    BrowserCaptureStateObs,
    BrowserMetaObs,
    BrowserPlatformObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.capabilities.browser.settle import PageSettle
from spectragent.core.capabilities.browser.tabs import FocusMove
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase, ObservationRegistrar
from spectragent.schemas.observation.capture_state import FailedVisibleResources, PointerState
from spectragent.schemas.observation.fragments import (
    ActionResultObs,
    HistoryObs,
    StepInfoObs,
    TaskObs,
    TerminalObs,
    ToolResultObs,
)
from spectragent.schemas.step_record import StepRecord, TokenUsage

#: A PNG signature plus filler — ``image_suffix`` reads only the header.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
#: A JPEG start-of-image marker plus filler.
JPEG_BYTES = b"\xff\xd8\xff\xe0" + bytes(range(64))

#: Handle of the button inside the indexed out-of-process frame (namespace 1).
FRAME_BUTTON_HANDLE = FRAME_HANDLE_BASE + 30


def _attach(parent: DOMElementNode, child: DOMElementNode) -> DOMElementNode:
    child.parent = parent
    parent.children.append(child)
    return child


def _text(parent: DOMElementNode, backend_node_id: int, value: str) -> DOMElementNode:
    """A ``#text`` node — where the BrowserUse cleaner keeps an element's text."""
    return _attach(
        parent,
        DOMElementNode(
            tag_name="#text", node_type=3, node_value=value, backend_node_id=backend_node_id
        ),
    )


def dom_page() -> DOMState:
    """A small page using every traced ``DOMElementNode`` field type.

    ``html > body > [form(indexed) > button(indexed), iframe(indexed) >
    #document > button(indexed, frame target), widget > #shadow-root >
    span]``. The form's entry contains the button's, and the iframe's entry
    contains the frame button's — the nesting the selector map used to
    write as stubs.
    """
    html = DOMElementNode(tag_name="html", node_type=1, node_id=1, backend_node_id=1)
    body = _attach(html, DOMElementNode(tag_name="body", backend_node_id=2, is_scrollable=True))

    form = _attach(
        body,
        DOMElementNode(
            tag_name="form",
            backend_node_id=3,
            attributes={"id": "checkout", "action": "/pay"},
            bounds=DOMRect(x=10.0, y=100.0, width=400.0, height=200.0),
            client_rect=DOMRect(x=0.0, y=0.0, width=400.0, height=200.0),
            scroll_rects=DOMRect(x=0.0, y=0.0, width=400.0, height=600.0),
            is_scrollable=True,
            element_index=3,
            is_interactive=True,
            paint_order=4,
        ),
    )
    button = _attach(
        form,
        DOMElementNode(
            tag_name="button",
            backend_node_id=4,
            frame_id="MAIN",
            session_id="tab",
            attributes={"type": "submit", "aria-label": "Pay now"},
            bounds=DOMRect(x=20.0, y=250.0, width=80.0, height=24.0),
            computed_styles={"display": "inline-block", "cursor": "pointer"},
            cursor_style="pointer",
            ax_role="button",
            ax_name="Pay now",
            ax_properties=[
                EnhancedAXProperty(name="focusable", value=True),
                EnhancedAXProperty(name="level", value=2),
                EnhancedAXProperty(name="valuetext", value="pay"),
                EnhancedAXProperty(name="description", value=None),
            ],
            element_index=4,
            is_interactive=True,
            has_js_click_listener=True,
            is_new=True,
            paint_order=5,
        ),
    )
    _text(button, 5, "Pay now")

    hidden = _attach(
        body,
        DOMElementNode(tag_name="div", backend_node_id=6, is_visible=False, should_display=False),
    )
    _text(hidden, 7, "not rendered")

    iframe = _attach(
        body,
        DOMElementNode(
            tag_name="iframe",
            backend_node_id=8,
            attributes={"id": "card-frame", "src": "https://pay.example/card"},
            bounds=DOMRect(x=10.0, y=320.0, width=300.0, height=120.0),
            element_index=8,
            is_interactive=True,
        ),
    )
    frame_doc = DOMElementNode(
        tag_name="#document",
        node_type=9,
        backend_node_id=1,
        frame_id="CARD",
        target_id="CARD",
        session_id="card",
    )
    frame_doc.parent = iframe
    iframe.content_document = frame_doc
    frame_button = _attach(
        frame_doc,
        DOMElementNode(
            tag_name="button",
            backend_node_id=30,
            frame_id="CARD",
            target_id="CARD",
            session_id="card",
            text_content="Confirm card",
            bounds=DOMRect(x=20.0, y=330.0, width=60.0, height=20.0),
            element_index=FRAME_BUTTON_HANDLE,
            is_interactive=True,
        ),
    )

    host = _attach(body, DOMElementNode(tag_name="price-widget", backend_node_id=9))
    shadow = DOMElementNode(tag_name="#shadow-root", node_type=11, backend_node_id=10)
    shadow.parent = host
    host.shadow_root_type = "open"
    host.shadow_roots = [shadow]
    _text(_attach(shadow, DOMElementNode(tag_name="span", backend_node_id=11)), 12, "$42")

    return DOMState(
        dom_tree=html,
        selector_map={3: form, 4: button, 8: iframe, FRAME_BUTTON_HANDLE: frame_button},
        scroll_info=ScrollInfo(
            scroll_x=0.0,
            scroll_y=120.0,
            scroll_width=1280.0,
            scroll_height=2400.0,
            viewport_width=1280.0,
            viewport_height=720.0,
        ),
        serialized_dom=(
            "[3]<form id=checkout />\n\t[4]<button>Pay now</button>\n"
            f"[8]<iframe id=card-frame />\n\t[{FRAME_BUTTON_HANDLE}]<button>Confirm card</button>"
        ),
    )


def _dom_page_fragment() -> DOMObs:
    state = dom_page()
    return DOMObs(
        element_tree=state,
        selector_map=dict(state.selector_map),
        budget=DOMBudgetReport(
            applied=["max_elements"],
            elements_before=9,
            elements_after=4,
            serialized_chars_before=512,
            serialized_chars_after=180,
        ),
    )


def _history() -> HistoryObs:
    return HistoryObs(
        items=[
            StepRecord(
                step=0,
                observation_summary="4 interactive elements",
                llm_output='{"action": [{"browser.click": {"index": 4}}]}',
                actions=[Action(name="browser.click", params={"index": 4})],
                action_results=[ActionResult(success=True, extracted_content="clicked")],
                timestamp=1760000000.5,
                token_usage=TokenUsage(prompt_tokens=30, completion_tokens=15, total_tokens=45),
                duration_ms=1200,
                llm_latency_ms=900,
            )
        ]
    )


SAMPLES: dict[str, dict[str, Callable[[], FragmentBase]]] = {
    "browser.platform": {
        "macos": lambda: BrowserPlatformObs(operating_system="macos"),
        "windows": lambda: BrowserPlatformObs(operating_system="windows"),
        "linux": lambda: BrowserPlatformObs(operating_system="linux"),
    },
    "browser.dom": {
        "page": _dom_page_fragment,
        "empty": DOMObs,
    },
    "browser.screenshot": {
        "png": lambda: ScreenshotObs(
            image_bytes=PNG_BYTES,
            viewport_width=1280,
            viewport_height=720,
            image_width=2560,
            image_height=1440,
            source_url="https://example.test/page#details",
        ),
        "jpeg": lambda: ScreenshotObs(image_bytes=JPEG_BYTES, viewport_width=800),
        "path_only": lambda: ScreenshotObs(image_path="/tmp/shot.png", viewport_width=1280),
        # Writes no artifact: a committed 0-byte file reads back as damage.
        "empty_capture": lambda: ScreenshotObs(image_bytes=b"", viewport_width=1280),
    },
    "browser.browser_meta": {
        "tabs": lambda: BrowserMetaObs(
            url="https://shop.example/checkout",
            page_title="Checkout",
            tabs=[{"id": "T1", "url": "https://shop.example/checkout", "active": True}],
            loader_id="7C0FFEE0LOADER",
        ),
    },
    "browser.capture_state": {
        # Every fact present: a capped wait, a followed tab, a known pointer, failed images.
        "measured": lambda: BrowserCaptureStateObs(
            settle=PageSettle(
                waited_ms=5000, outcome="capped", waiting_for=["load", "dom", "content"]
            ),
            focus_moved=FocusMove(reason="new_tab", from_tab="T1", to_tab="T2"),
            pointer=PointerState(status="known", x=120, y=95),
            failed_visible_resources=FailedVisibleResources(
                status="measured", count=3, identifiers=("missing.png", "hero.webp")
            ),
        ),
        # Nothing measured: no wait, no move, the pointer and the probe unknown.
        "unknown": lambda: BrowserCaptureStateObs(
            pointer=PointerState(status="unknown"),
            failed_visible_resources=FailedVisibleResources(
                status="unknown", detail="the probe got no answer within 1s"
            ),
        ),
    },
    "loop.action_result": {
        "failed": lambda: ActionResultObs(
            last_action="browser.click", success=False, error="element 9 is not in the map"
        ),
    },
    "loop.task": {
        "follow_ups": lambda: TaskObs(user_request="Find the price", follow_ups=["in EUR"]),
    },
    "loop.step_info": {
        "counters": lambda: StepInfoObs(step=3, max_steps=20, today="2026-10-10"),
    },
    "loop.history": {
        "one_step": _history,
    },
    "docker.terminal": {
        "failed_command": lambda: TerminalObs(
            stdout="collected 3 items", stderr="1 failed", exit_code=1, cwd="/repo"
        ),
    },
    "tool_sandbox.tool_result": {
        "structured": lambda: ToolResultObs(
            tool_name="search", result={"hits": [{"title": "a", "score": 0.5}]}, success=True
        ),
    },
}


def shipped_fragment_keys() -> set[str]:
    """Every fragment key a module under ``spectragent`` registers.

    Discovers the two packages fragments live in first, so a fragment
    whose module nothing has imported yet is still counted. Test-local
    stubs (registered from ``tests``) are excluded.
    """
    discover("spectragent.schemas")
    discover("spectragent.core.capabilities")
    return {
        key
        for key, cls in ObservationRegistrar.get_all().items()
        if getattr(cls, "__module__", "").startswith("spectragent.")
    }

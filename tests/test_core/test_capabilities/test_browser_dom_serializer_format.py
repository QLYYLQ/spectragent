"""Each DOM serializer describes its own output, and the description is true of it.

``docs/architecture_design.md`` §4.4 "观测格式由提供方描述" and §4.12: the
serializer that writes the DOM text owns the few lines a model reads to
understand it (``describe_format``), and agent prompts quote those lines
verbatim. So the description must hold of what ``serialize`` actually
writes. Checked here on real ``DOMState``s — the BrowserUse cleaner on
synthetic CDP payloads and the Skyvern cleaner on a committed real-Chrome
capture — never on hand-written element text, which is how a prompt
teaching ``[42]`` beside an observation written as ``data-idx="42"`` went
unnoticed.
"""

from __future__ import annotations

import re

import pytest

from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.dom.cleaning.skyvern import SkyvernCleaner
from spectragent.core.capabilities.browser.dom.models import DOMState, TargetCapture
from spectragent.core.capabilities.browser.dom.serializers import (
    DOMSerializerProtocol,
    DOMSerializerRegistrar,
)
from spectragent.core.capabilities.browser.dom.serializers.html_serializer import HTMLSerializer
from spectragent.core.capabilities.browser.dom.serializers.text_serializer import TextSerializer
from tests._helpers.dom_captures import load_capture
from tests._helpers.dom_payloads import Doc, El, clean, compile_target, reference_page

#: How each serializer writes an element's index: the marker its description
#: must teach, and the one every indexed element carries in its output. A new
#: serializer adds its row (``test_every_registered_serializer_has_a_marker``).
INDEX_MARKER: dict[str, str] = {
    "text": "[{index}]",
    "html": 'data-idx="{index}"',
}

#: The text serializer's other markers, as they appear in its output.
TEXT_MARKERS = (
    "*[",
    "|scroll element|",
    "|SHADOW(open)|",
    "|IFRAME|",
    "<!-- SVG content collapsed -->",
    " pages below)",
)


def _markers_page() -> TargetCapture:
    """A page with every marker the text serializer writes that the reference page lacks.

    A scrollable list (indexed by the cleaner, shown without an index by
    ``text``), a scrollable pane, an interactive shadow host, an icon button,
    a stand-alone SVG and a paragraph longer than the text cut.
    """
    body = [
        El(
            "div",
            30,
            bounds=(0, 0, 300, 100),
            attrs={"id": "list", "role": "listbox"},
            styles={"overflow": "auto", "overflow-y": "auto"},
            client=(0, 0, 300, 100),
            scroll=(0, 0, 300, 400),
            children=[
                El(
                    "a",
                    31,
                    bounds=(0, 0, 100, 20),
                    attrs={"id": "first", "href": "/one"},
                    children=[El("#text", 32, bounds=(0, 0, 100, 20), text="First result")],
                )
            ],
        ),
        El(
            "button",
            40,
            bounds=(0, 240, 100, 30),
            attrs={"id": "host-btn"},
            shadow=[
                El(
                    "span",
                    42,
                    bounds=(0, 240, 100, 30),
                    children=[El("#text", 43, bounds=(0, 240, 50, 20), text="Shadow label")],
                )
            ],
            shadow_root_id=41,
        ),
        El(
            "svg",
            55,
            bounds=(100, 300, 40, 40),
            attrs={"role": "img"},
            children=[El("path", 56, bounds=(100, 300, 40, 40))],
        ),
        El(
            "p",
            60,
            bounds=(0, 360, 600, 20),
            children=[El("#text", 61, bounds=(0, 360, 600, 20), text="x" * 300)],
        ),
    ]
    return compile_target(Doc("MAIN", html_id=1, body=body), session_id="tab")


async def _states() -> list[DOMState]:
    """Real cleaned states: BrowserUse on synthetic pages, Skyvern on a real capture.

    The markers page is cleaned twice, the second time against a previous
    step that lacked the shadow host, so ``*`` appears.
    """
    first = await clean(_markers_page())
    previous = {key: node for key, node in first.selector_map.items() if key != 40}
    return [
        await clean(reference_page()),
        await clean(_markers_page(), previous=previous),
        await SkyvernCleaner().clean(load_capture("skyvern_sticky"), (1280, 720)),
    ]


def _serializers() -> dict[str, DOMSerializerProtocol]:
    return {key: cls() for key, cls in DOMSerializerRegistrar.get_all().items()}


def test_every_registered_serializer_has_a_marker() -> None:
    assert set(INDEX_MARKER) == set(DOMSerializerRegistrar.keys())


@pytest.mark.parametrize("key", sorted(INDEX_MARKER))
async def test_the_description_names_the_marker_every_indexed_element_carries(key: str) -> None:
    serializer = _serializers()[key]
    marker = INDEX_MARKER[key]
    assert marker.format(index=42) in serializer.describe_format()
    for state in await _states():
        text = serializer.serialize(state)
        assert state.selector_map
        if key == "text":
            # The one documented exception: the text serializer numbers only
            # interactive elements, so an indexed scroll container that is not
            # one is shown as ``|scroll element|`` (the description says so).
            shown = [k for k, node in state.selector_map.items() if node.is_interactive]
        else:
            shown = list(state.selector_map)
        for index in shown:
            assert marker.format(index=index) in text, f"{key}: element {index} unmarked"


@pytest.mark.parametrize("key", sorted(INDEX_MARKER))
def test_no_description_teaches_another_serializer_s_marker(key: str) -> None:
    description = _serializers()[key].describe_format()
    for other, marker in INDEX_MARKER.items():
        if other != key:
            assert marker.format(index=42) not in description, other


async def test_every_marker_the_text_serializer_writes_is_described() -> None:
    serializer = TextSerializer()
    description = serializer.describe_format()
    written = "\n".join(serializer.serialize(state) for state in await _states())
    for marker in TEXT_MARKERS:
        assert marker in written, f"the fixtures no longer exercise {marker!r}"
        assert marker.strip(" )") in description, f"{marker!r} is written but not described"
    # A cut text line ends in "..." after the configured length.
    assert re.search(r"^x{200}\.\.\.$", written, re.MULTILINE)
    assert "200 characters" in description


async def test_the_html_description_claims_no_marker_html_never_writes() -> None:
    """``html`` marks neither new nor scrollable elements, shadow hosts nor frames."""
    serializer = HTMLSerializer()
    description = serializer.describe_format()
    written = "\n".join(serializer.serialize(state) for state in await _states())
    for marker in TEXT_MARKERS:
        assert marker not in written
        assert marker.strip(" )") not in description


def test_the_description_follows_the_instance_s_knobs() -> None:
    assert "80 characters" in TextSerializer(max_text_length=80).describe_format()
    assert "60 characters" in HTMLSerializer(max_href_length=60).describe_format()


def test_the_browser_describes_browser_dom_in_its_serializer_s_words() -> None:
    capability = BrowserCapability()
    capability.dom_serializer = HTMLSerializer(max_href_length=60)
    assert capability.fragment_formats() == {
        "browser.dom": HTMLSerializer(max_href_length=60).describe_format()
    }


def test_a_browser_without_the_dom_resolver_describes_nothing() -> None:
    assert BrowserCapability(profile="vision_only").fragment_formats() == {}

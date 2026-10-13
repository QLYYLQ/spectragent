"""The BrowserUse cleaner withholds sensitive values on the observation path (§4.12).

A control the shared rule calls sensitive (``dom/sensitivity.py``) puts no
value into the tree, so neither the prompt (``serialized_dom``) nor the
``browser.dom`` fragment the trace records carries one: the markup ``value``,
the text below the control (what was typed, a textarea's content, a select's
options) and the accessible name of a node inside it become ``<redacted>``.
The positions and the placeholder are the archive's raw-DOM redaction's, so
an offline re-clean of the recorded capture gives the live state. The live
counterpart is ``test_browser_archive_live.py::test_privacy_guard_on_the_dom_path``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from spectragent.core.archive.privacy import REDACTED
from spectragent.core.capabilities.browser.archive.dom_raw import capture_from_document
from spectragent.core.capabilities.browser.archive.redaction import redact_raw_capture
from spectragent.core.capabilities.browser.dom.cleaning.browser_use import BrowserUseCleaner
from spectragent.core.capabilities.browser.dom.models import DOMState, TargetCapture
from spectragent.core.capabilities.browser.dom.serializers.text_serializer import TextSerializer
from spectragent.core.capabilities.browser.observations import DOMObs
from spectragent.core.trace.encoding import encode_fragment
from tests._helpers.dom_payloads import Doc, El, compile_target

SECRETS = {
    "markup password": "markup-pw-Qz1",
    "typed password": "typed-pw-Kd2",
    "one-time code": "otp-text-Lm3",
    "card option value": "card-option-Vb4",
    "card option text": "card-option-text-Rt5",
    "hidden input": "hidden-csrf-Wy6",
    "inner accessible name": "typed-ax-name-Np7",
}
#: A plain text field's markup value: the agent's to see.
PLAIN_VALUE = "alice-shoes"


def _page() -> TargetCapture:
    body = [
        El(
            "input",
            10,
            bounds=(10, 10, 200, 30),
            attrs={"id": "user", "type": "text", "value": PLAIN_VALUE},
        ),
        El(
            "input",
            11,
            bounds=(10, 50, 200, 30),
            attrs={"id": "pw", "type": "password", "value": SECRETS["markup password"]},
            # What Chrome's user-agent shadow root holds: the typed value.
            shadow=[
                El(
                    "div",
                    13,
                    bounds=(12, 52, 196, 26),
                    children=[
                        El("#text", 14, bounds=(12, 52, 120, 20), text=SECRETS["typed password"])
                    ],
                )
            ],
            shadow_root_id=12,
        ),
        El(
            "textarea",
            15,
            bounds=(10, 90, 200, 60),
            attrs={"id": "otp-code"},
            children=[El("#text", 16, bounds=(12, 92, 120, 20), text=SECRETS["one-time code"])],
        ),
        El(
            "select",
            17,
            bounds=(10, 160, 200, 30),
            attrs={"id": "exp", "autocomplete": "cc-exp-month"},
            children=[
                El(
                    "option",
                    18,
                    bounds=(10, 160, 200, 30),
                    attrs={"value": SECRETS["card option value"]},
                    children=[
                        El("#text", 19, bounds=(12, 162, 150, 20), text=SECRETS["card option text"])
                    ],
                )
            ],
        ),
        El("input", 20, attrs={"type": "hidden", "name": "csrf", "value": SECRETS["hidden input"]}),
        El(
            "input",
            21,
            bounds=(10, 200, 200, 30),
            attrs={"id": "empty-pw", "type": "password", "value": ""},
        ),
        El(
            "button",
            22,
            bounds=(10, 240, 80, 30),
            attrs={"id": "go"},
            children=[El("#text", 23, bounds=(12, 242, 60, 20), text="Go now")],
        ),
    ]
    ax_nodes: list[dict[str, Any]] = [
        {"backendDOMNodeId": 10, "role": {"value": "textbox"}, "name": {"value": "User"}},
        {"backendDOMNodeId": 11, "role": {"value": "textbox"}, "name": {"value": "Password"}},
        {
            "backendDOMNodeId": 13,
            "role": {"value": "generic"},
            "name": {"value": SECRETS["inner accessible name"]},
        },
    ]
    return compile_target(Doc("MAIN", html_id=1, body=body), session_id="tab", ax_nodes=ax_nodes)


async def _clean(capture: TargetCapture) -> DOMState:
    return await BrowserUseCleaner().clean(capture, (1280, 720))


def _seen(state: DOMState) -> tuple[str, str]:
    """What the prompt shows, and the ``browser.dom`` fragment as the trace writes it."""
    prompt = TextSerializer().serialize(state)
    encoded = encode_fragment(0, DOMObs(element_tree=state, selector_map=dict(state.selector_map)))
    assert encoded is not None
    return prompt, encoded.file.data.decode("utf-8")


def _line(prompt: str, element_id: str) -> str:
    (line,) = [line for line in prompt.splitlines() if f"id={element_id}" in line]
    return line


@pytest.fixture
async def seen() -> tuple[str, str]:
    return _seen(await _clean(_page()))


@pytest.mark.parametrize("label", sorted(SECRETS))
async def test_no_sensitive_value_reaches_the_prompt_or_the_trace(
    seen: tuple[str, str], label: str
) -> None:
    prompt, trace = seen
    assert SECRETS[label] not in prompt
    assert SECRETS[label] not in trace


async def test_what_is_not_secret_is_kept(seen: tuple[str, str]) -> None:
    prompt, trace = seen
    assert f"value={PLAIN_VALUE}" in _line(prompt, "user")
    assert PLAIN_VALUE in trace
    # The control's own accessible name is its label, not its value.
    assert '"Password"' in trace
    assert "Go now" in prompt


async def test_a_value_the_page_wrote_shows_as_withheld(seen: tuple[str, str]) -> None:
    prompt, _ = seen
    assert f"value={REDACTED}" in _line(prompt, "pw")


async def test_an_empty_value_stays_empty(seen: tuple[str, str]) -> None:
    prompt, trace = seen
    assert REDACTED not in _line(prompt, "empty-pw")
    tree = json.loads(trace)["element_tree"]["dom_tree"]
    stack = [tree]
    found = None
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("attributes", {}).get("id") == "empty-pw":
                found = node
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)
    assert found is not None and found["attributes"]["value"] == ""


async def test_the_capture_itself_is_not_changed() -> None:
    """The archive reads the same capture: the cleaner withholds on the nodes it builds."""
    capture = _page()
    before = copy.deepcopy(capture.raw_dom), copy.deepcopy(capture.ax_tree)
    await _clean(capture)
    assert (capture.raw_dom, capture.ax_tree) == before
    assert SECRETS["typed password"] in json.dumps(capture.raw_dom)


async def test_an_offline_re_clean_of_the_archived_capture_matches_the_live_clean() -> None:
    capture = _page()
    archived, entries = redact_raw_capture(capture)
    assert entries, "the archive withheld something, so the comparison means something"
    live_prompt, live_trace = _seen(await _clean(capture))
    offline_prompt, offline_trace = _seen(await _clean(capture_from_document(archived)))
    assert offline_prompt == live_prompt

    def recorded(trace: str) -> dict[str, Any]:
        data = json.loads(trace)
        del data["captured_at"]  # when the fragment was made, not what it holds
        return data

    assert recorded(offline_trace) == recorded(live_trace)


async def test_frames_and_their_documents_are_covered_too() -> None:
    """An out-of-process frame is built from its own capture, with the same rule."""
    frame = compile_target(
        Doc(
            "CROSS",
            html_id=1,
            viewport=(320, 200),
            body=[
                El(
                    "input",
                    10,
                    bounds=(5, 5, 100, 20),
                    attrs={
                        "id": "frame-pw",
                        "type": "password",
                        "value": SECRETS["markup password"],
                    },
                )
            ],
        ),
        session_id="cross",
        target_id="CROSS",
        namespace=1,
    )
    page = compile_target(
        Doc(
            "MAIN",
            html_id=1,
            body=[El("iframe", 30, bounds=(0, 0, 320, 200), oopif_frame_id="CROSS")],
        ),
        session_id="tab",
        frames={"CROSS": frame},
    )
    prompt, trace = _seen(await _clean(page))
    assert "frame-pw" in prompt, "the frame's field was built"
    assert SECRETS["markup password"] not in prompt
    assert SECRETS["markup password"] not in trace

"""Committed DOM captures: a real Chrome capture, replayed through a cleaner offline.

A :class:`TargetCapture` is what a cleaner's ``capture`` hands its ``clean``.
Saving one from a real page lets a test run a cleaner on exactly the input
Chrome produced, with no browser — which is how the ``skyvern`` cleaner's
output is pinned byte for byte (``test_browser_skyvern_hit_test.py``).

JSON cannot key an object by ``int``; the Skyvern capture keys
``skyvern_nodes`` (and the hit-test results) by the page script's integer
ref, and :func:`capture_from_json` restores those keys — the maps the
archive's own reader restores (``skyvern_capture.INTEGER_KEYED_MAPS``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spectragent.core.capabilities.browser.dom.js.skyvern_capture import INTEGER_KEYED_MAPS
from spectragent.core.capabilities.browser.dom.models import TargetCapture

#: Directory of the committed captures and their goldens.
CAPTURES_DIR = Path(__file__).resolve().parent.parent / "test_core" / "test_capabilities" / "data"


def capture_to_json(capture: TargetCapture) -> dict[str, Any]:
    """*capture* (and its frame captures) as a JSON-safe dict."""
    return {
        "raw_dom": capture.raw_dom,
        "snapshot": capture.snapshot,
        "ax_tree": capture.ax_tree,
        "session_id": capture.session_id,
        "target_id": capture.target_id,
        "namespace": capture.namespace,
        "js_click_listener_ids": (
            None if capture.js_click_listener_ids is None else sorted(capture.js_click_listener_ids)
        ),
        "frames": {key: capture_to_json(child) for key, child in capture.frames.items()},
        "loader_id": capture.loader_id,
        "url": capture.url,
    }


def capture_from_json(data: dict[str, Any]) -> TargetCapture:
    """The inverse of :func:`capture_to_json`."""
    raw_dom = dict(data["raw_dom"])
    for key in INTEGER_KEYED_MAPS:
        if isinstance(raw_dom.get(key), dict):
            raw_dom[key] = {int(ref): value for ref, value in raw_dom[key].items()}
    listeners = data.get("js_click_listener_ids")
    return TargetCapture(
        raw_dom=raw_dom,
        snapshot=data["snapshot"],
        ax_tree=data["ax_tree"],
        session_id=data.get("session_id"),
        target_id=data.get("target_id"),
        namespace=data.get("namespace", 0),
        js_click_listener_ids=None if listeners is None else set(listeners),
        frames={key: capture_from_json(child) for key, child in data.get("frames", {}).items()},
        loader_id=data.get("loader_id"),
        url=data.get("url"),
    )


def load_capture(name: str) -> TargetCapture:
    """The committed capture ``data/<name>.capture.json``."""
    path = CAPTURES_DIR / f"{name}.capture.json"
    return capture_from_json(json.loads(path.read_text(encoding="utf-8")))

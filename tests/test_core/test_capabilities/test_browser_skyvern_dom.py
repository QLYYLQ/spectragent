"""Skyvern capture must be real before it can produce a DOM observation."""

from __future__ import annotations

import asyncio
from time import monotonic

import pytest

from spectragent.core.capabilities.browser.dom.capture import FrameTarget
from spectragent.core.capabilities.browser.dom.cleaning import DOMCaptureSource
from spectragent.core.capabilities.browser.dom.cleaning.skyvern import SkyvernCleaner
from spectragent.core.capabilities.browser.dom.js import skyvern_capture
from spectragent.core.capabilities.browser.dom.models import TargetCapture


async def test_missing_js_tree_is_an_error_instead_of_snapshot_fallback() -> None:
    cleaner = SkyvernCleaner()
    capture = TargetCapture(raw_dom={}, snapshot={"documents": []}, ax_tree={})
    with pytest.raises(RuntimeError, match="no JS element tree"):
        await cleaner.clean(capture, (1280, 720))


async def test_missing_frame_tree_never_evaluates_in_page_main_world() -> None:
    class CDP:
        async def send(self, method, _params, *, session_id=None):
            assert method == "Page.getFrameTree"
            return {}

    async def no_js(_method, _params, _session_id):
        raise AssertionError("JS must not run without a known isolated world")

    source = DOMCaptureSource(
        cdp=CDP(),  # type: ignore[arg-type]
        session_id="tab",
        frames=None,
        run_js=no_js,
    )
    with pytest.raises(RuntimeError, match="no root frame"):
        await skyvern_capture.capture_skyvern(source)


async def test_slow_frame_discovery_spends_only_the_frame_deadline(monkeypatch) -> None:
    class Frames:
        async def children(self, _session_id):
            await asyncio.Event().wait()

    class CDP:
        async def send(self, method, _params, *, session_id=None):
            assert method == "Page.getFrameTree" and session_id == "tab"
            return {"frameTree": {"frame": {"id": "root"}}}

    async def fake_frame(_source, _session_id, _entry, *, oop_by_id, depth, deadline):
        return TargetCapture(raw_dom={"skyvern_tree": {"tagName": "html"}}, snapshot={}, ax_tree={})

    monkeypatch.setattr(skyvern_capture, "_capture_frame_entry", fake_frame)
    source = DOMCaptureSource(
        cdp=CDP(),  # type: ignore[arg-type]
        session_id="tab",
        frames=Frames(),  # type: ignore[arg-type]
        run_js=lambda *_args: asyncio.sleep(0),
        frame_timeout_s=0.02,
    )
    started = monotonic()
    await skyvern_capture.capture_skyvern(source)
    assert monotonic() - started < 0.3


async def test_slow_frame_owner_lookup_spends_only_the_frame_deadline(monkeypatch) -> None:
    class Frames:
        async def children(self, _session_id):
            return [FrameTarget(target_id="child", session_id="child-sid", namespace=1)]

    class CDP:
        async def send(self, method, _params, *, session_id=None):
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "root"}}}
            assert method == "DOM.getFrameOwner" and session_id == "tab"
            await asyncio.Event().wait()

    async def fake_frame(_source, _session_id, _entry, *, oop_by_id, depth, deadline):
        return TargetCapture(raw_dom={"skyvern_tree": {"tagName": "html"}}, snapshot={}, ax_tree={})

    async def fake_oop(_source, frame, *, depth, deadline):
        return TargetCapture(
            raw_dom={"skyvern_tree": {"tagName": "html"}},
            snapshot={},
            ax_tree={},
            session_id=frame.session_id,
        )

    monkeypatch.setattr(skyvern_capture, "_capture_frame_entry", fake_frame)
    monkeypatch.setattr(skyvern_capture, "_capture_oop", fake_oop)
    source = DOMCaptureSource(
        cdp=CDP(),  # type: ignore[arg-type]
        session_id="tab",
        frames=Frames(),  # type: ignore[arg-type]
        run_js=lambda *_args: asyncio.sleep(0),
        frame_timeout_s=0.02,
    )
    started = monotonic()
    result = await skyvern_capture.capture_skyvern(source)
    assert "child" in result.frames
    assert monotonic() - started < 0.3

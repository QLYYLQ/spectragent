"""Watchdog wiring + teardown: no dead flags, no leaked CDP callbacks.

Covers two audit findings:

- §6.6.5 dead code. ``DOMWatchdog.enable_js_listener_detection`` defaulted
  False and no construction site ever passed it, so ``has_js_click_listener``
  — check #1 in ``dom/filters/interactivity.is_interactive`` — was
  permanently False and ``dom/js/event_listeners.py`` was unreachable. It is
  now driven by the profile. ``DownloadsWatchdog`` had the same shape with no
  consumer at all (nothing connected to its three events,
  ``Browser.setDownloadBehavior`` was never sent) and was deleted.

- §6.6.6 leaked ``CDPEventBridge``. PopupsWatchdog / CrashWatchdog open their
  own bridges, which live outside ``BrowserCapability._bridges``. ``close_tab``
  and ``teardown`` only closed the page-lifecycle bridges, so every closed tab
  left a callback bound to a closed EventScope — and ``EventScope.emit`` raises
  RuntimeError on a closed scope, swallowed by CDPClient's dispatch loop.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_cdp import EventScope, ScopeGroup

# Concrete DOM strategies register on import. Production gets these via the
# composer's recursive ``discover()`` pass (composer.py:177-188); these tests
# build watchdogs directly, so import them here.
import spectragent.core.capabilities.browser.dom.cleaning.skyvern  # noqa: F401
import spectragent.core.capabilities.browser.dom.serializers.html_serializer  # noqa: F401
import spectragent.core.capabilities.browser.dom.serializers.text_serializer  # noqa: F401
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.profiles import (
    BUILTIN_PROFILES,
    resolve_profile,
)
from spectragent.core.capabilities.browser.watchdogs import browser_watchdog_classes


class _CDP:
    """CDP stub that tracks event-callback registrations per method."""

    def __init__(self) -> None:
        self.is_connected = True
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self.event_callbacks: dict[str, list[Any]] = {}

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        self.sent.append((method, dict(params or {}), session_id))
        if method == "Target.attachToTarget":
            return {"sessionId": "cdp-sid-1"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "f1"}}}
        if method == "Target.getTargets":
            return {"targetInfos": []}
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        self.event_callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Any) -> None:
        cbs = self.event_callbacks.get(method, [])
        if callback in cbs:
            cbs.remove(callback)

    def live_callbacks(self) -> dict[str, int]:
        return {m: len(c) for m, c in self.event_callbacks.items() if c}


def _make_cap(profile: str = "standard") -> tuple[BrowserCapability, _CDP]:
    cap = BrowserCapability(profile=profile)
    cdp = _CDP()
    cap._inject_cdp_client(cdp)  # type: ignore[arg-type]
    cap._cdp = cdp  # type: ignore[assignment]
    cap._input_platform = "macos"  # lifecycle fixture begins after platform measurement
    cap._scope_group = ScopeGroup("test")  # type: ignore[assignment]
    cap._resolved_profile = resolve_profile(profile)
    return cap, cdp


# ─── §6.6.5 dead code ─────────────────────────────────────────────────


class TestJsListenerDetectionIsReachable:
    def test_standard_profile_enables_detection(self) -> None:
        assert resolve_profile("standard").js_click_listener_detection is True

    def test_dom_watchdog_receives_the_profile_flag(self) -> None:
        """The factory must pass it — it never did, so it was always False."""
        cap, _ = _make_cap("standard")
        watchdogs = cap._create_watchdogs_for_scope(level="tab", cdp_session_id="sid-1")
        dom = next(wd for wd in watchdogs if type(wd).__name__ == "DOMWatchdog")
        assert dom._enable_js_listeners is True

    def test_cheap_profiles_opt_out(self) -> None:
        # skyvern's cleaner ignores js_click_listener_ids entirely; the
        # lightweight profile exists to cut CDP work.
        for name in ("skyvern", "lightweight"):
            cap, _ = _make_cap(name)
            watchdogs = cap._create_watchdogs_for_scope(level="tab", cdp_session_id="sid-1")
            dom = [wd for wd in watchdogs if type(wd).__name__ == "DOMWatchdog"]
            if dom:
                assert dom[0]._enable_js_listeners is False

    async def test_detection_reaches_the_capture_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag on → detect_js_click_listeners runs and its ids are returned."""
        from spectragent.core.capabilities.browser.watchdogs.dom_watchdog import (
            DOMWatchdog,
        )

        called: list[bool] = []

        async def _fake_detect(_send: Any, session_id: str | None = None) -> set[int]:
            called.append(True)
            return {42}

        monkeypatch.setattr(
            "spectragent.core.capabilities.browser.dom.js.event_listeners"
            ".detect_js_click_listeners",
            _fake_detect,
        )

        wd = DOMWatchdog(_CDP(), enable_js_listener_detection=True)  # type: ignore[arg-type]
        capture, _dpr = await wd._capture_cdp_data()

        assert called == [True]
        assert capture.js_click_listener_ids == {42}

    def test_cross_site_iframe_capture_follows_the_profile(self) -> None:
        """DOM strategies with frame support retain frame-target discovery."""
        expected = {"standard": True, "skyvern": True, "lightweight": False}
        for name, enabled in expected.items():
            assert resolve_profile(name).cross_site_iframe_capture is enabled
            cap, _ = _make_cap(name)
            watchdogs = cap._create_watchdogs_for_scope(level="tab", cdp_session_id="sid-1")
            (dom,) = [wd for wd in watchdogs if type(wd).__name__ == "DOMWatchdog"]
            assert (dom._frames is not None) is enabled, name

    async def test_detection_off_skips_the_extra_round_trip(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs.dom_watchdog import (
            DOMWatchdog,
        )

        wd = DOMWatchdog(_CDP(), enable_js_listener_detection=False)  # type: ignore[arg-type]
        capture, _dpr = await wd._capture_cdp_data()
        assert capture.js_click_listener_ids is None


class TestDownloadsWatchdogIsGone:
    """No consumer ever existed — deleted rather than left half-wired."""

    def test_module_deleted(self) -> None:
        with pytest.raises(ImportError):
            __import__("spectragent.core.capabilities.browser.watchdogs.downloads_watchdog")

    def test_events_deleted(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import events

        for name in (
            "DownloadStartedEvent",
            "DownloadProgressEvent",
            "FileDownloadedEvent",
        ):
            assert not hasattr(events, name)

    def test_no_profile_still_requests_it(self) -> None:
        """A dangling name is now a hard error, not a silent skip.

        Profiles list exclusions rather than inclusions, so the way a
        deleted watchdog could linger is in ``watchdogs_exclude``.
        ``validate_watchdog_names`` rejects it.
        """
        for profile in BUILTIN_PROFILES.values():
            assert "downloads" not in profile.watchdogs_exclude
        assert "downloads" not in browser_watchdog_classes()


# ─── §6.6.6 leaked CDPEventBridge ─────────────────────────────────────


class TestWatchdogBridgesAreClosed:
    async def test_close_tab_releases_watchdog_callbacks(self) -> None:
        cap, cdp = _make_cap("standard")
        sid = await cap._attach_tab("T1")
        cap._default_session_id = "another-tab"

        # The tab's PopupsWatchdog registered its own CDP callback.
        assert cdp.live_callbacks().get("Page.javascriptDialogOpening") == 1

        await cap.close_tab(sid)

        # Nothing may still point at the now-closed scope: emit() on a
        # closed EventScope raises RuntimeError.
        assert cdp.live_callbacks() == {}

    async def test_teardown_releases_watchdog_callbacks(self) -> None:
        cap, cdp = _make_cap("standard")
        cap._global_scope = await cap._scope_group.create_scope("__browser__")  # type: ignore[assignment]
        cap._attach_browser_level_watchdogs(cap._global_scope)  # type: ignore[arg-type]
        await cap._attach_tab("T1")
        cap._default_session_id = "cdp-sid-1"

        # Browser-level (crash) + tab-level (popups) bridges are both live.
        assert cdp.live_callbacks().get("Inspector.targetCrashed") == 1
        assert cdp.live_callbacks().get("Page.javascriptDialogOpening") == 1

        await cap.teardown()

        assert cdp.live_callbacks() == {}

    async def test_detach_is_idempotent(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs.popups_watchdog import (
            PopupsWatchdog,
        )

        cdp = _CDP()
        wd = PopupsWatchdog(cdp)  # type: ignore[arg-type]
        scope = EventScope("test")
        wd.attach(scope, session_id="s1")  # type: ignore[arg-type]
        wd.detach(scope, "s1")
        wd.detach(scope, "s1")  # must not raise
        assert cdp.live_callbacks() == {}

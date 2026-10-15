"""ActionWatchdog._on_navigate — the commit wait, then the lifecycle wait.

Validates that ``_on_navigate``:
  - registers a one-shot waiter on the right event class for each
    ``wait_until`` value
  - filters by main_frame_id when populated
  - filters by loader_id once Page.navigate returns
  - emits NavigationCompleteEvent on lifecycle hit AND on timeout, and on a
    lifecycle timeout only logs a WARNING: the page committed, so the
    navigation succeeds (``docs/architecture_design.md`` §4.14,
    "``browser.navigate``")
  - fails a navigation that never commits (no reply within the commit
    bound), one the browser refuses, and one the browser reports failed
    (``errorText``), answering ``{"error": ...}`` without a lifecycle wait
  - does not wait for a same-document navigation (no ``loaderId``)
  - is always answered before its caller gives up: the event's own timeout
    is derived from both waits, which the old equal 30 s clocks never were.

The real-Chrome half is ``test_browser_navigate_live.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from agent_cdp import ScopeGroup

from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
    ActionWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    NAVIGATION_EVENT_MARGIN_S,
    BrowserErrorEvent,
    DOMContentLoadedEvent,
    LifecycleEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    PageLoadEvent,
)

_WATCHDOG_LOG = "spectragent.core.capabilities.browser.watchdogs.action_watchdog"


class _FakeConnection:
    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


class _FakeScope:
    """Records emit() calls and connect() registrations."""

    def __init__(self) -> None:
        self.emitted: list[Any] = []
        self.handlers: list[tuple[type, Any]] = []
        self._conns: list[_FakeConnection] = []
        self.scope_id = "navigation-fixture"
        self.close_callbacks: list[Any] = []

    def add_close_callback(self, callback: Any) -> None:
        self.close_callbacks.append(callback)

    def remove_close_callback(self, callback: Any) -> None:
        if callback in self.close_callbacks:
            self.close_callbacks.remove(callback)

    def emit(self, event: Any) -> None:
        self.emitted.append(event)

    def connect(self, event_type: type, handler: Any, **_: Any) -> _FakeConnection:
        self.handlers.append((event_type, handler))
        conn = _FakeConnection()
        self._conns.append(conn)
        return conn

    def fire(self, event: Any) -> None:
        """Test helper — invoke every handler registered for the event type."""
        for et, h in self.handlers:
            if isinstance(event, et):
                h(event)


class _FakeCDP:
    """Answers ``Page.navigate`` the way Chrome does in each case measured on Chrome 155.

    By default with the committed document's loader id. ``error_text`` adds
    the ``errorText`` Chrome sends when the navigation failed (an unreachable
    host, a 204, a download); ``hang`` never answers (a server that never
    responds); ``refuse`` fails the command (a URL Chrome will not navigate to).
    """

    def __init__(
        self,
        loader_id: str | None = "L1",
        *,
        error_text: str | None = None,
        hang: bool = False,
        refuse: str | None = None,
    ) -> None:
        self._loader_id = loader_id
        self._error_text = error_text
        self._hang = hang
        self._refuse = refuse
        self.sent: list[tuple[str, dict, str | None]] = []

    async def send(
        self, method: str, params: dict | None = None, session_id: str | None = None
    ) -> Any:
        self.sent.append((method, params or {}, session_id))
        if method == "Page.navigate":
            if self._refuse is not None:
                raise RuntimeError(self._refuse)
            if self._hang:
                await asyncio.Event().wait()
            reply: dict[str, Any] = {"loaderId": self._loader_id} if self._loader_id else {}
            if self._error_text is not None:
                reply["errorText"] = self._error_text
            return reply
        return {}


def _watchdog(cdp: _FakeCDP) -> tuple[ActionWatchdog, _FakeScope]:
    scope = _FakeScope()
    wd = ActionWatchdog(
        cdp,  # type: ignore[arg-type]
        input_platform="linux",
        main_frame_id_getter=lambda: "MAIN",
    )
    wd._scope = scope  # type: ignore[assignment]
    wd._session_id = "sid"
    return wd, scope


class TestOnNavigateLifecycleWiring:
    async def test_load_waits_for_pageloadevent(self) -> None:
        scope = _FakeScope()
        cdp = _FakeCDP()
        wd = ActionWatchdog(
            cdp,  # type: ignore[arg-type]
            input_platform="linux",
            main_frame_id_getter=lambda: "MAIN",
        )
        wd._scope = scope  # type: ignore[assignment]
        wd._session_id = "sid"

        async def _fire_after_navigate() -> None:
            # Wait until Page.navigate has been recorded, then fire the
            # matching PageLoadEvent through the registered handler.
            for _ in range(50):
                if any(m == "Page.navigate" for m, _p, _s in cdp.sent):
                    break
                await asyncio.sleep(0.005)
            scope.fire(
                PageLoadEvent(loader_id="L1", frame_id="MAIN"),
            )

        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                wd._on_navigate(
                    NavigateToUrlEvent(
                        url="https://example.com",
                        wait_until="load",
                        timeout_ms=2000,
                    ),
                )
            )
            tg.create_task(_fire_after_navigate())

        # Started + Completed both emitted.
        types = [type(e).__name__ for e in scope.emitted]
        assert "NavigationStartedEvent" in types
        assert "NavigationCompleteEvent" in types
        # Connection was disconnected after wait completed.
        assert all(c.disconnected for c in scope._conns)

    async def test_subframe_loader_mismatch_keeps_waiting_then_times_out(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Sub-frame loadEventFired (different loader_id) is filtered out.

        We wire a 200ms timeout, fire a stale event during the wait, and
        confirm the watchdog times out (logging a warning) rather than
        treating the stale event as completion — and that the timeout is
        no failure: the page committed, so the navigation succeeds.
        """
        scope = _FakeScope()
        cdp = _FakeCDP(loader_id="L1")
        wd = ActionWatchdog(
            cdp,  # type: ignore[arg-type]
            input_platform="linux",
            main_frame_id_getter=lambda: "MAIN",
        )
        wd._scope = scope  # type: ignore[assignment]
        wd._session_id = "sid"

        async def _fire_stale() -> None:
            # Wait for navigate to register, then fire a sub-frame event.
            for _ in range(50):
                if any(m == "Page.navigate" for m, _p, _s in cdp.sent):
                    break
                await asyncio.sleep(0.005)
            scope.fire(
                PageLoadEvent(loader_id="L_OTHER", frame_id="OTHER_FRAME"),
            )

        with caplog.at_level(logging.WARNING, logger=_WATCHDOG_LOG):
            async with asyncio.TaskGroup() as tg:
                navigation = tg.create_task(
                    wd._on_navigate(
                        NavigateToUrlEvent(
                            url="https://example.com",
                            wait_until="load",
                            # timeout_ms=200 → wait_s=0.2
                            timeout_ms=200,
                        ),
                    )
                )
                tg.create_task(_fire_stale())

        # Watchdog timed out and STILL emitted NavigationCompleteEvent.
        # Loaders/frames don't match, so the future never resolves.
        types = [type(e).__name__ for e in scope.emitted]
        assert "NavigationCompleteEvent" in types
        assert navigation.result() is None, "a committed page whose load is late is no failure"
        (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning.getMessage() == (
            "navigate: https://example.com committed, but its 'load' lifecycle event did not "
            "arrive within 0.2s; carrying on with the page as it is"
        )

    async def test_no_wait_until_skips_lifecycle_wait(self) -> None:
        """Empty/unknown wait_until → no waiter, no lifecycle hookup."""
        scope = _FakeScope()
        cdp = _FakeCDP()
        wd = ActionWatchdog(cdp, input_platform="linux")  # type: ignore[arg-type]
        wd._scope = scope  # type: ignore[assignment]
        wd._session_id = "sid"

        await wd._on_navigate(
            NavigateToUrlEvent(
                url="https://example.com",
                wait_until="",
            ),
        )
        # No connect() was called.
        assert scope.handlers == []
        # NavigationCompleteEvent emitted as soon as Page.navigate returned.
        assert any(isinstance(e, NavigationCompleteEvent) for e in scope.emitted)
        assert any(isinstance(e, NavigationStartedEvent) for e in scope.emitted)

    async def test_networkidle_accepts_almostidle_too(self) -> None:
        scope = _FakeScope()
        cdp = _FakeCDP()
        wd = ActionWatchdog(
            cdp,  # type: ignore[arg-type]
            input_platform="linux",
            main_frame_id_getter=lambda: "MAIN",
        )
        wd._scope = scope  # type: ignore[assignment]
        wd._session_id = "sid"

        async def _fire_almost_idle() -> None:
            for _ in range(50):
                if any(m == "Page.navigate" for m, _p, _s in cdp.sent):
                    break
                await asyncio.sleep(0.005)
            scope.fire(
                LifecycleEvent(
                    name="networkAlmostIdle",
                    frame_id="MAIN",
                    loader_id="L1",
                ),
            )

        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                wd._on_navigate(
                    NavigateToUrlEvent(
                        url="https://example.com",
                        wait_until="networkidle",
                        timeout_ms=2000,
                    ),
                )
            )
            tg.create_task(_fire_almost_idle())

        types = [type(e).__name__ for e in scope.emitted]
        assert "NavigationCompleteEvent" in types

    async def test_lifecycle_target_unknown_returns_none(self) -> None:
        wd = ActionWatchdog(
            _FakeCDP(),  # type: ignore[arg-type]
            input_platform="linux",
        )
        assert wd._lifecycle_target("nonsense") is None
        assert wd._lifecycle_target("") is None

    async def test_lifecycle_target_load(self) -> None:
        wd = ActionWatchdog(
            _FakeCDP(),  # type: ignore[arg-type]
            input_platform="linux",
        )
        target = wd._lifecycle_target("load")
        assert target is not None
        evt_type, predicate = target
        assert evt_type is PageLoadEvent
        assert predicate(object())  # base predicate is unconditional

    async def test_lifecycle_target_domcontentloaded(self) -> None:
        wd = ActionWatchdog(
            _FakeCDP(),  # type: ignore[arg-type]
            input_platform="linux",
        )
        target = wd._lifecycle_target("domcontentloaded")
        assert target is not None
        evt_type, _ = target
        assert evt_type is DOMContentLoadedEvent


class TestTheTwoBounds:
    def test_the_lifecycle_bound_falls_back_to_the_commit_bound_then_30_s(self) -> None:
        # lifecycle_timeout_ms wins.
        evt = NavigateToUrlEvent(
            url="x",
            wait_until="load",
            timeout_ms=10000,
            lifecycle_timeout_ms=2500,
        )
        assert (evt.commit_timeout_s, evt.lifecycle_timeout_s) == (10.0, 2.5)
        # Falls back to timeout_ms when lifecycle_timeout_ms is None.
        evt2 = NavigateToUrlEvent(
            url="x",
            wait_until="load",
            timeout_ms=7000,
        )
        assert (evt2.commit_timeout_s, evt2.lifecycle_timeout_s) == (7.0, 7.0)
        # Defaults to 30s when both are None.
        evt3 = NavigateToUrlEvent(url="x", wait_until="load", timeout_ms=None)
        assert (evt3.commit_timeout_s, evt3.lifecycle_timeout_s) == (30.0, 30.0)

    def test_the_caller_waits_longer_than_both_waits_together(self) -> None:
        """The event's own clock starts first, so it must outlast both waits.

        With the old equal 30 s clocks the caller always gave up first and
        the handler's warn-and-carry-on was unreachable.
        """
        assert NavigateToUrlEvent(url="x").event_timeout == 30 + 30 + NAVIGATION_EVENT_MARGIN_S
        assert (
            NavigateToUrlEvent(url="x", timeout_ms=30000, lifecycle_timeout_ms=8000).event_timeout
            == 30 + 8 + NAVIGATION_EVENT_MARGIN_S
        )
        assert NAVIGATION_EVENT_MARGIN_S > 0

    def test_an_emitter_s_own_timeout_is_kept(self) -> None:
        assert NavigateToUrlEvent(url="x", event_timeout=5.0).event_timeout == 5.0
        assert NavigateToUrlEvent(url="x", event_timeout=None).event_timeout is None


class TestNavigationOutcomes:
    """What the handler answers: ``None`` (success) or ``{"error": ...}``."""

    async def test_a_navigation_that_never_commits_fails(self) -> None:
        wd, scope = _watchdog(_FakeCDP(hang=True))
        started = time.monotonic()
        answer = await wd._on_navigate(
            NavigateToUrlEvent(url="https://slow.example/", timeout_ms=200)
        )
        message = "navigation to 'https://slow.example/' did not commit within 0.2 s"
        assert answer == {"error": message}
        assert time.monotonic() - started < 2.0
        (error,) = [e for e in scope.emitted if isinstance(e, BrowserErrorEvent)]
        assert error.message == message
        assert not any(isinstance(e, NavigationCompleteEvent) for e in scope.emitted)
        assert all(c.disconnected for c in scope._conns), "the lifecycle waiter is released"

    async def test_a_navigation_the_browser_refuses_fails(self) -> None:
        wd, _scope = _watchdog(_FakeCDP(refuse="CDP error: Cannot navigate to invalid URL"))
        answer = await wd._on_navigate(NavigateToUrlEvent(url="www.example.com"))
        assert answer == {
            "error": "navigation to 'www.example.com' was refused by the browser: "
            "CDP error: Cannot navigate to invalid URL"
        }

    @pytest.mark.parametrize("error_text", ["net::ERR_ABORTED", "net::ERR_NAME_NOT_RESOLVED"])
    async def test_a_navigation_the_browser_reports_failed_fails_at_once(
        self, error_text: str
    ) -> None:
        """A 204 or a download commits nothing: its lifecycle would be waited out for nothing."""
        wd, scope = _watchdog(_FakeCDP(error_text=error_text))
        started = time.monotonic()
        answer = await wd._on_navigate(
            NavigateToUrlEvent(url="https://a.example/x", lifecycle_timeout_ms=5000)
        )
        assert answer == {"error": f"navigation to 'https://a.example/x' failed: {error_text}"}
        assert time.monotonic() - started < 1.0, "no lifecycle wait"
        (complete,) = [e for e in scope.emitted if isinstance(e, NavigationCompleteEvent)]
        assert complete.error_text == error_text

    async def test_a_same_document_navigation_does_not_wait(self) -> None:
        """No ``loaderId`` in the reply: the document did not change, so nothing loads."""
        wd, scope = _watchdog(_FakeCDP(loader_id=None))
        started = time.monotonic()
        answer = await wd._on_navigate(
            NavigateToUrlEvent(url="https://a.example/#part", lifecycle_timeout_ms=5000)
        )
        assert answer is None
        assert time.monotonic() - started < 1.0
        assert any(isinstance(e, NavigationCompleteEvent) for e in scope.emitted)


async def test_through_a_real_scope_a_late_load_is_answered_before_the_caller_gives_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``await event`` returns the handler's answer: success, after the lifecycle bound.

    The lifecycle phase never arrives here, as on a page with a resource that
    never finishes. Through agent-cdp's own queue and per-handler bound, the
    event's derived timeout lets the handler finish its warn-and-carry-on.
    """
    group = ScopeGroup("test-navigate")
    scope = await group.create_scope("tab")
    watchdog = ActionWatchdog(
        _FakeCDP(),  # type: ignore[arg-type]
        input_platform="linux",
        main_frame_id_getter=lambda: "MAIN",
    )
    watchdog.attach(scope, session_id="sid")
    try:
        event = NavigateToUrlEvent(
            url="https://a.example/", timeout_ms=1000, lifecycle_timeout_ms=300
        )
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=_WATCHDOG_LOG):
            scope.emit(event)
            await event
        took = time.monotonic() - started
        (outcome,) = event.event_results.values()
        assert outcome.error is None and outcome.result is None
        assert 0.3 <= took < 1.0
        assert any("carrying on" in r.getMessage() for r in caplog.records)
    finally:
        await group.close_all()

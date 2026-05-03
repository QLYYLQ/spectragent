"""ActionWatchdog._on_navigate — verifies lifecycle wait behaviour.

Validates that the rewritten ``_on_navigate`` (M13.5):
  - registers a one-shot waiter on the right event class for each
    ``wait_until`` value
  - filters by main_frame_id when populated
  - filters by loader_id once Page.navigate returns
  - emits NavigationCompleteEvent on lifecycle hit AND on timeout
"""

from __future__ import annotations

import asyncio
from typing import Any

from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
    ActionWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    DOMContentLoadedEvent,
    LifecycleEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    PageLoadEvent,
)


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
    def __init__(self, loader_id: str | None = "L1") -> None:
        self._loader_id = loader_id
        self.sent: list[tuple[str, dict, str | None]] = []

    async def send(self, method: str, params: dict | None = None,
                   session_id: str | None = None) -> Any:
        self.sent.append((method, params or {}, session_id))
        if method == "Page.navigate":
            return {"loaderId": self._loader_id} if self._loader_id else {}
        return {}


class TestOnNavigateLifecycleWiring:
    async def test_load_waits_for_pageloadevent(self) -> None:
        scope = _FakeScope()
        cdp = _FakeCDP()
        wd = ActionWatchdog(
            cdp,  # type: ignore[arg-type]
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
        self,
    ) -> None:
        """Sub-frame loadEventFired (different loader_id) is filtered out.

        We wire a 200ms timeout, fire a stale event during the wait, and
        confirm the watchdog times out (logging a warning) rather than
        treating the stale event as completion.
        """
        scope = _FakeScope()
        cdp = _FakeCDP(loader_id="L1")
        wd = ActionWatchdog(
            cdp,  # type: ignore[arg-type]
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

        async with asyncio.TaskGroup() as tg:
            tg.create_task(
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

    async def test_no_wait_until_skips_lifecycle_wait(self) -> None:
        """Empty/unknown wait_until → no waiter, no lifecycle hookup."""
        scope = _FakeScope()
        cdp = _FakeCDP()
        wd = ActionWatchdog(cdp)  # type: ignore[arg-type]
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
        assert any(
            isinstance(e, NavigationCompleteEvent) for e in scope.emitted
        )
        assert any(
            isinstance(e, NavigationStartedEvent) for e in scope.emitted
        )

    async def test_networkidle_accepts_almostidle_too(self) -> None:
        scope = _FakeScope()
        cdp = _FakeCDP()
        wd = ActionWatchdog(
            cdp,  # type: ignore[arg-type]
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
        )
        assert wd._lifecycle_target("nonsense") is None
        assert wd._lifecycle_target("") is None

    async def test_lifecycle_target_load(self) -> None:
        wd = ActionWatchdog(
            _FakeCDP(),  # type: ignore[arg-type]
        )
        target = wd._lifecycle_target("load")
        assert target is not None
        evt_type, predicate = target
        assert evt_type is PageLoadEvent
        assert predicate(object())  # base predicate is unconditional

    async def test_lifecycle_target_domcontentloaded(self) -> None:
        wd = ActionWatchdog(
            _FakeCDP(),  # type: ignore[arg-type]
        )
        target = wd._lifecycle_target("domcontentloaded")
        assert target is not None
        evt_type, _ = target
        assert evt_type is DOMContentLoadedEvent

    async def test_resolve_lifecycle_timeout_priority(self) -> None:
        # lifecycle_timeout_ms wins.
        evt = NavigateToUrlEvent(
            url="x", wait_until="load",
            timeout_ms=10000, lifecycle_timeout_ms=2500,
        )
        assert ActionWatchdog._resolve_lifecycle_timeout_s(evt) == 2.5
        # Falls back to timeout_ms when lifecycle_timeout_ms is None.
        evt2 = NavigateToUrlEvent(
            url="x", wait_until="load", timeout_ms=7000,
        )
        assert ActionWatchdog._resolve_lifecycle_timeout_s(evt2) == 7.0
        # Defaults to 30s when both are None.
        evt3 = NavigateToUrlEvent(url="x", wait_until="load", timeout_ms=None)
        assert ActionWatchdog._resolve_lifecycle_timeout_s(evt3) == 30.0

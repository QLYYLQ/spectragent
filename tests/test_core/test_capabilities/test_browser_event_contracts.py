"""AF business, ownership and cleanup contracts on the installed agent-cdp API."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agent_cdp import BaseEvent, ConnectionType, Participation, ScopeGroup, expect

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.actions.click_coordinate import ClickCoordinateAction
from spectragent.core.capabilities.browser.dom.models import DOMElementNode
from spectragent.core.capabilities.browser.sessions import TabSessions
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.crash_watchdog import CrashWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
    ClickCoordinateEvent,
    ExtractContentEvent,
    GoBackEvent,
    GoForwardEvent,
    RefreshEvent,
    SelectDropdownEvent,
)
from spectragent.core.capabilities.browser.watchdogs.popups_watchdog import PopupsWatchdog
from spectragent.schemas.provider_session import ProviderSession


class _CDP:
    def __init__(self) -> None:
        self.is_connected = True
        self.calls: list[tuple[str, str | None]] = []
        self.callbacks: dict[str, list[Any]] = {}

    async def send(
        self,
        method: str,
        params: Any = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, session_id))
        if method == "DOM.getOuterHTML":
            return {"outerHTML": "<p>frame</p>"}
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        self.callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Any) -> None:
        self.callbacks[method].remove(callback)

    def fire(self, method: str, params: dict[str, Any], route: str | None) -> None:
        for callback in tuple(self.callbacks.get(method, ())):
            callback(params, route)


class _HeldBrowserRequest(BaseEvent[None]):
    pass


async def test_observers_and_waiters_cannot_replace_the_action_executor() -> None:
    group = ScopeGroup("missing-action-executor")
    scope = await group.create_scope("tab", max_history_size=0)
    cap = BrowserCapability()
    cap._default_session_id = "sid"
    cap._scopes["sid"] = scope
    seen: list[Any] = []
    scope.connect(
        ClickCoordinateEvent,
        seen.append,
        mode=ConnectionType.DIRECT,
        participation=Participation.OBSERVER,
    )
    try:
        with expect(scope, ClickCoordinateEvent):
            result = await ClickCoordinateAction().execute(
                {"coordinate_x": 2, "coordinate_y": 3}, capability=cap
            )
        assert not result.success
        assert "browser.action" in (result.error or "")
        assert seen == [], "receiver preflight must precede even direct observers"
    finally:
        await group.close_all()


async def test_none_is_valid_only_for_an_explicit_void_action_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = ScopeGroup("action-business-results")
    scope = await group.create_scope("tab")
    watchdog = ActionWatchdog(_CDP(), input_platform="linux")

    async def empty_click(event: ClickCoordinateEvent) -> None:
        return None

    monkeypatch.setattr(watchdog, "_on_click_coordinate", empty_click)
    watchdog.attach(scope, "sid")
    try:
        with pytest.raises(RuntimeError, match="successful clicked"):
            await dispatch_request(scope, ClickCoordinateEvent(coordinate_x=1, coordinate_y=2))
        await dispatch_request(scope, RefreshEvent())  # Acknowledged void CDP command.
    finally:
        await group.close_all()


async def test_owner_timeout_withdraws_a_queued_action_before_the_next_observation() -> None:
    group = ScopeGroup("queued-action-owner")
    scope = await group.create_scope("tab")
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold(event: _HeldBrowserRequest) -> None:
        entered.set()
        await release.wait()

    scope.connect(_HeldBrowserRequest, hold, mode=ConnectionType.QUEUED)
    cdp = _CDP()
    watchdog = ActionWatchdog(cdp, input_platform="linux")
    watchdog.attach(scope, "sid")
    scope.emit(_HeldBrowserRequest())
    await entered.wait()
    action = ClickCoordinateEvent(coordinate_x=1, coordinate_y=2, event_timeout=0.01)
    try:
        with pytest.raises(TimeoutError):
            await dispatch_request(scope, action)
        assert {result.status.value for result in action.results.values()} == {"cancelled"}
        release.set()
        await scope.flush(timeout=1)
        assert not cdp.calls
    finally:
        release.set()
        await group.close_all()


async def test_owner_cancellation_does_not_claim_a_running_browser_action_was_rolled_back() -> None:
    group = ScopeGroup("running-action-owner")
    scope = await group.create_scope("tab")
    entered, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    async def execute(event: RefreshEvent) -> None:
        effects.append("sent")
        entered.set()
        await release.wait()
        effects.append("acknowledged")

    scope.connect(RefreshEvent, execute, mode=ConnectionType.QUEUED, receiver_key="browser.action")
    event = RefreshEvent()
    owner = asyncio.create_task(dispatch_request(scope, event))
    try:
        await entered.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert {result.status.value for result in event.results.values()} == {"running"}
        release.set()
        await scope.flush(timeout=1)
        assert effects == ["sent", "acknowledged"]
        event.raise_for_status()
    finally:
        release.set()
        await group.close_all()


async def test_element_session_must_still_belong_to_the_bound_tab() -> None:
    group = ScopeGroup("element-ownership")
    scope = await group.create_scope("tab")
    sessions = TabSessions()
    sessions.bind("tab-a")
    sessions.bind("tab-b")
    sessions.attached({"sessionId": "frame-a", "targetInfo": {"type": "iframe"}}, "tab-a")
    node = DOMElementNode(tag_name="p", backend_node_id=1, session_id="frame-a")
    cdp = _CDP()
    watchdog = ActionWatchdog(
        cdp, input_platform="linux", sessions=sessions, selector_map_getter=lambda: {1: node}
    )
    watchdog.attach(scope, "tab-a")
    try:
        await dispatch_request(scope, ExtractContentEvent(backend_node_id=1, session_id="frame-a"))
        assert cdp.calls == [("DOM.getOuterHTML", "frame-a")]
        sessions.detached({"sessionId": "frame-a"}, "tab-a")
        for target in ("frame-a", "tab-b"):
            with pytest.raises(RuntimeError, match="does not belong to live tab"):
                await dispatch_request(
                    scope, ExtractContentEvent(backend_node_id=1, session_id=target)
                )
        assert len(cdp.calls) == 1
        sessions.unbind("tab-a")
        sessions.attached({"sessionId": "late", "targetInfo": {"type": "iframe"}}, "tab-a")
        assert not sessions.owns("tab-b", "late")
        assert not sessions.owns("tab-a", "late")
    finally:
        await group.close_all()


async def test_shared_popup_bindings_use_envelope_sessions_and_close_independently() -> None:
    group = ScopeGroup("shared-popups")
    a, b = await group.create_scope("a"), await group.create_scope("b")
    cdp = _CDP()
    watchdog = PopupsWatchdog(cdp)
    watchdog.attach(a, "sid-a")
    watchdog.attach(b, "sid-b")
    try:
        cdp.fire("Page.javascriptDialogOpening", {"type": "alert", "sessionId": "sid-a"}, None)
        await a.flush(timeout=1)
        assert not cdp.calls
        await group.close_scope("a")
        cdp.fire("Page.javascriptDialogOpening", {"type": "alert"}, "sid-a")
        cdp.fire("Page.javascriptDialogOpening", {"type": "alert"}, "sid-b")
        await b.flush(timeout=1)
        assert cdp.calls == [("Page.handleJavaScriptDialog", "sid-b")]
        assert len(watchdog.bindings) == 1
    finally:
        await group.close_all()
    assert not any(cdp.callbacks.values())


async def test_crash_observer_resolves_inspector_envelope_to_target(
    caplog: pytest.LogCaptureFixture,
) -> None:
    group = ScopeGroup("crash-routing")
    scope = await group.create_scope("browser")
    cdp = _CDP()
    watchdog = CrashWatchdog(cdp, target_sessions_getter=lambda: {"session-a": "target-a"})
    watchdog.attach(scope)
    try:
        with caplog.at_level(logging.ERROR):
            cdp.fire("Inspector.targetCrashed", {"sessionId": "session-a"}, None)
            cdp.fire("Inspector.targetCrashed", {}, "session-a")
            cdp.fire("Target.targetCrashed", {"targetId": "unknown"}, None)
            await scope.flush(timeout=1)
        messages = [
            record.getMessage()
            for record in caplog.records
            if "Target crashed:" in record.getMessage()
        ]
        assert len(messages) == 1
        assert "target_id=target-a" in messages[0]
    finally:
        await group.close_all()


async def test_archive_observer_failure_does_not_fail_the_observed_fragment() -> None:
    group = ScopeGroup("capture-and-observer")
    scope = await group.create_scope("tab", max_history_size=0)
    cap = BrowserCapability()
    cap._default_session_id = "sid"
    cap._scopes["sid"] = scope

    async def capture(event: BrowserStateRequestEvent) -> BrowserStateSummary:
        return BrowserStateSummary(fragments={"browser.platform": "fixture"})

    async def fail_recording(event: BrowserStateRequestEvent) -> None:
        raise RuntimeError("archive fixture failed")

    scope.connect(BrowserStateRequestEvent, capture, mode=ConnectionType.QUEUED)
    scope.connect(
        BrowserStateRequestEvent,
        fail_recording,
        mode=ConnectionType.QUEUED,
        participation=Participation.OBSERVER,
    )
    try:
        result = await cap._fire_state_event("sid", scope, frozenset({"browser.platform"}))
        assert result.fragments == {"browser.platform": "fixture"}
        assert result.errors == []
    finally:
        await group.close_all()


async def test_teardown_attempts_scope_transport_and_provider_after_archive_failure() -> None:
    cap = BrowserCapability()
    closed: list[str] = []
    group = ScopeGroup("cleanup-failures")
    scope = await group.create_scope("tab")
    cap._scope_group = group
    cap._scopes["sid"] = scope

    async def archive_close() -> None:
        closed.append("archive")
        raise RuntimeError("archive cleanup failed")

    async def control_close() -> None:
        closed.append("control")

    async def provider_close(session: ProviderSession) -> None:
        closed.append("provider")

    def scope_close() -> None:
        closed.append("scope")
        raise RuntimeError("scope cleanup failed")

    scope.add_close_callback(scope_close)
    cap._archive = cast(Any, SimpleNamespace(aclose=archive_close))
    cap._cdp = cast(Any, SimpleNamespace(stop=control_close))
    cap.backend = cast(
        Any, SimpleNamespace(open=lambda: None, close=provider_close, provider_id="fixture")
    )
    cap._session = ProviderSession(provider_id="fixture", session_id="owned", cdp_url="ws://owned")
    try:
        with pytest.raises(ExceptionGroup, match="Browser resource cleanup failed"):
            await cap.teardown()
        assert closed == ["archive", "scope", "control", "provider"]
        assert cap._scope_group is group, "unfinished scope remains supervised"
        assert cap._session is None
    finally:
        scope.remove_close_callback(scope_close)
        await group.close_all()


@pytest.mark.parametrize("event, entry_id", [(GoBackEvent(), 11), (GoForwardEvent(), 33)])
async def test_history_actions_navigate_to_the_adjacent_committed_entry(
    event: Any, entry_id: int
) -> None:
    sent: list[tuple[str, Any, str | None]] = []

    class HistoryCDP(_CDP):
        async def send(
            self,
            method: str,
            params: Any = None,
            session_id: str | None = None,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            sent.append((method, params, session_id))
            return {"currentIndex": 1, "entries": [{"id": 11}, {"id": 22}, {"id": 33}]}

    group = ScopeGroup("history-action")
    scope = await group.create_scope("tab")
    watchdog = ActionWatchdog(HistoryCDP(), input_platform="linux")
    watchdog.attach(scope, "sid")
    try:
        await dispatch_request(scope, event)
        assert sent == [
            ("Page.getNavigationHistory", {}, "sid"),
            ("Page.navigateToHistoryEntry", {"entryId": entry_id}, "sid"),
        ]
    finally:
        await group.close_all()


@pytest.mark.parametrize("event", [GoBackEvent(), GoForwardEvent()])
async def test_history_without_an_adjacent_entry_is_an_action_failure(event: Any) -> None:
    class HistoryCDP(_CDP):
        async def send(
            self,
            method: str,
            params: Any = None,
            session_id: str | None = None,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            assert method == "Page.getNavigationHistory"
            return {"currentIndex": 0, "entries": [{"id": 22}]}

    group = ScopeGroup("empty-history-action")
    scope = await group.create_scope("tab")
    watchdog = ActionWatchdog(HistoryCDP(), input_platform="linux")
    watchdog.attach(scope, "sid")
    try:
        with pytest.raises(RuntimeError, match="no navigation history entry"):
            await dispatch_request(scope, event)
    finally:
        await group.close_all()


@pytest.mark.parametrize(
    "params, reply",
    [
        ({"text": "missing"}, {"result": {"value": False}}),
        ({"index": 999}, {"result": {"value": False}}),
        ({"value": "b"}, {"exceptionDetails": {"text": "page exception"}}),
    ],
)
async def test_dropdown_missing_match_and_javascript_failure_are_not_success(
    params: dict[str, Any], reply: dict[str, Any]
) -> None:
    class DropdownCDP(_CDP):
        async def send(
            self,
            method: str,
            params: Any = None,
            session_id: str | None = None,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            if method == "DOM.resolveNode":
                return {"object": {"objectId": "select"}}
            assert method == "Runtime.callFunctionOn"
            assert params["returnByValue"] is True
            return reply

    group = ScopeGroup("dropdown-contract")
    scope = await group.create_scope("tab")
    watchdog = ActionWatchdog(
        DropdownCDP(),
        input_platform="linux",
        selector_map_getter=lambda: {1: DOMElementNode(tag_name="select", backend_node_id=1)},
    )
    watchdog.attach(scope, "sid")
    try:
        with pytest.raises(RuntimeError, match="Dropdown"):
            await dispatch_request(scope, SelectDropdownEvent(backend_node_id=1, **params))
    finally:
        await group.close_all()


async def test_tab_cleanup_failure_keeps_its_binding_and_still_closes_target_then_retries() -> None:
    group = ScopeGroup("tab-cleanup-retry")
    scope, js = await group.create_scope("target"), await group.create_scope("target:js")
    cdp = _CDP()
    cap = BrowserCapability()
    cap._cdp = cast(Any, cdp)
    cap._scope_group = group
    cap._scopes["sid"] = scope
    cap._js_scopes["sid"] = js
    cap._session_to_target["sid"] = "target"
    cap._target_to_session["target"] = "sid"
    cap._tab_sessions.bind("sid")
    watchdog = PopupsWatchdog(cdp)
    watchdog.attach(scope, "sid")
    cap._watchdogs["target"] = [watchdog]
    fail = True

    def cleanup() -> None:
        if fail:
            raise RuntimeError("binding close failed")

    watchdog.track_binding(scope, "sid", close_callbacks=[cleanup])
    try:
        with pytest.raises(ExceptionGroup, match="Tab 'target' close failed"):
            await cap.close_tab("sid")
        assert ("Target.closeTarget", None) in cdp.calls
        assert js.is_closed
        assert watchdog.bindings
        assert cap._session_to_target["sid"] == "target"
        assert not cap._tab_sessions.owns("sid", "sid")
        fail = False
        await cap.close_tab("sid")
        assert not watchdog.bindings
        assert group.scope_count == 0
        assert "sid" not in cap._session_to_target
    finally:
        fail = False
        await group.close_all()


async def test_cancelled_tab_cleanup_attempts_the_second_scope_and_target_before_propagating() -> (
    None
):
    attempted: list[str] = []

    class CancelOnceGroup(ScopeGroup):
        async def close_scope(self, scope_id: str, **options: Any) -> Any:
            attempted.append(scope_id)
            if attempted == ["target"]:
                raise asyncio.CancelledError()
            return await super().close_scope(scope_id, **options)

    group = CancelOnceGroup("tab-cleanup-cancel")
    scope, js = await group.create_scope("target"), await group.create_scope("target:js")
    cdp = _CDP()
    cap = BrowserCapability()
    cap._cdp = cast(Any, cdp)
    cap._scope_group = group
    cap._scopes["sid"] = scope
    cap._js_scopes["sid"] = js
    cap._session_to_target["sid"] = "target"
    cap._target_to_session["target"] = "sid"
    try:
        with pytest.raises(asyncio.CancelledError):
            await cap.close_tab("sid")
        assert attempted == ["target", "target:js"]
        assert ("Target.closeTarget", None) in cdp.calls
        assert js.is_closed
        assert cap._session_to_target["sid"] == "target"
        await cap.close_tab("sid")
        assert group.scope_count == 0
    finally:
        await group.close_all()

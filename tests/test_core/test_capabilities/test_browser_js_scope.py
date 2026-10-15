"""Cross-scope JS dispatch and tab-pair lifecycle."""

from __future__ import annotations

from typing import Any

import pytest
from agent_cdp import BaseEvent, ConnectionType, ScopeGroup
from agent_cdp.events.aggregation import HandlerError, event_result

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.watchdogs.events import ExecuteJavaScriptEvent
from spectragent.core.capabilities.browser.watchdogs.js_execution import (
    JavaScriptExecutionWatchdog,
)
from tests._helpers.browser_platform import PlatformProbe


class _ObserveRequest(BaseEvent[dict[str, Any]]):
    """Stand-in for a DOM resolver waiting inside its queued handler."""


class _CDP:
    def __init__(self) -> None:
        self.is_connected = True
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self.callbacks: dict[str, list[Any]] = {}
        self.targets = {"a"}
        self.platform_probe = PlatformProbe()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        self.sent.append((method, params, session_id))
        platform = self.platform_probe.reply(method, params, session_id)
        if platform is not None:
            return platform
        if method == "Target.attachToTarget":
            return {"sessionId": f"sid-{params['targetId']}"}
        if method == "Target.getTargets":
            return {
                "targetInfos": [
                    {"type": "page", "targetId": target, "url": "about:blank"}
                    for target in sorted(self.targets)
                ]
            }
        if method == "Target.createTarget":
            self.targets.add("b")
            return {"targetId": "b"}
        if method == "Target.closeTarget":
            self.targets.discard(params["targetId"])
            return {}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": f"frame-{session_id}"}}}
        if method == "Runtime.evaluate":
            return {"result": {"type": "string", "value": f"{session_id}:{params['expression']}"}}
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        self.callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Any) -> None:
        self.callbacks[method].remove(callback)

    def fire(self, method: str, params: dict[str, Any], parent_session_id: str | None) -> None:
        for callback in list(self.callbacks.get(method, [])):
            callback(params, parent_session_id)


def _attach_frame(cdp: _CDP, child: str, parent: str, *, target_type: str = "iframe") -> None:
    cdp.fire(
        "Target.attachedToTarget",
        {"sessionId": child, "targetInfo": {"type": target_type, "targetId": child}},
        parent,
    )


def _js_request(session_id: str, expression: str = "document.title") -> ExecuteJavaScriptEvent:
    return ExecuteJavaScriptEvent(
        method="Runtime.evaluate",
        params={"expression": expression, "returnByValue": True},
        session_id=session_id,
        event_timeout=1.0,
    )


def _fresh_js(event: ExecuteJavaScriptEvent) -> ExecuteJavaScriptEvent:
    return ExecuteJavaScriptEvent(
        method=event.method,
        params=dict(event.params),
        session_id=event.session_id,
        target_session_id=event.target_session_id,
        event_timeout=event.event_timeout,
    )


async def test_queued_observation_can_await_js_on_another_scope() -> None:
    cdp = _CDP()
    group = ScopeGroup("test-js")
    observation = await group.create_scope("tab-observation")
    js = await group.create_scope("tab-js")
    watchdog = JavaScriptExecutionWatchdog(cdp)  # type: ignore[arg-type]
    watchdog.bind_tab_scopes(observation, js, session_id="sid-1")

    async def observe(_event: _ObserveRequest) -> dict[str, Any]:
        request = observation.emit(_js_request("sid-1"))
        reply = await event_result(request)
        assert reply is not None
        return reply

    observation.connect(_ObserveRequest, observe, mode=ConnectionType.QUEUED)
    try:
        result = await event_result(observation.emit(_ObserveRequest(event_timeout=1.0)))
        assert result == {"result": {"type": "string", "value": "sid-1:document.title"}}
        assert (
            "Runtime.evaluate",
            {"expression": "document.title", "returnByValue": True},
            "sid-1",
        ) in cdp.sent
    finally:
        await group.close_all()


async def test_shared_js_watchdog_routes_two_tabs_and_rejects_wrong_session() -> None:
    cdp = _CDP()
    group = ScopeGroup("test-js-nm")
    obs_a = await group.create_scope("a-observation")
    js_a = await group.create_scope("a-js")
    obs_b = await group.create_scope("b-observation")
    js_b = await group.create_scope("b-js")
    watchdog = JavaScriptExecutionWatchdog(cdp)  # type: ignore[arg-type]
    global_scope = await group.create_scope("browser")
    watchdog.attach(global_scope)
    watchdog.bind_tab_scopes(obs_a, js_a, session_id="sid-a")
    watchdog.bind_tab_scopes(obs_b, js_b, session_id="sid-b")

    try:
        result_a = await event_result(obs_a.emit(_js_request("sid-a", "a")))
        result_b = await event_result(obs_b.emit(_js_request("sid-b", "b")))
        assert result_a == {"result": {"type": "string", "value": "sid-a:a"}}
        assert result_b == {"result": {"type": "string", "value": "sid-b:b"}}

        frame_request = _js_request("sid-a", "frame")
        frame_request.target_session_id = "iframe-a"
        with pytest.raises(HandlerError, match="does not belong"):
            await event_result(obs_a.emit(_fresh_js(frame_request)))
        assert not any(params.get("expression") == "frame" for _, params, _ in cdp.sent)

        _attach_frame(cdp, "iframe-a", "unknown-parent")
        _attach_frame(cdp, "iframe-a", "sid-a", target_type="page")
        with pytest.raises(HandlerError, match="does not belong"):
            await event_result(obs_a.emit(_fresh_js(frame_request)))

        _attach_frame(cdp, "iframe-a", "sid-a")
        _attach_frame(cdp, "nested-a", "iframe-a")
        _attach_frame(cdp, "iframe-b", "sid-b")
        assert await event_result(obs_a.emit(_fresh_js(frame_request))) == {
            "result": {"type": "string", "value": "iframe-a:frame"}
        }
        nested_request = _js_request("sid-a", "nested")
        nested_request.target_session_id = "nested-a"
        assert await event_result(obs_a.emit(_fresh_js(nested_request))) == {
            "result": {"type": "string", "value": "nested-a:nested"}
        }

        cross_tab_request = _js_request("sid-a", "cross-tab")
        cross_tab_request.target_session_id = "iframe-b"
        with pytest.raises(HandlerError, match="does not belong"):
            await event_result(obs_a.emit(_fresh_js(cross_tab_request)))
        cross_tab_request.target_session_id = "sid-b"
        with pytest.raises(HandlerError, match="does not belong"):
            await event_result(obs_a.emit(_fresh_js(cross_tab_request)))
        assert not any(params.get("expression") == "cross-tab" for _, params, _ in cdp.sent)

        cdp.fire("Target.detachedFromTarget", {"sessionId": "iframe-a"}, "sid-a")
        with pytest.raises(HandlerError, match="does not belong"):
            await event_result(obs_a.emit(_fresh_js(nested_request)))

        with pytest.raises(HandlerError, match="session mismatch"):
            await event_result(obs_a.emit(_js_request("sid-b", "wrong")))
        assert not any(params.get("expression") == "wrong" for _, params, _ in cdp.sent)

        await group.close_scope("a-observation")
        await group.close_scope("a-js")
        assert await event_result(obs_b.emit(_js_request("sid-b", "still-live"))) == {
            "result": {"type": "string", "value": "sid-b:still-live"}
        }
    finally:
        watchdog.detach(global_scope)
        await group.close_all()

    assert not any(cdp.callbacks.values())


async def test_js_watchdog_limits_methods_and_surfaces_page_exceptions() -> None:
    cdp = _CDP()
    watchdog = JavaScriptExecutionWatchdog(cdp)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unsupported JS CDP method"):
        await watchdog._execute(
            ExecuteJavaScriptEvent(
                method="Page.navigate", params={"url": "about:blank"}, session_id="sid"
            ),
            session_id="sid",
        )
    assert cdp.sent == []

    async def exception_reply(
        method: str, params: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        return {"exceptionDetails": {"text": "Uncaught ReferenceError"}}

    cdp.send = exception_reply  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Uncaught ReferenceError"):
        await watchdog._execute(_js_request("sid"), session_id="sid")


async def test_capability_closes_both_scopes_for_one_tab() -> None:
    cdp = _CDP()
    cap = BrowserCapability()
    cap._inject_cdp_client(cdp)  # type: ignore[arg-type]
    await cap.setup()
    group = cap._scope_group
    assert group is not None

    try:
        sid_a = cap.default_session_id
        assert sid_a == "sid-a"
        sid_b = await cap.create_tab()
        await cap.switch_tab("b")
        assert set(group.scope_ids) == {"__browser__", "a", "a:js", "b", "b:js"}
        assert cap._watchdog_context(sid_a).js_scope is cap._js_scopes[sid_a]
        assert (
            len([wd for wd in cap._shared_watchdogs if isinstance(wd, JavaScriptExecutionWatchdog)])
            == 1
        )

        assert await event_result(cap._scopes[sid_a].emit(_js_request(sid_a))) == {
            "result": {"type": "string", "value": f"{sid_a}:document.title"}
        }
        _attach_frame(cdp, "iframe-a", sid_a)
        frame_request = _js_request(sid_a, "before-close")
        frame_request.target_session_id = "iframe-a"
        assert await event_result(cap._scopes[sid_a].emit(frame_request)) == {
            "result": {"type": "string", "value": "iframe-a:before-close"}
        }
        await cap.close_tab(sid_a)
        assert set(group.scope_ids) == {"__browser__", "b", "b:js"}
        assert sid_a not in cap._scopes and sid_a not in cap._js_scopes
        _attach_frame(cdp, "iframe-after-close", sid_a)
        closed_tab_target = _js_request(sid_b, "after-close")
        closed_tab_target.target_session_id = "iframe-after-close"
        with pytest.raises(HandlerError, match="does not belong"):
            await event_result(cap._scopes[sid_b].emit(closed_tab_target))
        assert await event_result(cap._scopes[sid_b].emit(_js_request(sid_b))) == {
            "result": {"type": "string", "value": f"{sid_b}:document.title"}
        }
    finally:
        await cap.teardown()

    assert group.scope_count == 0
    assert cap._js_scopes == {}
    assert not any(cdp.callbacks.values())


async def test_failed_tab_attach_rolls_back_every_scope_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cdp = _CDP()
    cap = BrowserCapability()
    cap._inject_cdp_client(cdp)  # type: ignore[arg-type]
    await cap.setup()
    group = cap._scope_group
    assert group is not None
    js_watchdog = next(
        wd for wd in cap._shared_watchdogs if isinstance(wd, JavaScriptExecutionWatchdog)
    )
    original_bind = js_watchdog.bind_tab_scopes
    failed = False

    def fail_after_binding(observation_scope: Any, js_scope: Any, *, session_id: str) -> None:
        nonlocal failed
        original_bind(observation_scope, js_scope, session_id=session_id)
        if session_id == "sid-b" and not failed:
            failed = True
            raise RuntimeError("binding failed after connection")

    monkeypatch.setattr(js_watchdog, "bind_tab_scopes", fail_after_binding)
    before_callbacks = {name: len(callbacks) for name, callbacks in cdp.callbacks.items()}
    cdp.targets.add("b")  # A page-owned tab; switch_tab must leave it open on failure.

    try:
        with pytest.raises(RuntimeError, match="binding failed"):
            await cap.switch_tab("b")

        assert set(group.scope_ids) == {"__browser__", "a", "a:js"}
        assert "sid-b" not in cap._scopes
        assert "sid-b" not in cap._js_scopes
        assert "sid-b" not in cap._bridges
        assert "sid-b" not in cap._main_frame_ids
        assert "b" not in cap._watchdogs
        assert "b" not in cap._target_to_session
        assert "sid-b" not in cap._session_to_target
        assert not cap._tab_sessions.owns("sid-b", "sid-b")
        assert {name: len(callbacks) for name, callbacks in cdp.callbacks.items()} == (
            before_callbacks
        )
        assert ("Target.detachFromTarget", {"sessionId": "sid-b"}, None) in cdp.sent
        assert "b" in cdp.targets
        assert ("Target.closeTarget", {"targetId": "b"}, None) not in cdp.sent

        sid_b = (await cap.switch_tab("b")).session_id
        assert sid_b == "sid-b"
        assert set(group.scope_ids) == {"__browser__", "a", "a:js", "b", "b:js"}
        assert await event_result(cap._scopes[sid_b].emit(_js_request(sid_b))) == {
            "result": {"type": "string", "value": f"{sid_b}:document.title"}
        }
    finally:
        await cap.teardown()


async def test_create_tab_closes_its_target_if_attach_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cdp = _CDP()
    cap = BrowserCapability()
    cap._inject_cdp_client(cdp)  # type: ignore[arg-type]
    await cap.setup()
    original_attach = cap._attach_tab

    async def fail_attach(target_id: str, url: str = "") -> str:
        raise RuntimeError(f"failed to attach {target_id}")

    try:
        monkeypatch.setattr(cap, "_attach_tab", fail_attach)
        with pytest.raises(RuntimeError, match="failed to attach b"):
            await cap.create_tab()
        assert "b" not in cdp.targets
        assert ("Target.closeTarget", {"targetId": "b"}, None) in cdp.sent

        monkeypatch.setattr(cap, "_attach_tab", original_attach)
        assert await cap.create_tab() == "sid-b"
        assert "b" in cdp.targets
    finally:
        await cap.teardown()

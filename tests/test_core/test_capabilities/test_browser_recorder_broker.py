"""The shared recorder owns one socket and preserves each run's early-attach boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest

from spectragent.core.capabilities.browser import recorder_broker
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.recorder import (
    CDPCommand,
    RecorderConnection,
    RecorderParticipantBase,
    RecorderTarget,
)
from spectragent.core.capabilities.browser.recorder_broker import RecorderBroker


class Reply:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.abandoned = False

    async def wait(self, timeout: float | None = None) -> Any:
        return self.result

    def abandon(self) -> None:
        self.abandoned = True


class Transport:
    def __init__(self) -> None:
        self.is_connected = False
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.callbacks: dict[str, list[Callable[..., Any]]] = {}
        self.close_callbacks: list[Callable[..., Any]] = []
        self.targets: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, str] = {}
        self.sequence = 0
        self.auto_attach = False
        self.stop_count = 0
        self.fail_detach: set[str] = set()
        self.fail_disable = False
        self.fail_stop = False
        self.suppress_attach_event = False
        self.resume_gate: asyncio.Event | None = None

    def target(self, target_id: str, context_id: str, type: str = "page") -> dict[str, Any]:
        info = {
            "targetId": target_id,
            "browserContextId": context_id,
            "type": type,
            "url": "about:blank",
        }
        self.targets[target_id] = info
        return info

    def attach(self, target_id: str, *, parent: str | None = None, waiting: bool = False) -> str:
        self.sequence += 1
        sid = f"session-{self.sequence}"
        self.sessions[sid] = target_id
        if not self.suppress_attach_event:
            self.fire(
                "Target.attachedToTarget",
                {
                    "sessionId": sid,
                    "targetInfo": self.targets[target_id],
                    "waitingForDebugger": waiting,
                },
                parent,
            )
        return sid

    def fire(self, method: str, params: dict[str, Any], sid: str | None = None) -> None:
        for callback in tuple(self.callbacks.get(method, ())):
            callback(params, sid)

    async def start(self) -> None:
        self.is_connected = True

    async def stop(self) -> None:
        self.stop_count += 1
        if self.fail_stop:
            raise RuntimeError("socket stop failed")
        self.is_connected = False
        for callback in self.close_callbacks:
            callback(None)

    def lose(self, error: BaseException) -> None:
        self.is_connected = False
        for callback in self.close_callbacks:
            callback(error)

    def add_close_callback(self, callback: Callable[..., Any]) -> None:
        self.close_callbacks.append(callback)

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        self.callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        self.callbacks[method].remove(callback)

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        params = dict(params or {})
        self.calls.append((method, params, session_id))
        if method == "Target.setAutoAttach" and session_id is None:
            if not params["autoAttach"] and self.fail_disable:
                raise RuntimeError("disable failed")
            self.auto_attach = params["autoAttach"]
        elif method == "Target.getTargets":
            return {"targetInfos": list(self.targets.values())}
        elif method == "Target.getTargetInfo":
            return {"targetInfo": self.targets[params["targetId"]]}
        elif method == "Target.attachToTarget":
            return {"sessionId": self.attach(params["targetId"])}
        elif method == "Target.detachFromTarget":
            sid = params["sessionId"]
            if sid in self.fail_detach:
                raise RuntimeError("detach failed")
            self.sessions.pop(sid, None)
            self.fire("Target.detachedFromTarget", {"sessionId": sid})
        elif method == "Runtime.runIfWaitingForDebugger":
            if self.resume_gate is not None:
                await self.resume_gate.wait()
        elif method == "Target.createTarget":
            self.target("created", params["browserContextId"])
            return {"targetId": "created"}
        return {}

    async def post(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> Reply:
        return Reply(await self.send(method, params, session_id))


async def setup_broker() -> tuple[Transport, RecorderBroker]:
    transport = Transport()
    broker = RecorderBroker(cast(CDPClient, transport))
    await broker.open()
    return transport, broker


async def settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


async def test_activation_delivers_existing_context_targets_before_ack_and_never_global_switch():
    transport, broker = await setup_broker()
    try:
        transport.target("a-page", "a")
        transport.target("a-sw", "a", "service_worker")
        transport.target("a-shared", "a", "shared_worker")
        transport.target("b-page", "b")
        a = broker.client_for_context("a")
        assert broker.client_for_context("a") is a
        events = []
        a.on_event("Target.attachedToTarget", lambda p, s: events.append((p, s)))
        await a.send("Target.setAutoAttach", {"autoAttach": True})
        assert {event[0]["targetInfo"]["targetId"] for event in events} == {
            "a-page",
            "a-sw",
            "a-shared",
        }
        root_switches = [
            c for c in transport.calls if c[0] == "Target.setAutoAttach" and c[2] is None
        ]
        assert len(root_switches) == 1
        await a.send("Target.setAutoAttach", {"autoAttach": False})
        assert (
            len([c for c in transport.calls if c[0] == "Target.setAutoAttach" and c[2] is None])
            == 1
        )
        sid = events[0][0]["sessionId"]
        await a.send("Fetch.disable", {}, sid)
    finally:
        await broker.close()


async def test_activation_fallback_still_delivers_before_local_post_ack():
    transport, broker = await setup_broker()
    try:
        transport.target("a", "a")
        transport.suppress_attach_event = True
        client = broker.client_for_context("a")
        received = []
        client.on_event("Target.attachedToTarget", lambda p, s: received.append(p))
        reply = await client.post("Target.setAutoAttach", {"autoAttach": True})
        assert len(received) == 1
        assert await reply.wait() == {}
        reply.abandon()
    finally:
        await broker.close()


async def test_session_tree_routes_inherited_children_and_drops_conflicting_and_late_events():
    transport, broker = await setup_broker()
    try:
        a, b = broker.client_for_context("a"), broker.client_for_context("b")
        events_a, events_b = [], []
        for client, events in [(a, events_a), (b, events_b)]:
            client.on_event(
                "Network.requestWillBeSent", lambda p, s, out=events: out.append((p, s))
            )
            await client.send("Target.setAutoAttach", {"autoAttach": True})
        transport.target("page-a", "a")
        transport.target("page-b", "b")
        parent_a, parent_b = transport.attach("page-a"), transport.attach("page-b")
        child_info = transport.target("worker-a", "a", "worker")
        child_info.pop("browserContextId")
        child = transport.attach("worker-a", parent=parent_a, waiting=True)
        transport.fire("Network.requestWillBeSent", {"requestId": "a"}, child)
        transport.fire("Network.requestWillBeSent", {"requestId": "b"}, parent_b)
        assert events_a == [({"requestId": "a"}, child)]
        assert events_b == [({"requestId": "b"}, parent_b)]
        transport.fire("Target.detachedFromTarget", {"sessionId": parent_a})
        transport.fire("Network.requestWillBeSent", {"requestId": "late"}, child)
        assert len(events_a) == 1
        transport.fire(
            "Target.attachedToTarget",
            {
                "sessionId": child,
                "targetInfo": {**child_info, "browserContextId": "a"},
                "waitingForDebugger": False,
            },
        )
        transport.fire("Network.requestWillBeSent", {"requestId": "resurrected"}, child)
        assert len(events_a) == 1
        with pytest.raises(PermissionError):
            await a.send("Runtime.evaluate", {}, child)
        late = transport.attach("worker-a", parent=parent_a, waiting=True)
        conflict = transport.attach("page-b", parent=parent_a, waiting=True)
        await settle()
        assert late not in transport.sessions
        assert conflict not in transport.sessions
        assert len(events_b) == 1
    finally:
        await broker.close()


async def test_unclaimed_and_inactive_targets_only_resume_and_detach_without_closing():
    transport, broker = await setup_broker()
    try:
        inactive = broker.client_for_context("inactive")
        seen = []
        inactive.on_event("Target.attachedToTarget", lambda p, s: seen.append(p))
        for context in ("unknown", "inactive"):
            transport.target(context, context)
            transport.attach(context, waiting=True)
        await settle()
        assert not seen
        assert not transport.sessions
        assert set(transport.targets) == {"unknown", "inactive"}
        effects = [method for method, _, sid in transport.calls if sid is not None]
        assert effects == ["Runtime.runIfWaitingForDebugger"] * 2
        assert not any(method == "Target.closeTarget" for method, _, _ in transport.calls)
    finally:
        await broker.close()


async def test_stop_one_context_keeps_other_socket_events_and_original_loss():
    transport, broker = await setup_broker()
    a, b = broker.client_for_context("a"), broker.client_for_context("b")
    losses_a, losses_b = [], []
    a.add_close_callback(losses_a.append)
    b.add_close_callback(losses_b.append)
    await a.send("Target.setAutoAttach", {"autoAttach": True})
    await b.send("Target.setAutoAttach", {"autoAttach": True})
    transport.target("a", "a")
    transport.target("b", "b")
    sid_a, sid_b = transport.attach("a"), transport.attach("b")
    received = []
    b.on_event("Fetch.requestPaused", lambda p, s: received.append(s))
    await a.stop()
    assert losses_a == [None]
    assert transport.stop_count == 0
    assert not a.is_connected and b.is_connected
    assert sid_a not in transport.sessions and sid_b in transport.sessions
    transport.fire("Fetch.requestPaused", {"requestId": "b"}, sid_b)
    assert received == [sid_b]
    error = ConnectionError("original socket loss")
    transport.lose(error)
    assert losses_b == [error]
    assert losses_a == [None]
    with pytest.raises(ConnectionError) as raised:
        await b.send("Browser.getVersion")
    assert raised.value is error
    await broker.close()


async def test_context_commands_enforce_target_session_and_response_boundaries():
    transport, broker = await setup_broker()
    try:
        a = broker.client_for_context("a")
        await a.send("Target.setAutoAttach", {"autoAttach": True})
        transport.target("a", "a")
        transport.target("b", "b")
        sid_a, sid_b = transport.attach("a"), transport.attach("b")
        for method, params, sid in [
            ("Runtime.evaluate", {}, sid_b),
            ("Target.closeTarget", {"targetId": "b"}, None),
            ("Target.closeTarget", {"targetId": "b"}, sid_a),
            ("Target.detachFromTarget", {"sessionId": sid_b}, None),
            ("Target.setDiscoverTargets", {"discover": False}, None),
            ("Browser.close", {}, None),
            ("Browser.close", {}, sid_a),
            ("Target.disposeBrowserContext", {"browserContextId": "a"}, None),
            ("Storage.getCookies", {"browserContextId": "b"}, None),
        ]:
            with pytest.raises(PermissionError):
                await a.post(method, params, sid)
        for method in ("Storage.getCookies", "Browser.setPermission", "Target.createTarget"):
            await a.send(method, {})
            assert transport.calls[-1][1]["browserContextId"] == "a"
        reply = await a.post("Target.getTargets")
        assert {i["browserContextId"] for i in (await reply.wait())["targetInfos"]} == {"a"}
        assert not any(method == "Browser.close" for method, _, _ in transport.calls)
    finally:
        await broker.close()


async def test_stop_failure_retains_sessions_for_retry_and_broker_close_still_stops_socket():
    transport, broker = await setup_broker()
    a = broker.client_for_context("a")
    await a.send("Target.setAutoAttach", {"autoAttach": True})
    transport.target("a", "a")
    sid = transport.attach("a", waiting=True)
    transport.fail_detach.add(sid)
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        await a.stop()
    assert a.is_connected
    assert broker.cleanup_failures
    transport.fail_detach.clear()
    await a.stop()
    transport.fail_disable = True
    transport.fail_stop = True
    with pytest.raises(RuntimeError, match="may be retried"):
        await broker.close()
    assert transport.stop_count == 1
    transport.fail_stop = False
    await broker.close()
    assert not transport.is_connected
    assert transport.stop_count == 2


async def test_cleanup_is_bounded_and_cancellation_preserves_other_context(monkeypatch):
    monkeypatch.setattr(recorder_broker, "_COMMAND_TIMEOUT_S", 0.02)
    transport, broker = await setup_broker()
    a, b = broker.client_for_context("a"), broker.client_for_context("b")
    await a.send("Target.setAutoAttach", {"autoAttach": True})
    await b.send("Target.setAutoAttach", {"autoAttach": True})
    transport.target("a", "a")
    transport.target("b", "b")
    sid_a, sid_b = transport.attach("a", waiting=True), transport.attach("b", waiting=True)
    transport.resume_gate = asyncio.Event()
    task = asyncio.create_task(a.stop())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)
    assert sid_a not in transport.sessions
    assert sid_b in transport.sessions and b.is_connected
    assert any("timed out" in failure for failure in broker.cleanup_failures)
    await broker.close()


class Marker(RecorderParticipantBase):
    def target_commands(self, target: RecorderTarget):
        return [
            CDPCommand("Page.enable"),
            CDPCommand("Page.addScriptToEvaluateOnNewDocument", {"source": "marker=true"}),
        ]


async def test_existing_recorder_connection_uses_context_client_without_adaptation():
    transport, broker = await setup_broker()
    transport.target("root-a", "a")
    transport.target("root-b", "b")
    losses = []
    records = []
    for context in ("a", "b"):
        client = broker.client_for_context(context)
        recorder = RecorderConnection(
            client, participants=lambda: [Marker()], channel=None, on_lost=losses.append
        )
        await recorder.open()
        assert await recorder.wait_prepared(f"root-{context}", 1)
        records.append(recorder)
    before = len(transport.calls)
    await records[0].close()
    assert transport.is_connected and transport.stop_count == 0
    assert not losses
    assert not any(
        method == "Target.setAutoAttach" and sid is None
        for method, _, sid in transport.calls[before:]
    )
    transport.target("popup-b", "b")
    transport.attach("popup-b", waiting=True)
    assert await records[1].wait_prepared("popup-b", 1)
    await records[1].close()
    await broker.close()


@pytest.mark.parametrize("use_post", [False, True])
async def test_detach_snapshot_is_idempotent_only_for_this_contexts_retired_sessions(use_post):
    transport, broker = await setup_broker()
    try:
        a, b = broker.client_for_context("a"), broker.client_for_context("b")
        for client in (a, b):
            await client.send("Target.setAutoAttach", {"autoAttach": True})
        transport.target("a-parent", "a")
        transport.target("a-child", "a", "iframe")
        transport.target("b-parent", "b")
        parent = transport.attach("a-parent")
        child = transport.attach("a-child", parent=parent)
        foreign = transport.attach("b-parent")
        await a.send("Target.detachFromTarget", {"sessionId": parent})
        await b.send("Target.detachFromTarget", {"sessionId": foreign})
        before = len(transport.calls)

        async def detach(sid, envelope=None):
            if use_post:
                return await (
                    await a.post("Target.detachFromTarget", {"sessionId": sid}, envelope)
                ).wait()
            return await a.send("Target.detachFromTarget", {"sessionId": sid}, envelope)

        assert await detach(child) == {}
        assert await detach(parent) == {}
        assert len(transport.calls) == before
        with pytest.raises(PermissionError):
            await detach(foreign)
        with pytest.raises(PermissionError):
            await detach(child, foreign)
        with pytest.raises(PermissionError):
            await detach("never-seen")
        assert len(transport.calls) == before
        assert not broker.cleanup_failures
    finally:
        await broker.close()


@pytest.mark.parametrize("deferred_reply", [False, True])
async def test_detach_race_after_write_accepts_retirement_evidence(monkeypatch, deferred_reply):
    transport, broker = await setup_broker()
    try:
        a = broker.client_for_context("a")
        await a.send("Target.setAutoAttach", {"autoAttach": True})
        transport.target("a-parent", "a")
        transport.target("a-child", "a", "iframe")
        parent = transport.attach("a-parent")
        child = transport.attach("a-child", parent=parent)
        original_post = transport.post

        class LostChildReply:
            async def wait(self, timeout=None):
                transport.fire("Target.detachedFromTarget", {"sessionId": parent})
                raise RuntimeError("No session with given id")

            def abandon(self):
                pass

        async def race(method, params=None, session_id=None):
            if method == "Target.detachFromTarget" and params == {"sessionId": child}:
                if deferred_reply:
                    return LostChildReply()
                transport.fire("Target.detachedFromTarget", {"sessionId": parent})
                raise RuntimeError("No session with given id")
            return await original_post(method, params, session_id)

        monkeypatch.setattr(transport, "post", race)
        assert await a.send("Target.detachFromTarget", {"sessionId": child}) == {}
    finally:
        await broker.close()


async def test_async_callback_does_not_block_other_context_or_later_sync_callbacks(caplog):
    transport, broker = await setup_broker()
    gate = asyncio.Event()
    entered = asyncio.Event()
    finished = asyncio.Event()
    seen = []
    try:
        a, b = broker.client_for_context("a"), broker.client_for_context("b")
        for client in (a, b):
            await client.send("Target.setAutoAttach", {"autoAttach": True})
        transport.target("a", "a")
        transport.target("b", "b")
        sid_a, sid_b = transport.attach("a"), transport.attach("b")

        async def blocked_callback(params, sid):
            entered.set()
            await gate.wait()
            seen.append(("async-a", sid))
            finished.set()

        def broken_callback(params, sid):
            raise ValueError("callback intentionally failed")

        a.on_event("Network.requestWillBeSent", blocked_callback)
        a.on_event("Network.requestWillBeSent", broken_callback)
        a.on_event("Network.requestWillBeSent", lambda p, s: seen.append(("sync-a", s)))
        b.on_event("Network.requestWillBeSent", lambda p, s: seen.append(("sync-b", s)))
        transport.fire("Network.requestWillBeSent", {"requestId": "a"}, sid_a)
        await asyncio.wait_for(entered.wait(), 0.5)
        transport.fire("Network.requestWillBeSent", {"requestId": "b"}, sid_b)
        assert await b.send("Runtime.evaluate", {}, sid_b) == {}
        assert seen == [("sync-a", sid_a), ("sync-b", sid_b)]
        assert "callback intentionally failed" in caplog.text
        gate.set()
        await asyncio.wait_for(finished.wait(), 0.5)
        assert seen[-1] == ("async-a", sid_a)
    finally:
        gate.set()
        await broker.close()


@pytest.mark.parametrize("suppresses_cancel", [False, True])
async def test_stop_cancels_only_its_context_callbacks_and_reports_unfinished_work(
    monkeypatch, suppresses_cancel
):
    monkeypatch.setattr(recorder_broker, "_COMMAND_TIMEOUT_S", 0.02)
    transport, broker = await setup_broker()
    gates = {context: asyncio.Event() for context in ("a", "b")}
    entered = {context: asyncio.Event() for context in ("a", "b")}
    ended = {context: asyncio.Event() for context in ("a", "b")}
    cancelled = []

    async def callback(context):
        entered[context].set()
        try:
            while not gates[context].is_set():
                try:
                    await gates[context].wait()
                except asyncio.CancelledError:
                    cancelled.append(context)
                    if context != "a" or not suppresses_cancel:
                        raise
        finally:
            ended[context].set()

    try:
        a, b = broker.client_for_context("a"), broker.client_for_context("b")
        sessions = {}
        for context, client in (("a", a), ("b", b)):
            await client.send("Target.setAutoAttach", {"autoAttach": True})
            client.on_event("Network.requestWillBeSent", lambda p, s, ctx=context: callback(ctx))
            transport.target(context, context)
            sessions[context] = transport.attach(context)
            transport.fire("Network.requestWillBeSent", {}, sessions[context])
            await asyncio.wait_for(entered[context].wait(), 0.5)
        if suppresses_cancel:
            with pytest.raises(RuntimeError, match="callback task.*remain unfinished"):
                await asyncio.wait_for(a.stop(), 0.5)
            assert a.is_connected
            assert any("Context 'a'" in failure for failure in broker.cleanup_failures)
            gates["a"].set()
            await asyncio.wait_for(ended["a"].wait(), 0.5)
        await asyncio.wait_for(a.stop(), 0.5)
        assert not a.is_connected
        assert ended["a"].is_set()
        assert cancelled == ["a"]
        assert not ended["b"].is_set()
        assert await b.send("Runtime.evaluate", {}, sessions["b"]) == {}
        gates["b"].set()
        await asyncio.wait_for(ended["b"].wait(), 0.5)
        assert cancelled == ["a"]
    finally:
        for gate in gates.values():
            gate.set()
        await broker.close()

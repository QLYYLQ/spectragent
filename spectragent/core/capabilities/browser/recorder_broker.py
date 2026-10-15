"""One early-attach connection for the contexts of a shared Chrome host.

Chrome has no BrowserContext filter for browser-level auto-attach. The broker
therefore briefly pauses targets outside active runs too, then immediately
resumes and detaches them; it never claims those targets were left untouched.
Only active context clients receive recorder events or may configure targets.
A context client's stop releases its sessions, not this host-owned connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.context_access import BrowserContextAccess
from spectragent.core.capabilities.browser.recorder import (
    AUTO_ATTACH_PARAMS,
    RELEASE_AUTO_ATTACH_PARAMS,
    PendingCommand,
)

logger = logging.getLogger(__name__)
_COMMAND_TIMEOUT_S = 1.0
_EXISTING_ROOT_TYPES = frozenset({"page", "shared_worker", "service_worker"})


class _Reply:
    """A local acknowledgement, or a response filtered at the same boundary."""

    def __init__(
        self,
        result: Any = None,
        *,
        pending: PendingCommand | None = None,
        transform: Callable[[Any], Any] | None = None,
        detached: Callable[[], bool] | None = None,
    ) -> None:
        self._result = result
        self._pending = pending
        self._transform = transform
        self._detached = detached

    async def wait(self, timeout: float | None = None) -> Any:
        try:
            result = self._result if self._pending is None else await self._pending.wait(timeout)
        except RuntimeError:
            # A concurrent parent detach may retire this owned child after
            # its command was written but before Chrome answers that it is gone.
            if self._detached is not None and self._detached():
                return {}
            raise
        return self._transform(result) if self._transform is not None else result

    def abandon(self) -> None:
        if self._pending is not None:
            self._pending.abandon()


@dataclass(frozen=True)
class _Session:
    context: ContextRecorderClient | None
    target_id: str
    parent_id: str | None


class RecorderBroker:
    """Own browser-wide attachment and route sessions to one context client.

    The host opens this once before starting runs and closes it after their
    recorder clients. There is no process-global registry or borrowed-browser
    ownership here. ``close`` always attempts the socket stop after command
    failures; unconfirmed cleanup remains visible and a failed stop is retryable.
    """

    def __init__(self, client: CDPClient) -> None:
        self._client = client
        self._contexts: dict[str, ContextRecorderClient] = {}
        self._sessions: dict[str, _Session] = {}
        self._retired_sessions: set[str] = set()
        self._listeners: dict[str, Callable[..., Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._cleanup_failures: list[str] = []
        self._opened = False
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._loss: BaseException | None = None

    @property
    def cleanup_failures(self) -> tuple[str, ...]:
        return tuple(self._cleanup_failures)

    async def open(self) -> None:
        if self._closing or self._closed:
            raise RuntimeError("Recorder broker is closing or closed")
        if self._opened:
            return
        self._listen("Target.attachedToTarget")
        self._listen("Target.detachedFromTarget")
        self._client.add_close_callback(self._on_closed)
        if not self._client.is_connected:
            await self._client.start()
        await self._client.send(
            "Target.setAutoAttach", dict(AUTO_ATTACH_PARAMS), timeout=_COMMAND_TIMEOUT_S
        )
        self._opened = True

    def client_for_context(self, browser_context_id: str) -> ContextRecorderClient:
        if not browser_context_id:
            raise ValueError("A recorder client requires an explicit BrowserContext id")
        if self._closing or self._closed:
            raise RuntimeError("Recorder broker is closing or closed")
        if browser_context_id not in self._contexts:
            self._contexts[browser_context_id] = ContextRecorderClient(self, browser_context_id)
        return self._contexts[browser_context_id]

    async def close(self) -> None:
        if self._closed:
            return
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close())
        await self._finish_cleanup(self._close_task)

    @staticmethod
    async def _finish_cleanup(task: asyncio.Task[None]) -> None:
        """Finish the finite cleanup before propagating the caller's cancellation."""
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled() and task.done():
                failure = task.exception()
                if failure is not None:
                    cancellation.add_note(f"Recorder cleanup also failed: {failure!r}")
            raise

    async def _close(self) -> None:
        self._closing = True
        for context in self._contexts.values():
            context._active = False
        # Disabling global auto-attach also releases Chrome's auto-attached
        # sessions. Still attempt every remaining explicit session afterwards.
        if self._client.is_connected:
            await self._cleanup_command("Target.setAutoAttach", dict(RELEASE_AUTO_ATTACH_PARAMS))
            await asyncio.gather(*(self._release(sid) for sid in list(self._sessions)))
        await self._bounded(self._client.stop(), "stop recorder broker socket")
        if self._client.is_connected:
            raise RuntimeError(
                "Recorder broker socket cleanup is unconfirmed; close may be retried"
            )
        for method, callback in self._listeners.items():
            try:
                self._client.off_event(method, callback)
            except Exception as exc:
                self._cleanup_failures.append(f"unsubscribe {method}: {exc}")
        self._listeners.clear()
        for context in list(self._contexts.values()):
            context._stopped = True
            context._notify_closed(None)
            context._callbacks.clear()
        self._contexts.clear()
        self._sessions.clear()
        self._retired_sessions.clear()
        # In-flight callbacks/releases must not retain a closed host. Their
        # cancellation is bounded and uncooperative operations stay diagnostic.
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=_COMMAND_TIMEOUT_S)
            if unfinished:
                self._cleanup_failures.append(
                    f"{len(unfinished)} recorder broker operation(s) remain unfinished"
                )
        self._closed = True

    async def _activate(self, context: ContextRecorderClient) -> None:
        if not self._opened or self._closing or not self._client.is_connected:
            raise ConnectionError("Recorder broker is not open")
        if context._active:
            return
        context._active = True
        try:
            result = await self._client.send("Target.getTargets", timeout=_COMMAND_TIMEOUT_S)
            for info in result.get("targetInfos", []):
                if (
                    info.get("browserContextId") != context.context_id
                    or info.get("type") not in _EXISTING_ROOT_TYPES
                ):
                    continue
                target_id = str(info["targetId"])
                if any(
                    state.context is context and state.target_id == target_id
                    for state in self._sessions.values()
                ):
                    continue
                answer = await self._client.send(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                    timeout=_COMMAND_TIMEOUT_S,
                )
                # CDP sends attachedToTarget before the reply. The fallback
                # also makes that ready-barrier contract explicit for transports
                # which supply the acknowledgement without the event.
                sid = str(answer["sessionId"])
                if sid not in self._sessions:
                    self._attached(
                        {"sessionId": sid, "targetInfo": info, "waitingForDebugger": False},
                        None,
                    )
        except BaseException:
            context._active = False
            raise

    def _listen(self, method: str) -> None:
        if method in self._listeners:
            return

        def receive(params: dict[str, Any], session_id: str | None = None) -> None:
            if self._closed or self._loss is not None:
                return
            if method == "Target.attachedToTarget":
                self._attached(params, session_id)
            elif method == "Target.detachedFromTarget":
                self._detached(params, session_id)
            elif session_id is not None:
                state = self._sessions.get(session_id)
                if state is not None and state.context is not None:
                    state.context._receive(method, params, session_id)
            else:
                for context in tuple(self._contexts.values()):
                    if context._active:
                        context._receive(method, params, None)

        self._listeners[method] = receive
        self._client.on_event(method, receive)

    def _attached(self, params: dict[str, Any], parent_id: str | None) -> None:
        sid = str(params.get("sessionId") or "")
        if not sid or sid in self._sessions or sid in self._retired_sessions:
            return
        info = dict(params.get("targetInfo") or {})
        parent = self._sessions.get(parent_id) if parent_id is not None else None
        context_id = info.get("browserContextId")
        if parent is not None and parent.context is not None and context_id is None:
            context_id = parent.context.context_id
            info["browserContextId"] = context_id
            params = {**params, "targetInfo": info}
        context = self._contexts.get(str(context_id))
        if (
            self._closing
            or context is None
            or not context._active
            or (parent_id is not None and (parent is None or parent.context is not context))
        ):
            context = None
        self._sessions[sid] = _Session(context, str(info.get("targetId") or ""), parent_id)
        if context is None or not context._receive("Target.attachedToTarget", params, parent_id):
            self._spawn(self._release(sid))

    def _detached(self, params: dict[str, Any], parent_id: str | None) -> None:
        sid = str(params.get("sessionId") or "")
        self._forget(sid, parent_id)

    def _forget(self, sid: str, parent_id: str | None = None) -> None:
        # A parent disappearing retires all its children even if their detached
        # events arrive late or not at all; neither late data nor attach may
        # resurrect this subtree.
        children = [child for child, state in self._sessions.items() if state.parent_id == sid]
        for child in children:
            self._forget(child, sid)
        state = self._sessions.pop(sid, None)
        if sid:
            self._retired_sessions.add(sid)
        if state is not None and state.context is not None:
            state.context._receive(
                "Target.detachedFromTarget",
                {"sessionId": sid, "targetId": state.target_id},
                parent_id,
            )

    async def _release(self, sid: str) -> None:
        if sid not in self._sessions:
            return
        if self._client.is_connected:
            # Fetch.disable also settles any requests still held by an owner
            # whose recorder never reached its own cleanup.
            if self._sessions[sid].context is not None:
                await self._cleanup_command("Fetch.disable", {}, sid)
            if sid not in self._sessions:
                return
            await self._cleanup_command("Runtime.runIfWaitingForDebugger", {}, sid)
            if sid not in self._sessions:
                return
            if await self._cleanup_command("Target.detachFromTarget", {"sessionId": sid}):
                self._forget(sid)
        else:
            self._forget(sid)

    async def _cleanup_command(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> bool:
        return await self._bounded(
            self._client.send(method, params, session_id, timeout=_COMMAND_TIMEOUT_S),
            f"{method} ({session_id or params.get('sessionId', 'browser')})",
        )

    async def _bounded(self, operation: Awaitable[Any], description: str) -> bool:
        task = self._spawn(operation)
        done, _ = await asyncio.wait({task}, timeout=_COMMAND_TIMEOUT_S)
        if not done:
            task.cancel()
            self._cleanup_failures.append(f"{description}: timed out; completion unconfirmed")
            return False
        if task.cancelled():
            self._cleanup_failures.append(f"{description}: cancelled; completion unconfirmed")
            return False
        error = task.exception()
        if error is not None:
            self._cleanup_failures.append(f"{description}: {error}")
            return False
        return True

    def _spawn(self, operation: Awaitable[Any]) -> asyncio.Task[Any]:
        async def run() -> Any:
            return await operation

        task = asyncio.create_task(run())
        self._tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()  # retrieve even when no caller is awaiting it

        task.add_done_callback(finished)
        return task

    def _on_closed(self, reason: BaseException | None) -> None:
        if reason is not None:
            self._loss = reason
        for context in tuple(self._contexts.values()):
            context._notify_closed(reason)


class ContextRecorderClient:
    """The existing RecorderClient contract, scoped to one BrowserContext."""

    def __init__(self, broker: RecorderBroker, context_id: str) -> None:
        self.context_id = context_id
        self._broker = broker
        self._access = BrowserContextAccess(context_id, broker._client)
        self._callbacks: dict[str, list[Callable[..., Any]]] = {}
        self._callback_tasks: set[asyncio.Task[Any]] = set()
        self._close_callbacks: list[Callable[[BaseException | None], None]] = []
        self._active = False
        self._stopped = False
        self._notified = False
        self._close_reason: BaseException | None = None
        self._stop_task: asyncio.Task[None] | None = None

    @property
    def is_connected(self) -> bool:
        return not self._stopped and self._broker._client.is_connected

    def _check_live(self) -> None:
        if self._stopped:
            raise ConnectionError("Context recorder client has stopped")
        if self._broker._loss is not None:
            raise self._broker._loss
        if not self._broker._client.is_connected:
            raise ConnectionError("Recorder broker connection is not available")

    async def _local_auto_attach(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if (params or {}).get("autoAttach"):
            await self._broker._activate(self)
        else:
            self._active = False
        return {}

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        reply = await self.post(method, params, session_id)
        return await reply.wait(timeout)

    async def post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> PendingCommand:
        self._check_live()
        if method == "Target.setDiscoverTargets":
            raise PermissionError("Target discovery belongs to the context's control connection")
        if method == "Target.setAutoAttach" and session_id is None:
            return _Reply(await self._local_auto_attach(params))
        args = await self._access.command(method, params, session_id)
        detached: Callable[[], bool] | None = None
        if method == "Target.detachFromTarget":
            sid = str(args.get("sessionId", ""))

            def is_detached() -> bool:
                return self.is_connected and self._access.is_retired_session(sid)

            detached = is_detached
            if detached():
                return _Reply({})
        try:
            pending = await self._broker._client.post(method, args, session_id)
        except RuntimeError:
            if detached is not None and detached():
                return _Reply({})
            raise
        if detached is not None:
            return _Reply(pending=pending, detached=detached)
        if method == "Target.getTargets":
            return _Reply(
                pending=pending, transform=lambda result: self._access.response(method, result)
            )
        return pending

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        if self._stopped:
            raise ConnectionError("Context recorder client has stopped")
        self._callbacks.setdefault(method, []).append(callback)
        self._broker._listen(method)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        callbacks = self._callbacks.get(method)
        if callbacks is not None:
            with contextlib.suppress(ValueError):
                callbacks.remove(callback)
            if not callbacks:
                self._callbacks.pop(method, None)

    def add_close_callback(self, callback: Callable[[BaseException | None], None]) -> None:
        if self._notified:
            callback(self._close_reason)
        else:
            self._close_callbacks.append(callback)

    def _receive(self, method: str, params: dict[str, Any], session_id: str | None) -> bool:
        if self._stopped or not self._access.accept_event(method, params, session_id):
            return False
        for callback in tuple(self._callbacks.get(method, ())):
            try:
                result = callback(params, session_id)
                if inspect.isawaitable(result):
                    task = self._broker._spawn(result)
                    self._callback_tasks.add(task)
                    task.add_done_callback(self._callback_tasks.discard)
            except Exception:
                logger.exception("context recorder callback failed for %s", method)
        return True

    def _notify_closed(self, reason: BaseException | None) -> None:
        if self._notified:
            return
        self._notified = True
        self._close_reason = reason
        callbacks, self._close_callbacks = self._close_callbacks, []
        for callback in callbacks:
            try:
                callback(reason)
            except Exception:
                logger.exception("context recorder close callback failed")

    async def stop(self) -> None:
        if self._stopped:
            return
        if self._stop_task is None or self._stop_task.done():
            self._stop_task = asyncio.create_task(self._stop())
        await self._broker._finish_cleanup(self._stop_task)

    async def _stop(self) -> None:
        self._active = False
        sessions = [sid for sid, state in self._broker._sessions.items() if state.context is self]
        await asyncio.gather(*(self._broker._release(sid) for sid in sessions))
        remaining = [sid for sid, state in self._broker._sessions.items() if state.context is self]
        callbacks = [task for task in self._callback_tasks if not task.done()]
        for task in callbacks:
            task.cancel()
        if callbacks:
            _, unfinished = await asyncio.wait(callbacks, timeout=_COMMAND_TIMEOUT_S)
            if unfinished:
                detail = (
                    f"Context {self.context_id!r}: {len(unfinished)} recorder callback task(s) "
                    "remain unfinished; cleanup unconfirmed"
                )
                self._broker._cleanup_failures.append(detail)
                raise RuntimeError(detail)
        if remaining and self._broker._client.is_connected:
            raise RuntimeError(f"Context recorder cleanup unconfirmed for sessions {remaining}")
        self._stopped = True
        if self._broker._contexts.get(self.context_id) is self:
            self._broker._contexts.pop(self.context_id)
        self._callbacks.clear()
        self._notify_closed(None)

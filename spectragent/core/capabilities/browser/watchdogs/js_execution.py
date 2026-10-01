"""A shared JavaScript executor connected to each tab's JS scope.

This watchdog provides no observation fragment.  The DOM resolver remains
the sole producer of ``browser.dom``; it can request page-side work by
emitting :class:`ExecuteJavaScriptEvent` on its observation scope.  Each
connection targets that tab's separate JS scope, so the DOM handler may
await the request without blocking its own queued event loop.

One instance listens to any number of tabs.  CDP target-attach events track
which out-of-process iframe sessions belong to each tab, so a request from
one tab cannot name another tab's session as its JS target.
"""

from __future__ import annotations

from typing import Any, ClassVar

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope

from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import ExecuteJavaScriptEvent

_ALLOWED_METHODS = frozenset(
    {
        "Runtime.evaluate",
        "Runtime.callFunctionOn",
        "Runtime.getProperties",
        "Runtime.releaseObjectGroup",
        "Page.createIsolatedWorld",
    }
)


class JavaScriptExecutionWatchdog(BrowserWatchdog):
    """Execute permitted JS CDP commands in a tab-specific async domain."""

    level: ClassVar[WatchdogLevel] = "shared"
    provides: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, cdp: CDPCommandProtocol) -> None:
        self._cdp = cdp
        self._tracking = False
        self._tabs: set[str] = set()
        self._owner_by_child: dict[str, str] = {}
        self._parent_by_child: dict[str, str] = {}

    @classmethod
    def build(cls, ctx: WatchdogContext) -> JavaScriptExecutionWatchdog:
        return cls(ctx.cdp)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        """Watch CDP frame-session attachments for all bound tabs."""
        if self._tracking:
            return
        self._cdp.on_event("Target.attachedToTarget", self._on_attached)
        self._cdp.on_event("Target.detachedFromTarget", self._on_detached)
        self._tracking = True

    def detach(self) -> None:
        """Remove CDP callbacks and all tab/frame ownership records."""
        if self._tracking:
            self._cdp.off_event("Target.attachedToTarget", self._on_attached)
            self._cdp.off_event("Target.detachedFromTarget", self._on_detached)
            self._tracking = False
        self._tabs.clear()
        self._owner_by_child.clear()
        self._parent_by_child.clear()

    def bind_tab_scopes(
        self,
        observation_scope: EventScope,
        js_scope: EventScope,
        *,
        session_id: str,
    ) -> None:
        """Route this tab's JS requests to its independent execution queue."""
        self._tabs.add(session_id)

        async def execute_for_tab(event: ExecuteJavaScriptEvent) -> dict[str, Any]:
            if event.session_id != session_id:
                raise ValueError(
                    "JS request session mismatch: "
                    f"event names {event.session_id!r}, connection serves {session_id!r}"
                )
            target = event.target_session_id or session_id
            if session_id not in self._tabs or (
                target != session_id and self._owner_by_child.get(target) != session_id
            ):
                raise ValueError(
                    f"JS target session {target!r} does not belong to tab {session_id!r}"
                )
            return await self._execute(event, session_id=target)

        observation_scope.connect(
            ExecuteJavaScriptEvent,
            execute_for_tab,
            mode=ConnectionType.QUEUED,
            target_scope=js_scope,
        )

    def unbind_tab(self, session_id: str) -> None:
        """Revoke a closed tab and every child session before its scopes close."""
        self._tabs.discard(session_id)
        for child, owner in list(self._owner_by_child.items()):
            if owner == session_id:
                self._owner_by_child.pop(child, None)
                self._parent_by_child.pop(child, None)

    def _on_attached(self, params: dict[str, Any], parent_session_id: str | None) -> None:
        """Accept only iframe sessions descended from a currently bound tab."""
        if parent_session_id is None:
            return
        owner = (
            parent_session_id
            if parent_session_id in self._tabs
            else self._owner_by_child.get(parent_session_id)
        )
        if owner is None:
            return
        info = params.get("targetInfo") or {}
        child = params.get("sessionId")
        if info.get("type") != "iframe" or not isinstance(child, str) or not child:
            return
        if child in self._tabs:
            return
        if child in self._owner_by_child:
            self._revoke_subtree(child)
        self._owner_by_child[child] = owner
        self._parent_by_child[child] = parent_session_id

    def _on_detached(self, params: dict[str, Any], _parent_session_id: str | None) -> None:
        child = params.get("sessionId")
        if isinstance(child, str):
            if child in self._tabs:
                self.unbind_tab(child)
            else:
                self._revoke_subtree(child)

    def _revoke_subtree(self, child: str) -> None:
        """Forget a detached frame even if nested detach events arrive late."""
        pending = [child]
        while pending:
            session = pending.pop()
            pending.extend(
                candidate
                for candidate, parent in self._parent_by_child.items()
                if parent == session
            )
            self._owner_by_child.pop(session, None)
            self._parent_by_child.pop(session, None)

    async def _execute(self, event: ExecuteJavaScriptEvent, *, session_id: str) -> dict[str, Any]:
        if event.method not in _ALLOWED_METHODS:
            raise ValueError(f"Unsupported JS CDP method: {event.method!r}")

        reply = await self._cdp.send(event.method, event.params, session_id=session_id)
        if not isinstance(reply, dict):
            raise RuntimeError(f"{event.method} returned no CDP reply")

        details = reply.get("exceptionDetails")
        if details is not None:
            message = (
                details.get("text", "JavaScript exception")
                if isinstance(details, dict)
                else str(details)
            )
            if isinstance(details, dict):
                exception = details.get("exception")
                if isinstance(exception, dict) and exception.get("description"):
                    message = str(exception["description"])
            raise RuntimeError(f"{event.method} failed: {message}")

        return reply

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

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope, Participation, ResultContract

from spectragent.core.capabilities.browser.sessions import TabSessions
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

    def __init__(self, cdp: CDPCommandProtocol, *, sessions: TabSessions | None = None) -> None:
        self._cdp = cdp
        self._tracking = False
        self._sessions = sessions if sessions is not None else TabSessions()

    @classmethod
    def build(cls, ctx: WatchdogContext) -> JavaScriptExecutionWatchdog:
        return cls(ctx.cdp, sessions=ctx.tab_sessions)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        """Watch CDP frame-session attachments for all bound tabs."""
        if self._tracking:
            return
        self._cdp.on_event("Target.attachedToTarget", self._sessions.attached)
        self._cdp.on_event("Target.detachedFromTarget", self._sessions.detached)
        self._tracking = True
        self.track_binding(scope, session_id, close_callbacks=[self._stop_tracking])

    def _stop_tracking(self) -> None:
        """Remove CDP callbacks and all tab/frame ownership records."""
        if self._tracking:
            self._cdp.off_event("Target.attachedToTarget", self._sessions.attached)
            self._cdp.off_event("Target.detachedFromTarget", self._sessions.detached)
            self._tracking = False
        self._sessions.clear()

    def bind_tab_scopes(
        self,
        observation_scope: EventScope,
        js_scope: EventScope,
        *,
        session_id: str,
    ) -> None:
        """Route this tab's JS requests to its independent execution queue."""
        self._sessions.bind(session_id)

        async def execute_for_tab(event: ExecuteJavaScriptEvent) -> dict[str, Any]:
            if event.session_id != session_id:
                raise ValueError(
                    "JS request session mismatch: "
                    f"event names {event.session_id!r}, connection serves {session_id!r}"
                )
            target = event.target_session_id or session_id
            if not self._sessions.owns(session_id, target):
                raise ValueError(
                    f"JS target session {target!r} does not belong to tab {session_id!r}"
                )
            return await self._execute(event, session_id=target)

        connection = observation_scope.connect(
            ExecuteJavaScriptEvent,
            execute_for_tab,
            mode=ConnectionType.QUEUED,
            target_scope=js_scope,
            participation=Participation.HANDLER,
            receiver_key="browser.javascript",
            result_contract=ResultContract(lambda value: isinstance(value, dict)),
        )
        binding = self.track_binding(
            observation_scope,
            session_id,
            connections=[connection],
            close_callbacks=[lambda: self._sessions.unbind(session_id)],
        )
        # Either half closing revokes the pair, without affecting another tab.
        js_scope.add_close_callback(binding.close)
        observation_scope.add_close_callback(lambda: js_scope.remove_close_callback(binding.close))

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

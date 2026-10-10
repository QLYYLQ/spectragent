"""ArchiveWatchdog — the archive scope's consumer (§4.14).

The recorder connection forwards small metadata events (what each
participant admitted of the CDP events it declared, and target bookkeeping),
with their session ids, into one **browser-level** archive ``EventScope``.
This watchdog is what drains it: in the scope's own loop it hands every
:class:`RecorderEvent` to the participant that admitted it — target
bookkeeping to every bound archiver's session — and counts it as handled, so
the channel's barrier can tell a dropped event from a quiet browser.

It provides no fragment (precedent: ``JavaScriptExecutionWatchdog``) and is
installed **by binding, not by profile**: :meth:`should_install` answers
from the context, which carries an archive channel only while the recorder
is open and archivers are bound. Built from a context without one it is
inert.

For archivers that must capture *inside* an observation cycle (the
vision-only ``browser.dom_raw`` self-capture), :meth:`bind_tab_scopes`
connects the tab's observation scope to the archive scope across scopes
(``target_scope=``): the cycle's ``BrowserStateRequestEvent`` then also
runs a handler in the archive scope's loop, which the cycle waits for. It
returns no summary, so nothing on the archive side can change the
observation or its document stamps. Tabs keep exactly their two scopes;
there is no per-tab archive scope.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from agent_cdp import ConnectionType, EventScope

from spectragent.core.capabilities.browser.recorder import (
    ArchiveChannel,
    CycleRequest,
    RecorderEvent,
)
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import BrowserStateRequestEvent

logger = logging.getLogger(__name__)


class ArchiveWatchdog(BrowserWatchdog):
    """Hands archive-scope events (and, if wanted, cycles) to archiver sessions."""

    level: ClassVar[WatchdogLevel] = "shared"
    provides: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, channel: ArchiveChannel | None) -> None:
        self._channel = channel
        self._connections: list[Any] = []
        self._tab_connections: dict[str, Any] = {}

    @classmethod
    def should_install(cls, ctx: WatchdogContext) -> bool:
        return ctx.archive is not None

    @classmethod
    def build(cls, ctx: WatchdogContext) -> ArchiveWatchdog:
        return cls(ctx.archive)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        """Listen on the archive scope (not the global scope it is handed)."""
        if self._channel is None or self._connections:
            return
        self._connections.append(
            self._channel.scope.connect(RecorderEvent, self._on_event, mode=ConnectionType.QUEUED)
        )

    async def _on_event(self, event: RecorderEvent) -> None:
        channel = self._channel
        if channel is None:  # pragma: no cover — connected only with a channel
            return
        channel.mark_handled()
        consumer = event.consumer
        for participant in channel.participants():
            # An admitted event is its admitting participant's alone (it may
            # point into that participant's side store); target bookkeeping
            # (no consumer) goes to everyone.
            if consumer is not None and participant is not consumer:
                continue
            try:
                participant.on_event(event)
            except Exception:
                channel.errors += 1
                logger.exception(
                    "archive: %s failed on %s", type(participant).__name__, event.method
                )

    def bind_tab_scopes(
        self,
        observation_scope: EventScope,
        js_scope: EventScope,
        *,
        session_id: str,
    ) -> None:
        """Bring this tab's observation cycles into the archive scope, if wanted."""
        channel = self._channel
        if channel is None or not any(p.wants_cycles for p in channel.participants()):
            return
        target_id = observation_scope.metadata.get("target_id")

        async def archive_cycle(event: BrowserStateRequestEvent) -> None:
            request = CycleRequest(
                target_id=str(target_id) if target_id is not None else None,
                control_session_id=session_id,
                fragments=frozenset(event.fragments),
            )
            for participant in channel.participants():
                if not participant.wants_cycles:
                    continue
                try:
                    await participant.on_cycle(request)
                except Exception:
                    channel.errors += 1
                    logger.exception(
                        "archive: %s failed on a cycle of %s",
                        type(participant).__name__,
                        session_id,
                    )
            return None

        self._tab_connections[session_id] = observation_scope.connect(
            BrowserStateRequestEvent,
            archive_cycle,
            mode=ConnectionType.QUEUED,
            target_scope=channel.scope,
        )

    def unbind_tab(self, session_id: str) -> None:
        connection = self._tab_connections.pop(session_id, None)
        if connection is not None:
            connection.disconnect()

    def detach(self) -> None:
        for connection in [*self._connections, *self._tab_connections.values()]:
            connection.disconnect()
        self._connections.clear()
        self._tab_connections.clear()

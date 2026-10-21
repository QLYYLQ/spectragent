"""ArchiveWatchdog — the archive scope's consumer (§4.14).

The recorder connection forwards small metadata events (what each
participant admitted of the CDP events it declared, and target bookkeeping),
with their session ids, into one **browser-level** archive ``EventScope``.
This watchdog is what drains it: in the scope's own loop it hands every
:class:`RecorderEvent` to the participant that admitted it — target
bookkeeping to every bound archiver's session — and counts it as handled, so
the channel's flush can tell a dropped event from a quiet browser.

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
observation or its document stamps. Archivers that capture *before each
action* (``browser.frames``) are reached the same way: the archive side
emits an ``ActionBoundaryEvent`` on the tab's observation scope before the
action touches the page, and its one handler, here, runs in the archive
scope and hands an :class:`ActionBoundary` to every participant that wants
boundaries. Tabs keep exactly their two scopes; there is no per-tab archive
scope.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from agent_cdp import Connection, ConnectionType, EventScope, Participation, ResultContract

from spectragent.core.capabilities.browser.recorder import (
    ActionBoundary,
    ArchiveChannel,
    CycleRequest,
    RecorderEvent,
)
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    ActionBoundaryEvent,
    BrowserStateRequestEvent,
)

logger = logging.getLogger(__name__)


class ArchiveWatchdog(BrowserWatchdog):
    """Hands archive-scope events (and, if wanted, cycles) to archiver sessions."""

    level: ClassVar[WatchdogLevel] = "shared"
    provides: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, channel: ArchiveChannel | None) -> None:
        self._channel = channel

    @classmethod
    def should_install(cls, ctx: WatchdogContext) -> bool:
        return ctx.archive is not None

    @classmethod
    def build(cls, ctx: WatchdogContext) -> ArchiveWatchdog:
        return cls(ctx.archive)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        """Listen on the archive scope (not the global scope it is handed)."""
        if self._channel is None or any(binding.scope is scope for binding in self.bindings):
            return
        connection = self._channel.scope.connect(
            RecorderEvent,
            self._on_event,
            mode=ConnectionType.QUEUED,
            participation=Participation.HANDLER,
            receiver_key="archive.metadata",
            result_contract=ResultContract(lambda value: value is None),
        )
        self.track_binding(scope, session_id, connections=[connection])

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
        """Bring this tab's observation cycles and action boundaries into the archive scope.

        Each only if a participant wants it when the tab is bound; who
        receives is decided at delivery, from the participants bound then.
        """
        channel = self._channel
        if channel is None:
            return
        participants = channel.participants()
        wants_cycles = any(p.wants_cycles for p in participants)
        wants_boundaries = any(p.wants_action_boundaries for p in participants)
        if not (wants_cycles or wants_boundaries):
            return
        target_id = observation_scope.metadata.get("target_id")
        tab = str(target_id) if target_id is not None else None

        async def archive_cycle(event: BrowserStateRequestEvent) -> None:
            request = CycleRequest(
                target_id=tab,
                control_session_id=session_id,
                fragments=frozenset(event.fragments),
                request_id=event.event_id,
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

        async def archive_boundary(event: ActionBoundaryEvent) -> None:
            boundary = ActionBoundary(
                target_id=tab,
                control_session_id=session_id,
                action=event.action_name,
                requested_at=event.requested_at,
            )
            for participant in channel.participants():
                if not participant.wants_action_boundaries:
                    continue
                try:
                    await participant.on_action_boundary(boundary)
                except Exception:
                    channel.errors += 1
                    logger.exception(
                        "archive: %s failed on the boundary of %s on %s",
                        type(participant).__name__,
                        event.action_name,
                        session_id,
                    )
            return None

        self.unbind_tab(session_id)
        connections: list[Connection] = []
        if wants_cycles:
            connections.append(
                observation_scope.connect(
                    BrowserStateRequestEvent,
                    archive_cycle,
                    mode=ConnectionType.QUEUED,
                    target_scope=channel.scope,
                    participation=Participation.OBSERVER,
                    result_contract=ResultContract(lambda value: value is None),
                )
            )
        if wants_boundaries:
            connections.append(
                observation_scope.connect(
                    ActionBoundaryEvent,
                    archive_boundary,
                    mode=ConnectionType.QUEUED,
                    target_scope=channel.scope,
                    participation=Participation.HANDLER,
                    receiver_key="archive.action_boundary",
                    result_contract=ResultContract(lambda value: value is None),
                )
            )
        self.track_binding(observation_scope, session_id, connections=connections)

    def unbind_tab(self, session_id: str) -> None:
        for binding in self.bindings:
            if binding.session_id == session_id:
                self.detach(binding.scope, session_id)

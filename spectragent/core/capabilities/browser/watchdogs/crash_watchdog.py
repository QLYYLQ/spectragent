"""CrashWatchdog — browser target crash detection and focus recovery.

Bridges CDP ``Target.targetCrashed`` events and emits ``TargetCrashedEvent``.
Resolves Inspector envelope sessions through capability-owned target mappings.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import ClassVar

from agent_cdp import (
    CDPCommandProtocol,
    CDPEventBridge,
    ConnectionType,
    EventScope,
    Participation,
)

from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserErrorEvent,
    TargetCrashedEvent,
)

logger = logging.getLogger(__name__)


class CrashWatchdog(BrowserWatchdog):
    """Detects browser target crashes via CDP and emits TargetCrashedEvent."""

    level: ClassVar[WatchdogLevel] = "browser"

    def __init__(
        self, cdp: CDPCommandProtocol, *, target_sessions_getter: Callable[[], Mapping[str, str]]
    ) -> None:
        self._cdp = cdp
        self._target_sessions = target_sessions_getter

    @classmethod
    def build(cls, ctx: WatchdogContext) -> CrashWatchdog:
        return cls(ctx.cdp, target_sessions_getter=ctx.target_sessions_getter)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        # Inspector events identify the target through the envelope session;
        # Target.targetCrashed is browser-level and names the target in params.
        bridge = CDPEventBridge(self._cdp, scope, session_id=session_id)
        bridge.bridge(
            "Inspector.targetCrashed",
            lambda params, route: TargetCrashedEvent(
                target_id=self._target_sessions().get(route or "", ""),
                error=params.get("reason", "target crashed"),
            ),
            with_session=True,
        )
        bridge.bridge(
            "Target.targetCrashed",
            lambda params: TargetCrashedEvent(
                target_id=params.get("targetId", ""),
                error=params.get("status", "target crashed"),
            ),
        )
        bridge.bridge(
            "Inspector.detached",
            lambda params: BrowserErrorEvent(
                error_type="inspector_detached",
                message=params.get("reason", ""),
            ),
        )
        connection = scope.connect(
            TargetCrashedEvent,
            self._on_target_crashed,
            mode=ConnectionType.QUEUED,
            participation=Participation.OBSERVER,
            filter=lambda event: (
                isinstance(event, TargetCrashedEvent)
                and bool(event.target_id)
                and event.target_id in self._target_sessions().values()
            ),
        )
        self.track_binding(
            scope, session_id, connections=[connection], close_callbacks=[bridge.close]
        )

    async def _on_target_crashed(self, event: TargetCrashedEvent) -> None:
        logger.error(
            "Target crashed: target_id=%s error=%s",
            event.target_id,
            event.error,
        )
